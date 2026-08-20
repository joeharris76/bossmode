from __future__ import annotations

import pytest

from continual_agent.registry import Registry, RegistryError


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


def test_supervisor_selects_highest_priority_ready_task(registry):
    low = create_task(registry, title="Low", priority=1)
    high = create_task(registry, title="High", priority=10)

    tick = registry.supervisor_tick()

    assert tick["dispatch"]["id"] == high["id"]
    assert tick["dispatch"]["id"] != low["id"]


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
