from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from continual_agent.registry import MAX_TURN_RESULT_BYTES, Registry, RegistryError


@pytest.fixture
def registry(tmp_path):
    return Registry(tmp_path / "control.db")


def create_task(registry: Registry, *, title: str = "Test task", priority: int = 0):
    return registry.create_task(
        title=title,
        goal=f"Complete {title}",
        success_criteria="Independent evidence passes",
        priority=priority,
    )


def write_turn_result(
    turn: dict,
    *,
    turn_id: str | None = None,
    status: str = "succeeded",
    summary: str = "Result verified",
    artifacts: list[dict] | None = None,
) -> None:
    path = Path(turn["artifact_path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "turn_id": turn_id or turn["id"],
                "status": status,
                "summary": summary,
                "artifacts": artifacts or [],
            }
        )
    )


def test_dispatch_and_completion_are_transactional(registry):
    task = create_task(registry)

    run = registry.start_run(
        task["id"],
        agent_role="worker",
        thread_id="thread-123",
        model="test-model",
        reasoning_effort="medium",
    )
    finished = registry.finish_run(
        run["id"],
        outcome="succeeded",
        summary="Implemented and tested",
        artifacts=[{"path": "result.md", "kind": "report"}],
        tokens=100,
        duration_seconds=2.5,
    )

    assert finished["outcome"] == "succeeded"
    task_after_run = registry.get_task(task["id"])
    assert task_after_run["state"] == "evaluating"

    registry.add_evaluation(
        task["id"],
        run_id=run["id"],
        evaluator="reviewer",
        passed=True,
        evidence="Artifact content and targeted tests passed",
    )
    task_after = registry.get_task(task["id"])
    assert task_after["state"] == "succeeded"
    assert [event["event_type"] for event in task_after["events"]] == [
        "created",
        "run_started",
        "run_finished",
        "evaluated",
    ]


def test_invalid_lifecycle_transition_fails_closed(registry):
    task = create_task(registry)

    with pytest.raises(RegistryError, match="ready -> succeeded"):
        registry.transition_task(
            task["id"],
            "succeeded",
            actor="supervisor",
            reason="unverified shortcut",
        )

    assert registry.get_task(task["id"])["state"] == "ready"


def test_task_creation_rejects_terminal_and_runtime_owned_states(registry):
    for state in ("running", "evaluating", "succeeded", "failed", "archived"):
        with pytest.raises(RegistryError, match="invalid initial task state"):
            registry.create_task(
                title=state,
                goal="Do not bypass lifecycle gates",
                success_criteria="Creation is rejected",
                state=state,
            )


def test_only_run_operations_may_enter_or_leave_running(registry):
    task = create_task(registry)
    run = registry.start_run(task["id"], agent_role="worker")

    with pytest.raises(RegistryError, match="running -> blocked"):
        registry.transition_task(
            task["id"],
            "blocked",
            actor="supervisor",
            reason="manual shortcut",
        )

    assert registry.get_run(run["id"])["status"] == "running"
    registry.finish_run(run["id"], outcome="waiting_user", summary="Need a decision")
    with pytest.raises(RegistryError, match="waiting_user -> running"):
        registry.transition_task(
            task["id"],
            "running",
            actor="supervisor",
            reason="manual resume",
        )


def test_start_run_rejects_an_existing_open_run_in_inconsistent_state(registry):
    task = create_task(registry)
    run = registry.start_run(task["id"], agent_role="worker")
    with registry._connect() as connection:
        connection.execute("UPDATE tasks SET state = 'ready' WHERE id = ?", (task["id"],))

    with pytest.raises(RegistryError, match=f"running run: {run['id']}"):
        registry.start_run(task["id"], agent_role="worker")


def test_supervisor_selects_highest_priority_ready_task(registry):
    low = create_task(registry, title="Low", priority=1)
    high = create_task(registry, title="High", priority=10)

    tick = registry.supervisor_tick()

    assert tick["dispatch"]["id"] == high["id"]
    assert tick["dispatch"]["id"] != low["id"]


def test_supervisor_is_single_flight_and_returns_recovery_details(registry):
    active_task = create_task(registry, title="Active", priority=1)
    active_run = registry.start_run(active_task["id"], agent_role="worker")
    create_task(registry, title="Ready", priority=10)

    tick = registry.supervisor_tick()

    assert tick["dispatch"] is None
    assert tick["active"][0]["runs"][0]["id"] == active_run["id"]
    assert tick["active"][0]["runs"][0]["turns"] == []


def test_supervisor_exposes_tasks_needing_evaluation(registry):
    task = create_task(registry)
    run = registry.start_run(task["id"], agent_role="worker")
    registry.finish_run(run["id"], outcome="succeeded", summary="Execution completed")

    tick = registry.supervisor_tick()

    assert [item["id"] for item in tick["needs_evaluation"]] == [task["id"]]
    assert tick["dispatch"] is None


def test_failed_evaluation_fails_the_task(registry):
    task = create_task(registry)
    run = registry.start_run(task["id"], agent_role="worker")
    registry.finish_run(run["id"], outcome="succeeded", summary="Execution completed")

    registry.add_evaluation(
        task["id"],
        run_id=run["id"],
        evaluator="reviewer",
        passed=False,
        evidence="Success criterion was not demonstrated",
    )

    assert registry.get_task(task["id"])["state"] == "failed"


def test_evaluation_rejects_same_run_role(registry):
    task = create_task(registry)
    run = registry.start_run(task["id"], agent_role="reviewer")
    registry.finish_run(run["id"], outcome="succeeded", summary="Execution completed")

    with pytest.raises(RegistryError, match="must be independent"):
        registry.add_evaluation(
            task["id"],
            run_id=run["id"],
            evaluator="reviewer",
            passed=True,
            evidence="self report",
        )

    assert registry.get_task(task["id"])["state"] == "evaluating"


def test_herdr_binding_records_native_session_and_correlated_turns(registry, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    task = create_task(registry)
    run = registry.start_run(task["id"], agent_role="claude")
    binding = registry.bind_herdr_run(
        run["id"],
        herdr_session="continual-agent",
        worker_name="worker_1234",
        agent_kind="claude",
        session_source="herdr:claude",
        session_agent="claude",
        session_ref_kind="id",
        session_value="claude-session-1",
        pane_id="w1:p2",
        tab_id="w1:t2",
        workspace_id="w1",
    )

    first = registry.start_turn(run["id"], purpose="task", prompt="Produce the report")
    write_turn_result(first, summary="Report written")
    registry.finish_turn(
        first["id"],
        status="succeeded",
        summary="Report written",
        lifecycle_evidence="done",
    )
    second = registry.start_turn(
        run["id"], purpose="review_follow_up", prompt="Address the reviewer correction"
    )

    assert binding["native_session"] == {
        "source": "herdr:claude",
        "agent": "claude",
        "kind": "id",
        "value": "claude-session-1",
    }
    assert first["artifact_path"].endswith(f"{first['id']}.json")
    assert first["prompt_digest"] != second["prompt_digest"]
    assert second["ordinal"] == 2
    assert [turn["status"] for turn in registry.get_run(run["id"])["turns"]] == [
        "succeeded",
        "running",
    ]


def test_herdr_binding_refuses_native_session_substitution(registry):
    task = create_task(registry)
    run = registry.start_run(task["id"], agent_role="codex")
    registry.bind_herdr_run(
        run["id"],
        herdr_session="continual-agent",
        worker_name="worker_5678",
        agent_kind="codex",
        session_source="herdr:codex",
        session_agent="codex",
        session_ref_kind="id",
        session_value="codex-thread-1",
    )

    with pytest.raises(RegistryError, match="replace native Herdr session"):
        registry.bind_herdr_run(
            run["id"],
            herdr_session="continual-agent",
            worker_name="worker_5678",
            agent_kind="codex",
            session_source="foreign-source",
            session_agent="codex",
            session_ref_kind="id",
            session_value="codex-thread-1",
        )

    with pytest.raises(RegistryError, match="replace native Herdr session"):
        registry.bind_herdr_run(
            run["id"],
            herdr_session="continual-agent",
            worker_name="worker_5678",
            agent_kind="codex",
            session_source="herdr:codex",
            session_agent="codex",
            session_ref_kind="id",
            session_value="foreign-thread",
        )


def test_herdr_worker_cannot_be_bound_to_two_runs(registry):
    first_task = create_task(registry)
    first_run = registry.start_run(first_task["id"], agent_role="claude")
    registry.bind_herdr_run(
        first_run["id"],
        herdr_session="continual-agent",
        worker_name="worker_shared",
        agent_kind="claude",
    )
    second_task = registry.create_task(
        title="Second task",
        goal="Try a conflicting binding",
        success_criteria="The conflict is rejected",
    )
    second_run = registry.start_run(second_task["id"], agent_role="claude")

    with pytest.raises(RegistryError, match="already bound to another run"):
        registry.bind_herdr_run(
            second_run["id"],
            herdr_session="continual-agent",
            worker_name="worker_shared",
            agent_kind="claude",
        )


def test_finished_binding_releases_worker_name_for_a_later_run(registry):
    task = create_task(registry)
    first_run = registry.start_run(task["id"], agent_role="claude")
    registry.bind_herdr_run(
        first_run["id"],
        herdr_session="continual-agent",
        worker_name="worker_reuse",
        agent_kind="claude",
        session_source="herdr:claude",
        session_agent="claude",
        session_ref_kind="id",
        session_value="claude-session-1",
    )
    registry.finish_run(first_run["id"], outcome="failed", summary="Reviewer requested a retry")
    assert registry.get_run(first_run["id"])["herdr_binding"]["status"] == "stale"

    registry.transition_task(task["id"], "ready", actor="supervisor", reason="retry")
    second_run = registry.start_run(task["id"], agent_role="claude")
    second_binding = registry.bind_herdr_run(
        second_run["id"],
        herdr_session="continual-agent",
        worker_name="worker_reuse",
        agent_kind="claude",
        session_source="herdr:claude",
        session_agent="claude",
        session_ref_kind="id",
        session_value="claude-session-1",
    )

    assert second_binding["status"] == "live"
    assert second_binding["native_session"]["value"] == "claude-session-1"


def test_turn_requires_live_herdr_binding(registry):
    task = create_task(registry)
    run = registry.start_run(task["id"], agent_role="claude")

    with pytest.raises(RegistryError, match="requires a Herdr binding"):
        registry.start_turn(run["id"], purpose="task", prompt="hello")

    with pytest.raises(RegistryError, match="cannot manually set binding status to stale"):
        registry.bind_herdr_run(
            run["id"],
            herdr_session="continual-agent",
            worker_name="worker_9012",
            agent_kind="claude",
            status="stale",
        )

    registry.bind_herdr_run(
        run["id"],
        herdr_session="continual-agent",
        worker_name="worker_9012",
        agent_kind="claude",
        status="blocked",
    )
    with pytest.raises(RegistryError, match="requires a live Herdr binding"):
        registry.start_turn(run["id"], purpose="task", prompt="hello")


def test_turn_requires_one_open_turn_and_a_matching_result(registry, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    task = create_task(registry)
    run = registry.start_run(task["id"], agent_role="claude")
    registry.bind_herdr_run(
        run["id"],
        herdr_session="continual-agent",
        worker_name="worker_result",
        agent_kind="claude",
    )
    turn = registry.start_turn(run["id"], purpose="task", prompt="hello")
    assert turn["prompt"] == "hello"

    with pytest.raises(RegistryError, match="already has an open turn"):
        registry.start_turn(run["id"], purpose="correction", prompt="retry")
    with pytest.raises(RegistryError, match="result is unavailable"):
        registry.finish_turn(turn["id"], status="succeeded")

    write_turn_result(turn, turn_id="foreign-turn")
    with pytest.raises(RegistryError, match="ID does not match"):
        registry.finish_turn(turn["id"], status="succeeded")

    Path(turn["artifact_path"]).write_bytes(b"x" * (MAX_TURN_RESULT_BYTES + 1))
    with pytest.raises(RegistryError, match="exceeds"):
        registry.finish_turn(turn["id"], status="succeeded")

    bad_payload = f'```json\n{{"turn_id": "{turn["id"]}", "status": "succeeded"}}\n```'
    Path(turn["artifact_path"]).write_text(bad_payload)
    with pytest.raises(RegistryError, match="markdown code fence"):
        registry.finish_turn(turn["id"], status="succeeded")

    write_turn_result(
        turn,
        summary="Verified result",
        artifacts=[{"path": "result.md", "kind": "report"}],
    )
    finished = registry.finish_turn(turn["id"], status="succeeded")

    assert finished["summary"] == "Verified result"
    assert finished["prompt"] == "hello"
    assert finished["result"]["turn_id"] == turn["id"]
    assert finished["result"]["artifacts"][0]["path"] == "result.md"


def test_run_cannot_finish_with_open_turn(registry, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    task = create_task(registry)
    run = registry.start_run(task["id"], agent_role="claude")
    registry.bind_herdr_run(
        run["id"],
        herdr_session="continual-agent",
        worker_name="worker_open",
        agent_kind="claude",
    )
    turn = registry.start_turn(run["id"], purpose="task", prompt="hello")

    with pytest.raises(RegistryError, match="unfinished turn"):
        registry.finish_run(run["id"], outcome="succeeded", summary="too early")

    write_turn_result(turn, summary="done")
    registry.finish_turn(turn["id"], status="succeeded", summary="done")
    finished = registry.finish_run(run["id"], outcome="succeeded", summary="complete")
    assert finished["outcome"] == "succeeded"


def test_finish_run_with_turns_requires_at_least_one_succeeded_turn(
    registry, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    task = create_task(registry)
    run = registry.start_run(task["id"], agent_role="claude")
    registry.bind_herdr_run(
        run["id"],
        herdr_session="continual-agent",
        worker_name="worker_failed_turn",
        agent_kind="claude",
    )
    turn = registry.start_turn(run["id"], purpose="task", prompt="do work")
    registry.finish_turn(turn["id"], status="failed", summary="Worker crashed")

    with pytest.raises(RegistryError, match="requires at least one succeeded turn"):
        registry.finish_run(run["id"], outcome="succeeded", summary="claimed success anyway")

    finished = registry.finish_run(run["id"], outcome="failed", summary="acknowledged failure")
    assert finished["outcome"] == "failed"


def test_evaluation_requires_evaluating_state_and_latest_run(registry):
    task = create_task(registry)
    run = registry.start_run(task["id"], agent_role="worker")

    with pytest.raises(RegistryError, match="must be in evaluating state"):
        registry.add_evaluation(
            task["id"],
            run_id=run["id"],
            evaluator="reviewer",
            passed=True,
            evidence="premature eval",
        )

    registry.finish_run(run["id"], outcome="succeeded", summary="done")
    assert registry.get_task(task["id"])["state"] == "evaluating"

    with pytest.raises(RegistryError, match="must be independent"):
        registry.add_evaluation(
            task["id"],
            run_id=run["id"],
            evaluator="worker",
            passed=True,
            evidence="self eval",
        )

    evaluation = registry.add_evaluation(
        task["id"],
        run_id=run["id"],
        evaluator="reviewer",
        passed=True,
        evidence="verified by test",
    )
    assert evaluation["passed"] == 1
    assert registry.get_task(task["id"])["state"] == "succeeded"


def test_transition_task_preserves_next_action_and_clears_blocked_on(registry):
    task = registry.create_task(
        title="Action task",
        goal="Check state preservation",
        success_criteria="State preserved",
        next_action="inspect logs",
    )
    blocked = registry.transition_task(
        task["id"],
        "blocked",
        actor="supervisor",
        reason="waiting on key",
        blocked_on="API key",
    )
    assert blocked["next_action"] == "inspect logs"
    assert blocked["blocked_on"] == "API key"

    ready = registry.transition_task(
        task["id"],
        "ready",
        actor="supervisor",
        reason="key acquired",
    )
    assert ready["next_action"] == "inspect logs"
    assert ready["blocked_on"] is None


def test_initialize_upgrades_a_real_version_one_schema(tmp_path):
    database = tmp_path / "control.db"
    version_one = Path(__file__).parent / "fixtures" / "schema_v1.sql"
    with sqlite3.connect(database) as connection:
        connection.executescript(version_one.read_text())

    registry = Registry(database)
    registry.initialize()

    with registry._connect() as connection:
        assert connection.execute("SELECT version FROM schema_meta").fetchone()[0] == 4
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        turn_columns = {row[1] for row in connection.execute("PRAGMA table_info(run_turns)")}
    assert {"herdr_bindings", "run_turns"} <= tables
    assert "result_json" in turn_columns
    assert "prompt" in turn_columns


def test_version_two_migration_preserves_and_stales_finished_bindings(tmp_path):
    database = tmp_path / "control.db"
    version_one = Path(__file__).parent / "fixtures" / "schema_v1.sql"
    with sqlite3.connect(database) as connection:
        connection.executescript(version_one.read_text())
        Registry._migrate_v1_to_v2(connection)
        connection.execute("UPDATE schema_meta SET version = 2")
        connection.execute(
            """
            INSERT INTO tasks(
                id, title, goal, success_criteria, state, permissions_json,
                created_at, updated_at
            ) VALUES ('task_old', 'Old', 'Migrate', 'Preserved', 'failed', '{}', 'now', 'now')
            """
        )
        connection.execute(
            """
            INSERT INTO runs(
                id, task_id, agent_role, status, artifacts_json, retries,
                started_at, finished_at
            ) VALUES ('run_old', 'task_old', 'claude', 'finished', '[]', 0, 'now', 'now')
            """
        )
        connection.execute(
            """
            INSERT INTO herdr_bindings(
                run_id, herdr_session, worker_name, agent_kind,
                status, bound_at, reconciled_at
            ) VALUES ('run_old', 'continual-agent', 'worker_old', 'claude', 'live', 'now', 'now')
            """
        )

    registry = Registry(database)
    registry.initialize()

    migrated = registry.get_run("run_old")
    assert migrated["herdr_binding"]["status"] == "stale"
    with registry._connect() as connection:
        assert connection.execute("SELECT version FROM schema_meta").fetchone()[0] == 4
        indexes = {row[1] for row in connection.execute("PRAGMA index_list(herdr_bindings)")}
    assert "idx_active_herdr_worker" in indexes


def test_preference_proposes_memory_without_applying_it(registry):
    task = create_task(registry)
    registry.add_feedback(
        task["id"],
        kind="preference",
        recurrence_key="reports.explicit-limits",
        content="Separate evidence from inference",
    )

    created = registry.propose_promotions()

    assert [(item["target_layer"], item["status"]) for item in created] == [("memory", "proposed")]
    assert registry.propose_promotions() == []


def test_repeated_correction_requires_passing_evidence_for_skill(registry):
    first = create_task(registry, title="First")
    second = create_task(registry, title="Second")
    for task in (first, second):
        registry.add_feedback(
            task["id"],
            kind="correction",
            recurrence_key="closeout.require-evidence",
            content="Do not call work complete before checking evidence",
        )

    assert registry.propose_promotions() == []

    run = registry.start_run(second["id"], agent_role="worker")
    registry.finish_run(run["id"], outcome="succeeded", summary="Corrected workflow completed")
    evaluation = registry.add_evaluation(
        second["id"],
        run_id=run["id"],
        evaluator="reviewer",
        passed=True,
        score=1.0,
        evidence="The revised closeout checked its artifact",
    )
    created = registry.propose_promotions()

    assert created[0]["target_layer"] == "skill"
    assert created[0]["evidence"]["evaluation_ids"] == [evaluation["id"]]


def test_repeated_failure_proposes_deterministic_control(registry):
    task = create_task(registry)
    for content in ("Archived too early", "Archived before preserving context"):
        registry.add_feedback(
            task["id"],
            kind="failure",
            recurrence_key="lifecycle.archive-guard",
            content=content,
        )

    created = registry.propose_promotions()

    assert created[0]["target_layer"] == "control"
    assert "deterministic" in created[0]["rationale"]


def test_promotion_counts_matching_feedback_kind_only(registry):
    task = create_task(registry)
    registry.add_feedback(
        task["id"],
        kind="failure",
        recurrence_key="lifecycle.archive-guard",
        content="First failure",
    )
    registry.add_feedback(
        task["id"],
        kind="preference",
        recurrence_key="lifecycle.archive-guard",
        content="Irrelevant preference under same key",
    )
    registry.add_feedback(
        task["id"],
        kind="failure",
        recurrence_key="lifecycle.archive-guard",
        content="Second failure",
    )

    created = registry.propose_promotions()
    assert len(created) == 1
    assert created[0]["target_layer"] == "control"
    assert "Repeated failure appeared 2 times" in created[0]["rationale"]
    assert len(created[0]["evidence"]["feedback_ids"]) == 2


def test_rejected_promotion_can_be_reproposed(registry):
    task = create_task(registry)
    for content in ("Failure 1", "Failure 2"):
        registry.add_feedback(
            task["id"],
            kind="failure",
            recurrence_key="lifecycle.retry",
            content=content,
        )
    promotion = registry.propose_promotions()[0]
    registry.set_promotion_status(promotion["id"], "rejected")

    # Adding new feedback under the same key allows re-proposing
    registry.add_feedback(
        task["id"],
        kind="failure",
        recurrence_key="lifecycle.retry",
        content="Failure 3",
    )
    reproposed = registry.propose_promotions()
    assert len(reproposed) == 1
    assert reproposed[0]["id"] == promotion["id"]
    assert reproposed[0]["status"] == "proposed"
    assert "Repeated failure appeared 3 times" in reproposed[0]["rationale"]


def test_promotion_status_requires_explicit_order(registry):
    task = create_task(registry)
    registry.add_feedback(
        task["id"],
        kind="preference",
        recurrence_key="style.brief",
        content="Prefer concise status reports",
    )
    promotion = registry.propose_promotions()[0]

    with pytest.raises(RegistryError, match="proposed -> applied"):
        registry.set_promotion_status(promotion["id"], "applied")

    accepted = registry.set_promotion_status(promotion["id"], "accepted")
    assert accepted["status"] == "accepted"
    assert registry.set_promotion_status(promotion["id"], "applied")["status"] == "applied"
