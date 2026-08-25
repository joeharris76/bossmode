from __future__ import annotations

import json
import subprocess
import sys
import zipfile
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
    canonical_skill = source_root / ".agents" / "skills" / "bossmode" / "SKILL.md"
    with zipfile.ZipFile(wheel) as wheel_archive:
        assert (
            wheel_archive.read("bossmode/skills/bossmode/SKILL.md") == canonical_skill.read_bytes()
        )
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
    project = tmp_path / "project"
    project.mkdir()
    database = project / ".bossmode" / "control.db"

    def invoke(*arguments: str, expected_exit: int = 0) -> dict:
        # `install-skill` touches no registry and rejects --db.
        prefix = [] if arguments[:1] == ("install-skill",) else ["--db", str(database)]
        completed = subprocess.run(
            [str(executable), *prefix, *arguments],
            cwd=project,
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == expected_exit, completed.stderr
        return json.loads(completed.stdout if expected_exit == 0 else completed.stderr)

    initialized = invoke("install-skill", "--project-dir", str(project))
    installed_skill = project / ".agents" / "skills" / "bossmode" / "SKILL.md"
    assert initialized["status"] == "installed"
    assert initialized["skill_version"] == "0.1.0"
    assert initialized["skill_path"] == str(installed_skill)
    assert "one bounded task" in initialized["next_prompt"]
    assert installed_skill.read_bytes() == canonical_skill.read_bytes()
    assert invoke("install-skill", "--project-dir", str(project))["status"] == "already_installed"
    assert not database.exists()

    assert invoke()["next_task"] is None
    requested_outcome = "Create proof.txt containing clean-install-ready."
    supervisor_prompt = initialized["next_prompt"].replace(
        "[describe the outcome you want]", requested_outcome
    )
    assert supervisor_prompt.endswith(requested_outcome)
    task = invoke(
        "task",
        "create",
        "--title",
        "Create clean-install proof",
        "--goal",
        requested_outcome,
        "--success-criteria",
        "proof.txt exists and contains exactly clean-install-ready.",
        "--permissions-json",
        '{"filesystem":{"write":["proof.txt"]},"network":false}',
    )
    assert task["goal"] == requested_outcome
    assert task["permissions"]["network"] is False
    run = invoke("run", "start", task["id"], "--role", "worker")
    invoke(
        "herdr",
        "bind",
        run["id"],
        "--herdr-session",
        "bossmode",
        "--worker",
        "wheel_worker",
        "--agent-kind",
        "codex",
    )
    turn = invoke(
        "turn",
        "start",
        run["id"],
        "--purpose",
        "task",
        "--prompt",
        requested_outcome,
    )
    proof = project / "proof.txt"
    proof.write_text("clean-install-ready.\n")
    artifact = project / turn["artifact_path"]
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(
        json.dumps(
            {
                "turn_id": turn["id"],
                "status": "succeeded",
                "summary": "Created and checked proof.txt",
                "artifacts": [{"path": "proof.txt", "kind": "proof"}],
            }
        )
    )
    invoke(
        "turn",
        "finish",
        turn["id"],
        "--outcome",
        "succeeded",
        "--summary",
        "Created and checked proof.txt",
    )
    invoke(
        "run",
        "finish",
        run["id"],
        "--outcome",
        "succeeded",
        "--summary",
        "Installed CLI completed the bounded task",
        "--artifacts-json",
        '[{"path":"proof.txt","kind":"proof"}]',
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
        "proof.txt contains exactly clean-install-ready.",
    )
    persisted = invoke("task", "show", task["id"])
    assert persisted["state"] == "succeeded"
    assert persisted["runs"][0]["turns"][0]["result"]["summary"] == (
        "Created and checked proof.txt"
    )
    assert proof.read_text() == "clean-install-ready.\n"

    assert invoke("task", "show", "missing", expected_exit=2) == {
        "error": "task not found: missing"
    }
