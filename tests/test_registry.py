from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from threading import Barrier

import pytest

from bossmode.registry import MAX_TURN_RESULT_BYTES, Registry, RegistryError


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


def test_start_run_rolls_back_every_write_when_event_recording_fails(registry, monkeypatch):
    task = create_task(registry)

    def fail_event(*_args, **_kwargs):
        raise RuntimeError("event write failed")

    monkeypatch.setattr(registry, "_record_event", fail_event)
    with pytest.raises(RuntimeError, match="event write failed"):
        registry.start_run(task["id"], agent_role="worker")

    preserved = registry.get_task(task["id"])
    assert preserved["state"] == "ready"
    assert preserved["runs"] == []
    assert [event["event_type"] for event in preserved["events"]] == ["created"]


def test_finish_run_rolls_back_run_task_and_binding(registry, monkeypatch):
    task = create_task(registry)
    run = registry.start_run(task["id"], agent_role="worker")
    registry.bind_herdr_run(
        run["id"], herdr_session="bossmode", worker_name="worker_rollback", agent_kind="pi"
    )

    def fail_event(*_args, **_kwargs):
        raise RuntimeError("event write failed")

    monkeypatch.setattr(registry, "_record_event", fail_event)
    with pytest.raises(RuntimeError, match="event write failed"):
        registry.finish_run(run["id"], outcome="failed", summary="should roll back")

    assert registry.get_task(task["id"])["state"] == "running"
    preserved_run = registry.get_run(run["id"])
    assert preserved_run["status"] == "running"
    assert preserved_run["herdr_binding"]["status"] == "live"


def test_evaluation_rolls_back_record_and_task_state(registry, monkeypatch):
    task = create_task(registry)
    run = registry.start_run(task["id"], agent_role="worker")
    registry.finish_run(run["id"], outcome="succeeded", summary="done")

    def fail_event(*_args, **_kwargs):
        raise RuntimeError("event write failed")

    monkeypatch.setattr(registry, "_record_event", fail_event)
    with pytest.raises(RuntimeError, match="event write failed"):
        registry.add_evaluation(
            task["id"],
            run_id=run["id"],
            evaluator="reviewer",
            passed=True,
            evidence="should roll back",
        )

    preserved = registry.get_task(task["id"])
    assert preserved["state"] == "evaluating"
    assert preserved["evaluations"] == []


def test_concurrent_run_starts_allow_exactly_one_winner(tmp_path):
    database = tmp_path / "control.db"
    registry = Registry(database)
    task = create_task(registry)
    barrier = Barrier(2)

    def attempt(role: str):
        barrier.wait()
        try:
            return Registry(database).start_run(task["id"], agent_role=role)
        except RegistryError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(attempt, ("worker-a", "worker-b")))

    winners = [result for result in results if isinstance(result, dict)]
    losers = [result for result in results if isinstance(result, RegistryError)]
    assert len(winners) == 1
    assert len(losers) == 1
    persisted = registry.get_task(task["id"])
    assert persisted["state"] == "running"
    assert len(persisted["runs"]) == 1


def test_concurrent_fresh_initialization_is_singleton_and_error_free(tmp_path):
    for attempt_number in range(20):
        database = tmp_path / f"control-{attempt_number}.db"
        barrier = Barrier(8)

        def initialize(_: int, current_barrier=barrier, current_database=database):
            current_barrier.wait()
            try:
                Registry(current_database).initialize()
                return None
            except Exception as caught:
                return caught

        with ThreadPoolExecutor(max_workers=8) as executor:
            errors = [result for result in executor.map(initialize, range(8)) if result]

        assert errors == []
        registry = Registry(database)
        with closing(registry._connect()) as connection:
            assert connection.execute("SELECT COUNT(*) FROM schema_meta").fetchone()[0] == 1
            assert connection.execute("SELECT version FROM schema_meta").fetchone()[0] == 5
            assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_competing_cli_processes_start_exactly_one_run(tmp_path):
    database = tmp_path / "control.db"
    registry = Registry(database)
    task = create_task(registry)

    def attempt(role: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "bossmode.cli",
                "--db",
                str(database),
                "run",
                "start",
                task["id"],
                "--role",
                role,
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(attempt, ("process-a", "process-b")))

    assert sorted(result.returncode for result in results) == [0, 2]
    persisted = registry.get_task(task["id"])
    assert persisted["state"] == "running"
    assert len(persisted["runs"]) == 1


def test_concurrent_run_finishes_allow_exactly_one_winner(tmp_path):
    database = tmp_path / "control.db"
    registry = Registry(database)
    task = create_task(registry)
    run = registry.start_run(task["id"], agent_role="worker")
    barrier = Barrier(2)

    def attempt(summary: str):
        barrier.wait()
        try:
            return Registry(database).finish_run(run["id"], outcome="succeeded", summary=summary)
        except RegistryError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(attempt, ("finish-a", "finish-b")))

    assert sum(isinstance(result, dict) for result in results) == 1
    assert sum(isinstance(result, RegistryError) for result in results) == 1
    persisted = registry.get_task(task["id"])
    assert persisted["state"] == "evaluating"
    assert persisted["runs"][0]["status"] == "finished"
    assert sum(event["event_type"] == "run_finished" for event in persisted["events"]) == 1


def test_concurrent_evaluations_allow_exactly_one_winner(tmp_path):
    database = tmp_path / "control.db"
    registry = Registry(database)
    task = create_task(registry)
    run = registry.start_run(task["id"], agent_role="worker")
    registry.finish_run(run["id"], outcome="succeeded", summary="done")
    barrier = Barrier(2)

    def attempt(evaluator: str):
        barrier.wait()
        try:
            return Registry(database).add_evaluation(
                task["id"],
                run_id=run["id"],
                evaluator=evaluator,
                passed=True,
                evidence="concurrent review",
            )
        except RegistryError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(attempt, ("reviewer-a", "reviewer-b")))

    assert sum(isinstance(result, dict) for result in results) == 1
    assert sum(isinstance(result, RegistryError) for result in results) == 1
    persisted = registry.get_task(task["id"])
    assert persisted["state"] == "succeeded"
    assert len(persisted["evaluations"]) == 1
    assert sum(event["event_type"] == "evaluated" for event in persisted["events"]) == 1


def test_uncommitted_child_process_writes_are_rolled_back(tmp_path):
    database = tmp_path / "control.db"
    registry = Registry(database)
    task = create_task(registry)
    child = """
import os
import sys
from bossmode.registry import Registry

registry = Registry(sys.argv[1])
registry._record_event = lambda *_args, **_kwargs: os._exit(17)
registry.start_run(sys.argv[2], agent_role="crashing-worker")
"""

    completed = subprocess.run(
        [sys.executable, "-c", child, str(database), task["id"]],
        check=False,
    )

    assert completed.returncode == 17
    persisted = registry.get_task(task["id"])
    assert persisted["state"] == "ready"
    assert persisted["runs"] == []
    assert [event["event_type"] for event in persisted["events"]] == ["created"]
    with closing(registry._connect()) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


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
    for state in (
        "running",
        "evaluating",
        "waiting_user",
        "blocked",
        "succeeded",
        "failed",
        "archived",
    ):
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
    with closing(registry._connect()) as connection:
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
        herdr_session="bossmode",
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
        herdr_session="bossmode",
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
            herdr_session="bossmode",
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
            herdr_session="bossmode",
            worker_name="worker_5678",
            agent_kind="codex",
            session_source="herdr:codex",
            session_agent="codex",
            session_ref_kind="id",
            session_value="foreign-thread",
        )

    preserved = registry.get_run(run["id"])["herdr_binding"]
    assert preserved["herdr_session"] == "bossmode"
    assert preserved["worker_name"] == "worker_5678"
    assert preserved["native_session"] == {
        "source": "herdr:codex",
        "agent": "codex",
        "kind": "id",
        "value": "codex-thread-1",
    }


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"herdr_session": " "}, "session name is required"),
        ({"worker_name": "INVALID NAME"}, "invalid Herdr worker name"),
        ({"agent_kind": " "}, "agent kind is required"),
        ({"session_source": "herdr:pi"}, "must be supplied together"),
        (
            {
                "session_source": "herdr:pi",
                "session_agent": "pi",
                "session_ref_kind": "url",
                "session_value": "pi-1",
            },
            "invalid native session reference kind",
        ),
    ],
)
def test_herdr_binding_rejects_invalid_identity_inputs(registry, overrides, message):
    task = create_task(registry)
    run = registry.start_run(task["id"], agent_role="pi")
    arguments = {
        "herdr_session": "bossmode",
        "worker_name": "worker_valid",
        "agent_kind": "pi",
    }
    arguments.update(overrides)

    with pytest.raises(RegistryError, match=message):
        registry.bind_herdr_run(run["id"], **arguments)

    assert registry.get_run(run["id"])["herdr_binding"] is None


def test_herdr_binding_requires_an_existing_running_run(registry):
    with pytest.raises(RegistryError, match="run not found"):
        registry.bind_herdr_run(
            "missing", herdr_session="bossmode", worker_name="worker_missing", agent_kind="pi"
        )

    task = create_task(registry)
    run = registry.start_run(task["id"], agent_role="pi")
    registry.finish_run(run["id"], outcome="failed", summary="done")
    with pytest.raises(RegistryError, match="requires a running run"):
        registry.bind_herdr_run(
            run["id"],
            herdr_session="bossmode",
            worker_name="worker_finished",
            agent_kind="pi",
        )


def test_herdr_worker_cannot_be_bound_to_two_runs(registry):
    first_task = create_task(registry)
    first_run = registry.start_run(first_task["id"], agent_role="claude")
    registry.bind_herdr_run(
        first_run["id"],
        herdr_session="bossmode",
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
            herdr_session="bossmode",
            worker_name="worker_shared",
            agent_kind="claude",
        )


def test_finished_binding_releases_worker_name_for_a_later_run(registry):
    task = create_task(registry)
    first_run = registry.start_run(task["id"], agent_role="claude")
    registry.bind_herdr_run(
        first_run["id"],
        herdr_session="bossmode",
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
        herdr_session="bossmode",
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
            herdr_session="bossmode",
            worker_name="worker_9012",
            agent_kind="claude",
            status="stale",
        )

    registry.bind_herdr_run(
        run["id"],
        herdr_session="bossmode",
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
        herdr_session="bossmode",
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


@pytest.mark.parametrize("missing_field", ["turn_id", "status", "summary", "artifacts"])
def test_turn_result_requires_every_contract_field(registry, tmp_path, monkeypatch, missing_field):
    monkeypatch.chdir(tmp_path)
    task = create_task(registry)
    run = registry.start_run(task["id"], agent_role="pi")
    registry.bind_herdr_run(
        run["id"], herdr_session="bossmode", worker_name="worker_fields", agent_kind="pi"
    )
    turn = registry.start_turn(run["id"], purpose="task", prompt="validate fields")
    payload = {
        "turn_id": turn["id"],
        "status": "succeeded",
        "summary": "done",
        "artifacts": [],
    }
    payload.pop(missing_field)
    Path(turn["artifact_path"]).parent.mkdir(parents=True, exist_ok=True)
    Path(turn["artifact_path"]).write_text(json.dumps(payload))

    with pytest.raises(RegistryError, match=f"missing fields: {missing_field}"):
        registry.finish_turn(turn["id"], status="succeeded")

    preserved = registry.get_turn(turn["id"])
    assert preserved["status"] == "running"
    assert preserved["result"] is None


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("invalid_utf8", "not valid JSON"),
        ("invalid_json", "not valid JSON"),
        ("array", "JSON object"),
        ("wrong_status", "must have status succeeded"),
        ("blank_summary", "non-empty string"),
        ("non_string_summary", "non-empty string"),
        ("summary_mismatch", "does not match"),
        ("artifacts_object", "non-empty path and kind"),
        ("artifact_missing_kind", "non-empty path and kind"),
        ("artifact_blank_path", "non-empty path and kind"),
        ("artifact_non_string_kind", "non-empty path and kind"),
    ],
)
def test_turn_result_rejects_malformed_payloads(registry, tmp_path, monkeypatch, case, message):
    monkeypatch.chdir(tmp_path)
    task = create_task(registry)
    run = registry.start_run(task["id"], agent_role="pi")
    registry.bind_herdr_run(
        run["id"], herdr_session="bossmode", worker_name="worker_payload", agent_kind="pi"
    )
    turn = registry.start_turn(run["id"], purpose="task", prompt="validate payload")
    path = Path(turn["artifact_path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "turn_id": turn["id"],
        "status": "succeeded",
        "summary": "expected summary",
        "artifacts": [],
    }
    if case == "invalid_utf8":
        path.write_bytes(b"\xff")
    elif case == "invalid_json":
        path.write_text("{")
    elif case == "array":
        path.write_text("[]")
    else:
        if case == "wrong_status":
            payload["status"] = "failed"
        elif case == "blank_summary":
            payload["summary"] = " "
        elif case == "non_string_summary":
            payload["summary"] = 42
        elif case == "summary_mismatch":
            payload["summary"] = "different"
        elif case == "artifacts_object":
            payload["artifacts"] = {}
        elif case == "artifact_missing_kind":
            payload["artifacts"] = [{"path": "result.md"}]
        elif case == "artifact_blank_path":
            payload["artifacts"] = [{"path": "", "kind": "report"}]
        elif case == "artifact_non_string_kind":
            payload["artifacts"] = [{"path": "result.md", "kind": 1}]
        path.write_text(json.dumps(payload))

    with pytest.raises(RegistryError, match=message):
        registry.finish_turn(
            turn["id"],
            status="succeeded",
            summary="expected summary",
        )

    preserved = registry.get_turn(turn["id"])
    assert preserved["status"] == "running"
    assert preserved["result"] is None


def test_run_cannot_finish_with_open_turn(registry, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    task = create_task(registry)
    run = registry.start_run(task["id"], agent_role="claude")
    registry.bind_herdr_run(
        run["id"],
        herdr_session="bossmode",
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
        herdr_session="bossmode",
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

    first_evaluation = registry.add_evaluation(
        task["id"],
        run_id=run["id"],
        evaluator="reviewer",
        passed=False,
        evidence="retry required",
    )
    assert first_evaluation["passed"] == 0
    registry.transition_task(task["id"], "ready", actor="supervisor", reason="retry")
    retry = registry.start_run(task["id"], agent_role="worker")
    registry.finish_run(retry["id"], outcome="succeeded", summary="retry done")

    with pytest.raises(RegistryError, match=f"evaluating run: {retry['id']}"):
        registry.add_evaluation(
            task["id"],
            run_id=run["id"],
            evaluator="reviewer",
            passed=True,
            evidence="stale run",
        )

    evaluation = registry.add_evaluation(
        task["id"],
        run_id=retry["id"],
        evaluator="reviewer",
        passed=True,
        evidence="verified by test",
    )
    assert evaluation["passed"] == 1
    assert registry.get_task(task["id"])["state"] == "succeeded"


def test_evaluation_rejects_missing_foreign_and_unfinished_runs(registry):
    first = create_task(registry, title="First")
    first_run = registry.start_run(first["id"], agent_role="worker")
    registry.finish_run(first_run["id"], outcome="succeeded", summary="done")

    with pytest.raises(RegistryError, match="run not found"):
        registry.add_evaluation(
            first["id"],
            run_id="missing",
            evaluator="reviewer",
            passed=True,
            evidence="missing",
        )

    second = create_task(registry, title="Second")
    second_run = registry.start_run(second["id"], agent_role="worker")
    registry.finish_run(second_run["id"], outcome="succeeded", summary="done")
    with pytest.raises(RegistryError, match="does not belong to task"):
        registry.add_evaluation(
            first["id"],
            run_id=second_run["id"],
            evaluator="reviewer",
            passed=True,
            evidence="foreign",
        )

    third = create_task(registry, title="Third")
    third_run = registry.start_run(third["id"], agent_role="worker")
    with closing(registry._connect()) as connection:
        connection.execute("UPDATE tasks SET state = 'evaluating' WHERE id = ?", (third["id"],))
    with pytest.raises(RegistryError, match="requires a finished run"):
        registry.add_evaluation(
            third["id"],
            run_id=third_run["id"],
            evaluator="reviewer",
            passed=True,
            evidence="unfinished",
        )


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
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.executescript(version_one.read_text())

    registry = Registry(database)
    registry.initialize()

    with closing(registry._connect()) as connection:
        assert connection.execute("SELECT version FROM schema_meta").fetchone()[0] == 5
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        turn_columns = {row[1] for row in connection.execute("PRAGMA table_info(run_turns)")}
    assert {"herdr_bindings", "run_turns", "maintenance_runs"} <= tables
    assert "result_json" in turn_columns
    assert "prompt" in turn_columns


def test_version_two_migration_preserves_and_stales_finished_bindings(tmp_path):
    database = tmp_path / "control.db"
    version_two = Path(__file__).parent / "fixtures" / "schema_v2.sql"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.executescript(version_two.read_text())

    registry = Registry(database)
    registry.initialize()

    migrated = registry.get_run("run_v2")
    assert migrated["herdr_binding"]["status"] == "stale"
    assert migrated["turns"][0]["summary"] == "historical turn"
    assert migrated["turns"][0]["result"] is None
    with closing(registry._connect()) as connection:
        assert connection.execute("SELECT version FROM schema_meta").fetchone()[0] == 5
        indexes = {row[1] for row in connection.execute("PRAGMA index_list(herdr_bindings)")}
        assert connection.execute("SELECT COUNT(*) FROM evaluations").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM feedback").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM promotions").fetchone()[0] == 1
    assert "idx_active_herdr_worker" in indexes


def test_version_three_migration_preserves_turn_data(tmp_path):
    database = tmp_path / "control.db"
    version_three = Path(__file__).parent / "fixtures" / "schema_v3.sql"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.executescript(version_three.read_text())

    registry = Registry(database)
    registry.initialize()

    with closing(registry._connect()) as connection:
        turn = connection.execute("SELECT * FROM run_turns WHERE id = 'turn_v3'").fetchone()
        assert turn["summary"] == "preserved"
        assert json.loads(turn["result_json"])["turn_id"] == "turn_v3"
        assert turn["prompt"] == ""
        assert connection.execute("SELECT version FROM schema_meta").fetchone()[0] == 5
        assert connection.execute("SELECT COUNT(*) FROM evaluations").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM feedback").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM promotions").fetchone()[0] == 1


def test_version_four_migration_preserves_records_and_is_idempotent(tmp_path):
    database = tmp_path / "control.db"
    version_four = Path(__file__).parent / "fixtures" / "schema_v4.sql"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.executescript(version_four.read_text())

    registry = Registry(database)
    registry.initialize()
    registry.initialize()

    with closing(registry._connect()) as connection:
        assert connection.execute("SELECT version FROM schema_meta").fetchone()[0] == 5
        turn = connection.execute("SELECT * FROM run_turns WHERE id = 'turn_v4'").fetchone()
        assert turn["prompt"] == "historical prompt"
        assert json.loads(turn["result_json"])["turn_id"] == "turn_v4"
        assert connection.execute("SELECT COUNT(*) FROM evaluations").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM feedback").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM promotions").fetchone()[0] == 1
        assert (
            connection.execute(
                """
                SELECT COUNT(*) FROM sqlite_master
                WHERE type = 'table' AND name = 'maintenance_runs'
                """
            ).fetchone()[0]
            == 1
        )


def test_failed_migration_rolls_back_schema_and_version(tmp_path, monkeypatch):
    database = tmp_path / "control.db"
    version_four = Path(__file__).parent / "fixtures" / "schema_v4.sql"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.executescript(version_four.read_text())

    registry = Registry(database)

    def fail_migration(connection):
        connection.execute("CREATE TABLE migration_probe(value TEXT)")
        connection.execute("INVALID SQL")

    monkeypatch.setattr(registry, "_migrate_v4_to_v5", fail_migration)
    with pytest.raises(RegistryError, match="migration 4 -> 5 failed"):
        registry.initialize()

    with closing(registry._connect()) as connection:
        assert connection.execute("SELECT version FROM schema_meta").fetchone()[0] == 4
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE name = 'migration_probe'"
            ).fetchone()[0]
            == 0
        )


@pytest.mark.parametrize(
    ("version", "message"),
    [
        (0, "no registry migration from schema 0"),
        (999, "newer than supported"),
    ],
)
def test_initialize_rejects_unsupported_schema_versions(tmp_path, version, message):
    database = tmp_path / "control.db"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("CREATE TABLE schema_meta(version INTEGER NOT NULL)")
        connection.execute("INSERT INTO schema_meta(version) VALUES (?)", (version,))

    with pytest.raises(RegistryError, match=message):
        Registry(database).initialize()


def test_initialize_rejects_missing_schema_version(tmp_path):
    database = tmp_path / "control.db"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("CREATE TABLE schema_meta(version INTEGER NOT NULL)")

    with pytest.raises(RegistryError, match="schema version is missing"):
        Registry(database).initialize()


def test_initialize_rejects_duplicate_schema_version_rows(tmp_path):
    database = tmp_path / "control.db"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("CREATE TABLE schema_meta(version INTEGER NOT NULL)")
        connection.executemany("INSERT INTO schema_meta(version) VALUES (?)", [(4,), (4,)])

    with pytest.raises(RegistryError, match="exactly one row"):
        Registry(database).initialize()


def test_initialize_rejects_corrupt_database(tmp_path):
    database = tmp_path / "control.db"
    database.write_bytes(b"not a sqlite database")

    with pytest.raises(sqlite3.DatabaseError):
        Registry(database).initialize()


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


@pytest.mark.parametrize(
    "agent_kind,session_source",
    [
        ("pi", "herdr:pi"),
        ("codex", "herdr:codex"),
        ("claude", "herdr:claude"),
        ("agy", "herdr:agy"),
        ("grok", "herdr:grok"),
        ("muse", "herdr:muse"),
    ],
)
def test_all_supported_agents_can_bind_and_execute_turns(
    registry, tmp_path, monkeypatch, agent_kind, session_source
):
    monkeypatch.chdir(tmp_path)
    task = create_task(registry, title=f"Task for {agent_kind}")
    run = registry.start_run(task["id"], agent_role=agent_kind, model=f"{agent_kind}-model")
    binding = registry.bind_herdr_run(
        run["id"],
        herdr_session="bossmode",
        worker_name=f"worker_{agent_kind}",
        agent_kind=agent_kind,
        session_source=session_source,
        session_agent=agent_kind,
        session_ref_kind="id",
        session_value=f"{agent_kind}-sess-100",
    )
    assert binding["agent_kind"] == agent_kind
    assert binding["native_session"]["source"] == session_source

    turn = registry.start_turn(run["id"], purpose="task", prompt=f"Execute for {agent_kind}")
    write_turn_result(
        turn,
        summary=f"Completed by {agent_kind}",
        artifacts=[{"path": f"out_{agent_kind}.md", "kind": "report"}],
    )
    finished_turn = registry.finish_turn(
        turn["id"],
        status="succeeded",
        summary=f"Completed by {agent_kind}",
        lifecycle_evidence="done",
    )
    assert finished_turn["status"] == "succeeded"

    finished_run = registry.finish_run(
        run["id"],
        outcome="succeeded",
        summary=f"Run finished for {agent_kind}",
        artifacts=[{"path": f"out_{agent_kind}.md", "kind": "report"}],
    )
    assert finished_run["status"] == "finished"


@pytest.mark.parametrize(
    "coordinator_role,worker_role,reviewer_role",
    [
        ("agy", "claude", "codex"),
        ("codex", "pi", "grok"),
        ("claude", "muse", "agy"),
        ("pi", "agy", "muse"),
        ("grok", "codex", "claude"),
        ("muse", "grok", "pi"),
    ],
)
def test_any_agent_can_coordinate_and_review(
    registry, coordinator_role, worker_role, reviewer_role
):
    task = registry.create_task(
        title=f"Coordinated by {coordinator_role}",
        goal="Demonstrate coordinator flexibility",
        success_criteria="Evaluated by independent reviewer",
        state="backlog",
    )
    registry.transition_task(
        task["id"],
        "ready",
        actor=coordinator_role,
        reason=f"Prepared by {coordinator_role} supervisor",
    )

    tick = registry.supervisor_tick()
    assert tick["dispatch"]["id"] == task["id"]

    run = registry.start_run(task["id"], agent_role=worker_role)
    registry.finish_run(
        run["id"],
        outcome="succeeded",
        summary=f"Worker {worker_role} completed task",
    )

    evaluation = registry.add_evaluation(
        task["id"],
        run_id=run["id"],
        evaluator=reviewer_role,
        passed=True,
        score=0.95,
        evidence=f"Checked by {reviewer_role}",
    )
    assert evaluation["passed"] == 1
    assert registry.get_task(task["id"])["state"] == "succeeded"
