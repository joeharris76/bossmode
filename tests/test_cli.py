from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from bossmode import registry as registry_module
from bossmode.cli import main


def test_cli_smoke(tmp_path, capsys):
    database = tmp_path / "control.db"

    # 1. Naked bossmode invocation on fresh DB auto-initializes and returns clean state
    assert main(["--db", str(database)]) == 0
    state = json.loads(capsys.readouterr().out)
    assert state["next_task"] is None
    assert state["running"] == []
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
    assert reconciled["next_task"]["id"] == task["id"]


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


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (
            [
                "task",
                "create",
                "--title",
                "Invalid JSON",
                "--goal",
                "Reject malformed input",
                "--success-criteria",
                "Structured error",
                "--permissions-json",
                "{",
            ],
            "invalid JSON",
        ),
        (
            [
                "run",
                "finish",
                "missing",
                "--outcome",
                "failed",
                "--summary",
                "invalid artifacts",
                "--artifacts-json",
                "{}",
            ],
            "expected JSON list",
        ),
    ],
)
def test_cli_rejects_invalid_json_boundaries(tmp_path, capsys, arguments, message):
    assert main(["--db", str(tmp_path / "control.db"), *arguments]) == 2
    assert message in json.loads(capsys.readouterr().err)["error"]


def test_cli_returns_structured_error_when_database_is_locked(tmp_path, capsys, monkeypatch):
    database = tmp_path / "control.db"
    assert main(["--db", str(database)]) == 0
    capsys.readouterr()
    lock = sqlite3.connect(database, isolation_level=None)
    lock.execute("BEGIN IMMEDIATE")
    monkeypatch.setattr(registry_module, "SQLITE_BUSY_TIMEOUT_MS", 25)
    started = time.perf_counter()
    try:
        exit_code = main(["--db", str(database)])
    finally:
        lock.rollback()
        lock.close()

    assert time.perf_counter() - started < 1
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
                "--agent-kind",
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
                "outcome": "succeeded",
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
                "--outcome",
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
                "--category",
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
                "--category",
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
                "--agent-kind",
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
                "--agent-kind",
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


def test_cli_maintenance_subcommand(tmp_path, capsys):
    database = tmp_path / "control.db"
    assert main(["--db", str(database), "maintenance"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["database"]["integrity"] == "ok"
    assert report["health"]["status"] == "healthy"


def test_cli_schedule_subcommands_require_operational_authority(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["schedule", "status"]) == 2
    error = json.loads(capsys.readouterr().err)
    assert "operational registry authority" in error["error"]


def test_cli_default_and_reconcile_are_the_only_reconciliation_spellings(tmp_path, capsys):
    """The v0.1.0 surface has exactly two spellings and no aliases.

    `tick`, the `supervisor` group, and `next` were removed; this pins both the
    survivors' identical behaviour and the removals.
    """
    database = tmp_path / "control.db"

    assert main(["--db", str(database)]) == 0
    default_state = json.loads(capsys.readouterr().out)
    assert main(["--db", str(database), "reconcile"]) == 0
    explicit_state = json.loads(capsys.readouterr().out)
    assert default_state == explicit_state
    assert set(default_state) == {
        "next_task",
        "running",
        "evaluating",
        "waiting_user",
        "blocked",
        "new_promotion_proposals",
        "promotion_proposals",
    }


def test_cli_task_transition_rejects_unreachable_states(tmp_path, capsys):
    """Only reachable targets are offered; lifecycle commands own the rest."""
    database = tmp_path / "control.db"
    assert (
        main(
            [
                "--db",
                str(database),
                "task",
                "create",
                "--title",
                "T",
                "--goal",
                "G",
                "--success-criteria",
                "S",
            ]
        )
        == 0
    )
    task = json.loads(capsys.readouterr().out)

    for unreachable in ("running", "evaluating", "succeeded", "failed", "backlog", "waiting_user"):
        with pytest.raises(SystemExit) as exit_info:
            main(
                [
                    "--db",
                    str(database),
                    "task",
                    "transition",
                    task["id"],
                    unreachable,
                    "--actor",
                    "supervisor",
                    "--reason",
                    "r",
                ]
            )
        assert exit_info.value.code == 2

    assert (
        main(
            [
                "--db",
                str(database),
                "task",
                "transition",
                task["id"],
                "blocked",
                "--actor",
                "supervisor",
                "--reason",
                "needs a decision",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["state"] == "blocked"


def test_cli_install_skill_rejects_a_registry_path(tmp_path, capsys):
    """`install-skill` touches no registry, so it refuses --db instead of ignoring it."""
    assert main(["--db", str(tmp_path / "control.db"), "install-skill"]) == 2
    assert "does not use a registry" in json.loads(capsys.readouterr().err)["error"]


def test_cli_herdr_show_returns_the_binding(tmp_path, capsys, monkeypatch):
    database = tmp_path / "control.db"
    monkeypatch.chdir(tmp_path)
    run = _bound_run(database, capsys)

    assert main(["--db", str(database), "herdr", "show", run["id"]]) == 0
    binding = json.loads(capsys.readouterr().out)
    assert binding["worker_name"] == "worker_show"
    assert binding["status"] == "live"


def test_cli_promotion_apply_records_verified_implementation(tmp_path, capsys):
    database = tmp_path / "control.db"
    _task_with_repeated_failures(database, capsys)

    assert main(["--db", str(database), "promotion", "propose"]) == 0
    capsys.readouterr()
    assert main(["--db", str(database), "promotion", "list", "--status", "proposed"]) == 0
    proposals = json.loads(capsys.readouterr().out)
    assert proposals

    promotion_id = proposals[0]["id"]
    assert main(["--db", str(database), "promotion", "accept", promotion_id]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "accepted"
    assert main(["--db", str(database), "promotion", "apply", promotion_id]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "applied"


def test_cli_promotion_reject_closes_a_proposal(tmp_path, capsys):
    database = tmp_path / "control.db"
    _task_with_repeated_failures(database, capsys)
    assert main(["--db", str(database), "promotion", "propose"]) == 0
    proposals = json.loads(capsys.readouterr().out)
    assert proposals

    promotion_id = proposals[0]["id"]
    assert main(["--db", str(database), "promotion", "reject", promotion_id]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "rejected"


def _task_with_repeated_failures(database: Path, capsys) -> dict:
    assert (
        main(
            [
                "--db",
                str(database),
                "task",
                "create",
                "--title",
                "T",
                "--goal",
                "G",
                "--success-criteria",
                "S",
            ]
        )
        == 0
    )
    task = json.loads(capsys.readouterr().out)
    for _ in range(2):
        assert (
            main(
                [
                    "--db",
                    str(database),
                    "feedback",
                    task["id"],
                    "--category",
                    "failure",
                    "--key",
                    "api.retry",
                    "--content",
                    "retries are unbounded",
                ]
            )
            == 0
        )
        capsys.readouterr()
    return task


def _bound_run(database: Path, capsys) -> dict:
    assert (
        main(
            [
                "--db",
                str(database),
                "task",
                "create",
                "--title",
                "T",
                "--goal",
                "G",
                "--success-criteria",
                "S",
            ]
        )
        == 0
    )
    task = json.loads(capsys.readouterr().out)
    assert main(["--db", str(database), "run", "start", task["id"], "--role", "worker"]) == 0
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
                "worker_show",
                "--agent-kind",
                "claude",
            ]
        )
        == 0
    )
    capsys.readouterr()
    return run


@pytest.mark.parametrize(
    "removed",
    [
        # Retired top-level spellings.
        ["tick"],
        ["supervisor"],
        ["supervisor", "tick"],
        ["supervisor", "reconcile"],
        ["supervisor", "next"],
        ["next"],
        ["init"],
        ["registry", "init"],
        ["registry", "open"],
        # Retired subcommands.
        ["task", "add", "--title", "T", "--goal", "G", "--success-criteria", "S"],
        ["promotion", "set", "promo_1", "accepted"],
        # Retired flags, replaced without aliases.
        ["turn", "finish", "turn_1", "--status", "succeeded"],
        ["herdr", "bind", "run_1", "--herdr-session", "s", "--worker", "w", "--kind", "claude"],
        ["feedback", "task_1", "--kind", "failure", "--key", "k", "--content", "c"],
    ],
)
def test_cli_rejects_every_retired_spelling(tmp_path, removed):
    """v0.1.0 ships no compatibility aliases; each retired name must fail loudly."""
    database = tmp_path / "control.db"
    with pytest.raises(SystemExit) as exit_info:
        main(["--db", str(database), *removed])
    assert exit_info.value.code == 2


def test_cli_task_transition_backlog_to_blocked(tmp_path, capsys):
    database = tmp_path / "control.db"
    assert (
        main(
            [
                "--db",
                str(database),
                "task",
                "create",
                "--title",
                "Backlog Task",
                "--goal",
                "Wait for upstream",
                "--success-criteria",
                "Unblocked",
                "--state",
                "backlog",
            ]
        )
        == 0
    )
    task = json.loads(capsys.readouterr().out)
    assert task["state"] == "backlog"

    assert (
        main(
            [
                "--db",
                str(database),
                "task",
                "transition",
                task["id"],
                "blocked",
                "--actor",
                "supervisor",
                "--reason",
                "Waiting for upstream merged contracts",
                "--blocked-on",
                "bossmode-upstream-pkg",
            ]
        )
        == 0
    )
    transitioned = json.loads(capsys.readouterr().out)
    assert transitioned["state"] == "blocked"
    assert transitioned["blocked_on"] == "bossmode-upstream-pkg"


def test_cli_run_bind_and_reasoning_effort_source(tmp_path, capsys):
    database = tmp_path / "control.db"
    assert (
        main(
            [
                "--db",
                str(database),
                "task",
                "create",
                "--title",
                "Task 1",
                "--goal",
                "Goal 1",
                "--success-criteria",
                "Pass",
            ]
        )
        == 0
    )
    task = json.loads(capsys.readouterr().out)

    # 1. Start run without thread-id
    assert (
        main(
            [
                "--db",
                str(database),
                "run",
                "start",
                task["id"],
                "--role",
                "worker",
            ]
        )
        == 0
    )
    run = json.loads(capsys.readouterr().out)
    assert run["thread_id"] is None

    # 2. Bind thread-id and effort metadata
    assert (
        main(
            [
                "--db",
                str(database),
                "run",
                "bind",
                run["id"],
                "--thread-id",
                "native_thread_xyz",
                "--model",
                "claude-sonnet-5",
                "--reasoning-effort",
                "high",
                "--reasoning-effort-source",
                "observed",
            ]
        )
        == 0
    )
    bound_run = json.loads(capsys.readouterr().out)
    assert bound_run["thread_id"] == "native_thread_xyz"
    assert bound_run["model"] == "claude-sonnet-5"
    assert bound_run["reasoning_effort"] == "high"
    assert bound_run["reasoning_effort_source"] == "observed"


def test_cli_run_finish_cancelled(tmp_path, capsys):
    database = tmp_path / "control.db"
    assert (
        main(
            [
                "--db",
                str(database),
                "task",
                "create",
                "--title",
                "Cancel Task",
                "--goal",
                "Goal",
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
                "run",
                "start",
                task["id"],
                "--role",
                "worker",
                "--thread-id",
                "th_1",
            ]
        )
        == 0
    )
    run = json.loads(capsys.readouterr().out)

    # Finish as cancelled
    assert (
        main(
            [
                "--db",
                str(database),
                "run",
                "finish",
                run["id"],
                "--outcome",
                "cancelled",
                "--summary",
                "User requested model change",
            ]
        )
        == 0
    )
    finished = json.loads(capsys.readouterr().out)
    assert finished["outcome"] == "cancelled"

    # Task is back in ready
    assert main(["--db", str(database), "task", "show", task["id"]]) == 0
    show_task = json.loads(capsys.readouterr().out)
    assert show_task["state"] == "ready"


def test_cli_errors_write_only_to_stderr_and_exit_two(tmp_path, capsys):
    database = tmp_path / "control.db"

    # 1. Non-existent task transition
    exit_code = main(
        [
            "--db",
            str(database),
            "task",
            "transition",
            "task_missing",
            "ready",
            "--actor",
            "supervisor",
            "--reason",
            "test",
        ]
    )
    assert exit_code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    err_json = json.loads(captured.err)
    assert err_json == {"error": "task not found: task_missing"}

    # 2. Invalid reasoning effort source choice
    with pytest.raises(SystemExit) as exc_info:
        main(
            [
                "--db",
                str(database),
                "run",
                "start",
                "task_1",
                "--role",
                "worker",
                "--reasoning-effort-source",
                "bad_source",
            ]
        )
    assert exc_info.value.code == 2


def test_cli_feedback_with_observation_category(tmp_path, capsys):
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
                "Test",
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
                "--category",
                "observation",
                "--key",
                "worktree.artifact-landing",
                "--content",
                "Copy artifacts from isolated worktree before completing run",
            ]
        )
        == 0
    )
    fb = json.loads(capsys.readouterr().out)
    assert fb["category"] == "observation"
    assert fb["recurrence_key"] == "worktree.artifact-landing"
