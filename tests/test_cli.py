from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from bossmode.cli import main


def test_cli_smoke(tmp_path, capsys):
    database = tmp_path / "control.db"

    # 1. Naked bossmode invocation on fresh DB auto-initializes and returns clean state
    assert main(["--db", str(database)]) == 0
    state = json.loads(capsys.readouterr().out)
    assert state["dispatch"] is None
    assert state["active"] == []
    assert database.exists()

    # 2. Create task using `task create`
    assert (
        main(
            [
                "--db",
                str(database),
                "task",
                "create",
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

    # 3. Re-run naked bossmode to confirm single-flight dispatch of ready task
    assert main(["--db", str(database)]) == 0
    reconciled = json.loads(capsys.readouterr().out)
    assert reconciled["dispatch"]["id"] == task["id"]


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
    assert main(["--db", str(database)]) == 0
    capsys.readouterr()
    lock = sqlite3.connect(database, isolation_level=None)
    lock.execute("BEGIN IMMEDIATE")
    try:
        exit_code = main(["--db", str(database)])
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
                "create",
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
                "bossmode",
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
    assert turn["artifact_path"].startswith(".bossmode/turns/turn_")

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


def test_cli_promotion_lifecycle_commands(tmp_path, capsys):
    database = tmp_path / "control.db"
    assert (
        main(
            [
                "--db",
                str(database),
                "task",
                "create",
                "--title",
                "Feedback Task",
                "--goal",
                "Test promotions",
                "--success-criteria",
                "Pass",
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
                "feedback",
                task["id"],
                "--kind",
                "failure",
                "--key",
                "env.missing-key",
                "--content",
                "Failure 1",
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "--db",
                str(database),
                "feedback",
                task["id"],
                "--kind",
                "failure",
                "--key",
                "env.missing-key",
                "--content",
                "Failure 2",
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert main(["--db", str(database), "promotion", "propose"]) == 0
    proposals = json.loads(capsys.readouterr().out)
    assert len(proposals) == 1
    prop_id = proposals[0]["id"]

    assert main(["--db", str(database), "promotion", "accept", prop_id]) == 0
    accepted = json.loads(capsys.readouterr().out)
    assert accepted["status"] == "accepted"

    assert main(["--db", str(database), "promotion", "apply", prop_id]) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["status"] == "applied"


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


@pytest.mark.parametrize("kind", ["pi", "codex", "claude", "agy", "grok", "muse"])
def test_cli_supports_all_agent_kinds(tmp_path, capsys, monkeypatch, kind):
    monkeypatch.chdir(tmp_path)
    database = tmp_path / "control.db"

    assert (
        main(
            [
                "--db",
                str(database),
                "task",
                "create",
                "--title",
                f"Task for {kind}",
                "--goal",
                "Verify agent kind support",
                "--success-criteria",
                "Run completes successfully",
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
                kind,
                "--model",
                f"{kind}-base",
            ]
        )
        == 0
    )
    run = json.loads(capsys.readouterr().out)
    assert run["agent_role"] == kind

    assert (
        main(
            [
                "--db",
                str(database),
                "herdr",
                "bind",
                run["id"],
                "--herdr-session",
                "bossmode",
                "--worker",
                f"worker_{kind}",
                "--kind",
                kind,
                "--session-source",
                f"herdr:{kind}",
                "--session-agent",
                kind,
                "--session-ref-kind",
                "id",
                "--session-value",
                f"{kind}-123",
            ]
        )
        == 0
    )
    binding = json.loads(capsys.readouterr().out)
    assert binding["agent_kind"] == kind
