from __future__ import annotations

import json

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


def test_cli_records_herdr_binding_and_turn(tmp_path, capsys):
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
