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
