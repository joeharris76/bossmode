from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from continual_agent.cli import main


def test_cli_smoke(tmp_path, capsys):
    database = tmp_path / "control.db"

    assert main(["--db", str(database), "init"]) == 0
    initialized = json.loads(capsys.readouterr().out)
    assert initialized["initialized"] is True

    assert (
        main(
            [
                "--db",
                str(database),
                "task",
                "add",
                "--title",
                "Smoke task",
                "--goal",
                "Exercise the CLI",
                "--success-criteria",
                "Supervisor selects the task",
                "--priority",
                "5",
            ]
        )
        == 0
    )
    task = json.loads(capsys.readouterr().out)

    assert main(["--db", str(database), "supervisor", "tick"]) == 0
    tick = json.loads(capsys.readouterr().out)
    assert tick["dispatch"]["id"] == task["id"]


def test_cli_returns_structured_error(tmp_path, capsys):
    database = tmp_path / "control.db"

    exit_code = main(
        [
            "--db",
            str(database),
            "task",
            "transition",
            "missing",
            "ready",
            "--actor",
            "supervisor",
            "--reason",
            "test",
        ]
    )

    assert exit_code == 2
    error = json.loads(capsys.readouterr().err)
    assert error == {"error": "task not found: missing"}


def test_cli_returns_structured_error_when_database_is_locked(tmp_path, capsys):
    database = tmp_path / "control.db"
    assert main(["--db", str(database), "init"]) == 0
    capsys.readouterr()
    lock = sqlite3.connect(database, isolation_level=None)
    lock.execute("BEGIN IMMEDIATE")
    try:
        exit_code = main(["--db", str(database), "supervisor", "tick"])
    finally:
        lock.rollback()
        lock.close()

    assert exit_code == 2
    error = json.loads(capsys.readouterr().err)
    assert error == {"error": "database error: database is locked"}


def test_cli_records_herdr_binding_and_turn(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    database = tmp_path / "control.db"
    assert (
        main(
            [
                "--db",
                str(database),
                "task",
                "add",
                "--title",
                "Herdr task",
                "--goal",
                "Delegate through Herdr",
                "--success-criteria",
                "A correlated artifact exists",
            ]
        )
        == 0
    )
    task = json.loads(capsys.readouterr().out)
    assert (
        main(
            [
                "--db",
                str(database),
                "run",
                "start",
                task["id"],
                "--role",
                "claude",
            ]
        )
        == 0
    )
    run = json.loads(capsys.readouterr().out)

    assert (
        main(
            [
                "--db",
                str(database),
                "herdr",
                "bind",
                run["id"],
                "--herdr-session",
                "continual-agent",
                "--worker",
                "worker_1234",
                "--kind",
                "claude",
                "--session-source",
                "herdr:claude",
                "--session-agent",
                "claude",
                "--session-ref-kind",
                "id",
                "--session-value",
                "session-1",
            ]
        )
        == 0
    )
    binding = json.loads(capsys.readouterr().out)
    assert binding["native_session"]["value"] == "session-1"

    assert (
        main(
            [
                "--db",
                str(database),
                "turn",
                "start",
                run["id"],
                "--purpose",
                "task",
                "--prompt",
                "Write the requested artifact",
            ]
        )
        == 0
    )
    turn = json.loads(capsys.readouterr().out)
    assert turn["status"] == "running"
    assert turn["artifact_path"].startswith(".continual/turns/turn_")

    assert main(["--db", str(database), "run", "show", run["id"]]) == 0
    shown_run = json.loads(capsys.readouterr().out)
    assert shown_run["turns"][0]["id"] == turn["id"]
    assert shown_run["herdr_binding"]["worker_name"] == "worker_1234"

    assert main(["--db", str(database), "turn", "show", turn["id"]]) == 0
    shown_turn = json.loads(capsys.readouterr().out)
    assert shown_turn["artifact_path"] == turn["artifact_path"]

    assert main(["--db", str(database), "task", "show", task["id"]]) == 0
    shown_task = json.loads(capsys.readouterr().out)
    assert shown_task["runs"][0]["turns"][0]["id"] == turn["id"]

    result_path = Path(turn["artifact_path"])
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(
        json.dumps(
            {
                "turn_id": turn["id"],
                "status": "succeeded",
                "summary": "CLI result verified",
                "artifacts": [],
            }
        )
    )
    assert (
        main(
            [
                "--db",
                str(database),
                "turn",
                "finish",
                turn["id"],
                "--status",
                "succeeded",
            ]
        )
        == 0
    )
    finished_turn = json.loads(capsys.readouterr().out)
    assert finished_turn["result"]["summary"] == "CLI result verified"
    assert finished_turn["prompt"] == "Write the requested artifact"

    assert (
        main(
            [
                "--db",
                str(database),
                "run",
                "finish",
                run["id"],
                "--outcome",
                "succeeded",
                "--summary",
                "Run succeeded",
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert (
        main(
            [
                "--db",
                str(database),
                "evaluate",
                task["id"],
                "--run-id",
                run["id"],
                "--evaluator",
                "reviewer",
                "--passed",
                "--evidence",
                "CLI evaluation passed",
            ]
        )
        == 0
    )
    evaluation = json.loads(capsys.readouterr().out)
    assert evaluation["passed"] == 1
    assert evaluation["run_id"] == run["id"]


def test_cli_herdr_bind_rejects_stale_status(tmp_path):
    database = tmp_path / "control.db"
    with pytest.raises(SystemExit) as exc_info:
        main(
            [
                "--db",
                str(database),
                "herdr",
                "bind",
                "run-123",
                "--herdr-session",
                "session",
                "--worker",
                "worker1",
                "--kind",
                "claude",
                "--status",
                "stale",
            ]
        )
    assert exc_info.value.code == 2
