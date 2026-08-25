from __future__ import annotations

import json
from pathlib import Path

import pytest

from bossmode.cli import main
from bossmode.registry import Registry, RegistryError


def test_maintenance_on_fresh_database(tmp_path: Path) -> None:
    db_path = tmp_path / "control.db"
    registry = Registry(db_path)
    report = registry.run_maintenance()

    assert report["id"].startswith("maint_")
    assert report["database"]["integrity"] == "ok"
    assert report["database"]["journal_mode"] == "wal"
    assert report["database"]["size_bytes"] > 0
    assert report["health"]["status"] == "healthy"
    assert report["health"]["active_runs"] == 0
    assert report["health"]["orphaned_turns"] == 0
    assert report["health"]["stale_herdr_bindings"] == 0
    assert report["telemetry"] == []
    assert report["promotions"]["new_proposals_count"] == 0

    runs = registry.list_maintenance_runs()
    assert len(runs) == 1
    assert runs[0]["id"] == report["id"]
    assert runs[0]["status"] == "succeeded"
    assert runs[0]["summary"]["health"]["status"] == "healthy"


def test_maintenance_telemetry_aggregations(tmp_path: Path) -> None:
    db_path = tmp_path / "control.db"
    registry = Registry(db_path)

    # Task 1 & Run 1: Claude with high reasoning and tokens
    task1 = registry.create_task(title="Task 1", goal="Goal 1", success_criteria="Pass")
    run1 = registry.start_run(
        task1["id"],
        agent_role="worker_claude",
        model="claude-3-7-sonnet",
        reasoning_effort="high",
    )
    registry.finish_run(
        run1["id"],
        outcome="succeeded",
        summary="Done 1",
        tokens=10000,
        duration_seconds=20.0,
    )

    # Task 2 & Run 2: Claude with high reasoning, failed run
    task2 = registry.create_task(title="Task 2", goal="Goal 2", success_criteria="Pass")
    run2 = registry.start_run(
        task2["id"],
        agent_role="worker_claude",
        model="claude-3-7-sonnet",
        reasoning_effort="high",
    )
    registry.finish_run(
        run2["id"],
        outcome="failed",
        summary="Failed 2",
        tokens=6000,
        duration_seconds=10.0,
    )

    # Task 3 & Run 3: Pi with no reasoning and no tokens reported
    task3 = registry.create_task(title="Task 3", goal="Goal 3", success_criteria="Pass")
    run3 = registry.start_run(
        task3["id"],
        agent_role="worker_pi",
        model="pi-base",
    )
    registry.finish_run(
        run3["id"],
        outcome="succeeded",
        summary="Done 3",
        duration_seconds=5.0,
    )

    report = registry.run_maintenance()
    telemetry = report["telemetry"]
    assert len(telemetry) == 2

    claude_stats = next(t for t in telemetry if t["model"] == "claude-3-7-sonnet")
    assert claude_stats["reasoning_effort"] == "high"
    assert claude_stats["total_runs"] == 2
    assert claude_stats["runs_with_tokens"] == 2
    assert claude_stats["avg_tokens"] == 8000.0
    assert claude_stats["avg_duration_sec"] == 15.0
    assert claude_stats["success_rate_pct"] == 50.0

    pi_stats = next(t for t in telemetry if t["model"] == "pi-base")
    assert pi_stats["reasoning_effort"] == "none"
    assert pi_stats["total_runs"] == 1
    assert pi_stats["runs_with_tokens"] == 0
    assert pi_stats["avg_tokens"] is None
    assert pi_stats["avg_duration_sec"] == 5.0
    assert pi_stats["success_rate_pct"] == 100.0


def test_maintenance_detects_orphaned_turns_and_warning_health(tmp_path: Path) -> None:
    db_path = tmp_path / "control.db"
    registry = Registry(db_path)

    task = registry.create_task(title="Orphan Task", goal="Test", success_criteria="Pass")
    run = registry.start_run(task["id"], agent_role="worker_claude")
    registry.bind_herdr_run(
        run["id"],
        herdr_session="bossmode",
        worker_name="worker_orphan",
        agent_kind="claude",
    )
    registry.start_turn(run["id"], purpose="task", prompt="Test prompt")

    # Manually finish run without finishing turn (simulating abnormal termination)
    with registry._transaction() as conn:
        conn.execute(
            "UPDATE runs SET status = 'finished', outcome = 'failed' WHERE id = ?",
            (run["id"],),
        )

    report = registry.run_maintenance()
    assert report["health"]["orphaned_turns"] == 1
    assert report["health"]["status"] == "warning"


def test_maintenance_discovers_promotions(tmp_path: Path) -> None:
    db_path = tmp_path / "control.db"
    registry = Registry(db_path)

    task = registry.create_task(title="Feedback Task", goal="Test", success_criteria="Pass")
    registry.add_feedback(
        task["id"],
        category="failure",
        recurrence_key="env.missing-var",
        content="Missing env 1",
    )
    registry.add_feedback(
        task["id"],
        category="failure",
        recurrence_key="env.missing-var",
        content="Missing env 2",
    )

    report = registry.run_maintenance()
    assert report["promotions"]["new_proposals_count"] == 1
    assert report["promotions"]["pending_approval_count"] == 1
    assert report["promotions"]["new_proposals"][0]["target_layer"] == "control"
    assert report["promotions"]["new_proposals"][0]["recurrence_key"] == "env.missing-var"

    second = registry.run_maintenance()
    assert second["promotions"]["new_proposals_count"] == 0
    assert second["promotions"]["pending_approval_count"] == 1


def test_maintenance_records_collection_failures(tmp_path: Path, monkeypatch) -> None:
    registry = Registry(tmp_path / "control.db")
    registry.initialize()

    def fail_scan():
        raise RuntimeError("promotion scan failed")

    monkeypatch.setattr(registry, "propose_promotions", fail_scan)
    with pytest.raises(RegistryError, match="maintenance failed: promotion scan failed"):
        registry.run_maintenance()

    runs = registry.list_maintenance_runs()
    assert len(runs) == 1
    assert runs[0]["status"] == "failed"
    assert runs[0]["summary"] == {}
    assert runs[0]["error_message"] == "promotion scan failed"


def test_maintenance_records_success_persistence_failures(tmp_path: Path, monkeypatch) -> None:
    registry = Registry(tmp_path / "control.db")
    registry.initialize()
    original = registry._record_maintenance_run
    attempts = 0

    def fail_once(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("success record failed")
        return original(*args, **kwargs)

    monkeypatch.setattr(registry, "_record_maintenance_run", fail_once)
    with pytest.raises(RegistryError, match="maintenance failed: success record failed"):
        registry.run_maintenance()

    runs = registry.list_maintenance_runs()
    assert len(runs) == 1
    assert runs[0]["status"] == "failed"
    assert runs[0]["error_message"] == "success record failed"


def test_cli_maintenance_command(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "control.db"
    code = main(["--db", str(db_path), "maintenance"])
    assert code == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert "id" in data
    assert data["database"]["integrity"] == "ok"
    assert data["health"]["status"] == "healthy"
