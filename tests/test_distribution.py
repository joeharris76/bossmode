from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.integration
def test_built_wheel_supports_full_cli_round_trip(tmp_path: Path) -> None:
    source_root = Path(__file__).parents[1]
    dist_dir = tmp_path / "dist"
    environment = tmp_path / "environment"
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(dist_dir)],
        cwd=source_root,
        capture_output=True,
        text=True,
        check=True,
    )
    wheel = next(dist_dir.glob("*.whl"))
    subprocess.run(
        ["uv", "venv", "--python", sys.executable, str(environment)],
        capture_output=True,
        text=True,
        check=True,
    )
    executable = environment / "bin" / "bossmode"
    subprocess.run(
        ["uv", "pip", "install", "--python", str(environment / "bin" / "python"), str(wheel)],
        capture_output=True,
        text=True,
        check=True,
    )
    database = tmp_path / "control.db"

    def invoke(*arguments: str, expected_exit: int = 0) -> dict:
        completed = subprocess.run(
            [str(executable), "--db", str(database), *arguments],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == expected_exit, completed.stderr
        return json.loads(completed.stdout if expected_exit == 0 else completed.stderr)

    assert invoke()["dispatch"] is None
    task = invoke(
        "task",
        "create",
        "--title",
        "Wheel task",
        "--goal",
        "Exercise the installed distribution",
        "--success-criteria",
        "The persisted lifecycle succeeds",
    )
    run = invoke("run", "start", task["id"], "--role", "worker")
    invoke(
        "herdr",
        "bind",
        run["id"],
        "--herdr-session",
        "bossmode",
        "--worker",
        "wheel_worker",
        "--kind",
        "codex",
    )
    turn = invoke(
        "turn",
        "start",
        run["id"],
        "--purpose",
        "task",
        "--prompt",
        "Complete the wheel smoke",
    )
    artifact = tmp_path / turn["artifact_path"]
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(
        json.dumps(
            {
                "turn_id": turn["id"],
                "status": "succeeded",
                "summary": "Wheel smoke completed",
                "artifacts": [],
            }
        )
    )
    invoke(
        "turn",
        "finish",
        turn["id"],
        "--status",
        "succeeded",
        "--summary",
        "Wheel smoke completed",
    )
    invoke(
        "run",
        "finish",
        run["id"],
        "--outcome",
        "succeeded",
        "--summary",
        "Installed CLI completed the task",
    )
    invoke(
        "evaluate",
        task["id"],
        "--run-id",
        run["id"],
        "--evaluator",
        "reviewer",
        "--passed",
        "--evidence",
        "Installed-wheel lifecycle verified",
    )
    persisted = invoke("task", "show", task["id"])
    assert persisted["state"] == "succeeded"
    assert persisted["runs"][0]["turns"][0]["result"]["summary"] == "Wheel smoke completed"

    assert invoke("task", "show", "missing", expected_exit=2) == {
        "error": "task not found: missing"
    }
