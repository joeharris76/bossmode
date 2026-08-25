from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from bossmode import registry as registry_module
from bossmode.cli import (
    _approved_batch_workers,
    _approved_writer,
    _parser,
    _protected_branches,
    _run_artifacts,
    main,
)


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


def test_cli_schedule_subcommands(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["schedule", "status"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert "status" in status
    assert "platform" in status


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
        # Retired subcommands.
        ["task", "add", "--title", "T", "--goal", "G", "--success-criteria", "S"],
        ["promotion", "set", "promo_1", "accepted"],
        ["run", "manager-start"],
        ["run", "worker-start"],
        ["run", "reviewer-start"],
        ["run", "reconcile-accepted-head"],
        ["run", "accepted-head-reconcile"],
        ["run", "reconcile-head"],
        ["resource", "reconcile"],
        ["status", "executive"],
        [
            "dispatch",
            "batch",
            "root-task",
            "--managers-json",
            "[]",
            "--workers-json",
            "[]",
        ],
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


def test_cli_evaluate_reviewed_head_parser_and_contract_paths(tmp_path, capsys, monkeypatch):
    reviewed_head = "a57b1d4cb8f18432dfb1d2f7f64be5b19c20ff5d"
    captured = []

    def fake_add_evaluation(self, task_id, **kwargs):
        captured.append((task_id, kwargs))
        if kwargs["reviewed_head_sha"] is None:
            raise registry_module.RegistryError("team evaluation requires an exact reviewed head")
        return {"id": "eval-reviewed-head", "passed": int(kwargs["passed"]), **kwargs}

    monkeypatch.setattr(registry_module.Registry, "add_evaluation", fake_add_evaluation)
    parsed = _parser().parse_args(
        [
            "evaluate",
            "task-team",
            "--run-id",
            "run-worker",
            "--evaluator",
            "reviewer",
            "--passed",
            "--evidence",
            "exact head checked",
            "--reviewed-head-sha",
            reviewed_head,
        ]
    )
    assert parsed.reviewed_head_sha == reviewed_head

    assert (
        main(
            [
                "--db",
                str(tmp_path / "control.db"),
                "evaluate",
                "task-team",
                "--run-id",
                "run-worker",
                "--evaluator",
                "reviewer",
                "--passed",
                "--evidence",
                "exact head checked",
                "--reviewed-head-sha",
                reviewed_head,
            ]
        )
        == 0
    )
    success = json.loads(capsys.readouterr().out)
    assert success["reviewed_head_sha"] == reviewed_head
    assert captured[-1][1]["reviewed_head_sha"] == reviewed_head

    assert (
        main(
            [
                "--db",
                str(tmp_path / "control.db"),
                "evaluate",
                "task-team",
                "--run-id",
                "run-worker",
                "--evaluator",
                "reviewer",
                "--failed",
                "--evidence",
                "missing exact head",
            ]
        )
        == 2
    )
    failure = json.loads(capsys.readouterr().err)
    assert "exact reviewed head" in failure["error"]
    assert captured[-1][1]["reviewed_head_sha"] is None


def test_cli_run_finish_rejects_accepted_head_without_writer(tmp_path, capsys):
    database = tmp_path / "control.db"
    assert (
        main(
            [
                "--db",
                str(database),
                "task",
                "create",
                "--title",
                "Accepted head",
                "--goal",
                "Record the accepted Git head",
                "--success-criteria",
                "The accepted head is durable",
            ]
        )
        == 0
    )
    task = json.loads(capsys.readouterr().out)
    assert main(["--db", str(database), "run", "start", task["id"], "--role", "worker"]) == 0
    run = json.loads(capsys.readouterr().out)

    accepted_head = "a57b1d4cb8f18432dfb1d2f7f64be5b19c20ff5d"
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
                "Accepted exact head",
                "--accepted-head-sha",
                accepted_head,
            ]
        )
        == 2
    )
    assert "writer identity" in json.loads(capsys.readouterr().err)["error"]


def test_cli_task_create_accepts_first_class_approved_base(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    database = tmp_path / "control.db"
    approved_base = "a57b1d4cb8f18432dfb1d2f7f64be5b19c20ff5d"
    captured = {}

    def fake_create_task(self, **kwargs):
        captured.update(kwargs)
        return {"id": "task-approved-base"}

    monkeypatch.setattr(registry_module.Registry, "create_task", fake_create_task)
    assert (
        main(
            [
                "--db",
                str(database),
                "task",
                "create",
                "--title",
                "Base",
                "--goal",
                "Goal",
                "--success-criteria",
                "Criteria",
                "--approved-base-sha",
                approved_base,
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["id"] == "task-approved-base"
    assert captured["approved_base_sha"] == approved_base


def test_cli_record_accepted_head_forwards_repository(tmp_path, capsys, monkeypatch):
    captured = {}

    def fake_record(self, run_id, **kwargs):
        captured["run_id"] = run_id
        captured.update(kwargs)
        return {"id": run_id, "writer_identity": {"accepted_head_sha": kwargs["accepted_head_sha"]}}

    monkeypatch.setattr(registry_module.Registry, "record_accepted_head", fake_record)
    accepted_head = "a57b1d4cb8f18432dfb1d2f7f64be5b19c20ff5d"
    assert (
        main(
            [
                "--db",
                str(tmp_path / "control.db"),
                "run",
                "record-head",
                "run-worker",
                "--repository-path",
                str(tmp_path / "repository"),
                "--accepted-head-sha",
                accepted_head,
                "--evidence",
                "live Git verified",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["id"] == "run-worker"
    assert captured == {
        "run_id": "run-worker",
        "repository_path": str(tmp_path / "repository"),
        "accepted_head_sha": accepted_head,
        "evidence": "live Git verified",
    }


@pytest.mark.parametrize(
    "retired_flag",
    [
        ["--approved-repository", "/tmp/repository"],
        ["--base-sha", "a57b1d4cb8f18432dfb1d2f7f64be5b19c20ff5d"],
    ],
)
def test_cli_start_worker_rejects_retired_approval_flags(retired_flag):
    with pytest.raises(SystemExit) as error:
        _parser().parse_args(
            [
                "run",
                "start-worker",
                "task-worker",
                "--manager-run-id",
                "run-manager",
                "--identity-json",
                '{"source":"native","value":"worker"}',
                "--writer-json",
                '{"branch_name":"feature/worker","base_sha":"a57b1d4cb8f18432dfb1d2f7f64be5b19c20ff5d","worktree_path":"/tmp/worktree","worktree_id":"wt-worker"}',
                *retired_flag,
            ]
        )
    assert error.value.code == 2


def test_cli_record_accepted_head_requires_repository_path(tmp_path, capsys):
    with pytest.raises(SystemExit) as error:
        main(
            [
                "--db",
                str(tmp_path / "control.db"),
                "run",
                "record-head",
                "run-worker",
                "--accepted-head-sha",
                "a57b1d4cb8f18432dfb1d2f7f64be5b19c20ff5d",
                "--evidence",
                "live Git verified",
            ]
        )
    assert error.value.code == 2
    assert "--repository-path" in capsys.readouterr().err


def test_cli_worker_start_forwards_approval_inputs(tmp_path, capsys, monkeypatch):
    captured = {}

    def fake_start_worker_run(self, task_id, **kwargs):
        captured["task_id"] = task_id
        captured.update(kwargs)
        return {"id": "run_worker", "status": "running"}

    monkeypatch.setattr(registry_module.Registry, "start_worker_run", fake_start_worker_run)
    approved_repository = tmp_path / "repository"
    approved_base = "a57b1d4cb8f18432dfb1d2f7f64be5b19c20ff5d"
    writer = {
        "branch_name": "feature/worker",
        "base_sha": approved_base,
        "worktree_path": str(tmp_path / "worktree"),
        "worktree_id": "wt-worker",
    }

    assert (
        main(
            [
                "--db",
                str(tmp_path / "control.db"),
                "run",
                "start-worker",
                "task-1",
                "--manager-run-id",
                "run-manager",
                "--identity-json",
                '{"source":"native","value":"worker"}',
                "--writer-json",
                json.dumps(writer),
                "--repository-path",
                str(approved_repository),
                "--approved-repository-path",
                str(approved_repository),
                "--approved-base-sha",
                approved_base,
                "--protected-branches-json",
                '["main", "release"]',
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["id"] == "run_worker"
    assert captured["repository_path"] == str(approved_repository)
    assert captured["writer"]["approved_base_sha"] == approved_base
    assert captured["writer"]["protected_branches"] == ["main", "release"]


def test_cli_rejects_unapproved_writer_inputs(tmp_path, capsys, monkeypatch):
    called = False

    def fake_start_worker_run(self, task_id, **kwargs):
        nonlocal called
        called = True
        return {"id": "unexpected"}

    monkeypatch.setattr(registry_module.Registry, "start_worker_run", fake_start_worker_run)
    writer = {
        "branch_name": "release",
        "base_sha": "a57b1d4cb8f18432dfb1d2f7f64be5b19c20ff5d",
        "worktree_path": str(tmp_path / "worktree"),
        "worktree_id": "wt-worker",
    }
    assert (
        main(
            [
                "--db",
                str(tmp_path / "control.db"),
                "run",
                "start-worker",
                "task-1",
                "--manager-run-id",
                "run-manager",
                "--identity-json",
                '{"source":"native","value":"worker"}',
                "--writer-json",
                json.dumps(writer),
                "--approved-base-sha",
                "b" * 40,
                "--protected-branches-json",
                '["release"]',
            ]
        )
        == 2
    )
    assert called is False
    assert "does not match the approved base SHA" in json.loads(capsys.readouterr().err)["error"]


def test_cli_approval_helpers_fail_closed(tmp_path):
    writer = {
        "branch_name": "feature/worker",
        "base_sha": "a57b1d4cb8f18432dfb1d2f7f64be5b19c20ff5d",
        "worktree_path": str(tmp_path / "worktree"),
        "worktree_id": "wt-worker",
    }
    with pytest.raises(registry_module.RegistryError, match="expected JSON object"):
        _approved_writer(
            None,
            repository_path=None,
            approved_repository_path=None,
            approved_base_sha=None,
            protected_branches=set(),
        )
    with pytest.raises(registry_module.RegistryError, match="hexadecimal Git SHA"):
        _run_artifacts("[]", "not-a-sha")
    with pytest.raises(registry_module.RegistryError, match="declared more than once"):
        _run_artifacts('[{"kind":"accepted-head","sha":"a"}]', "a57b1d4")
    with pytest.raises(registry_module.RegistryError, match="protected-branch set"):
        _approved_writer(
            {**writer, "branch_name": "refs/heads/release"},
            repository_path=None,
            approved_repository_path=None,
            approved_base_sha=None,
            protected_branches={"release"},
        )
    with pytest.raises(registry_module.RegistryError, match="approved repository path"):
        _approved_writer(
            writer,
            repository_path=str(tmp_path / "repository-a"),
            approved_repository_path=str(tmp_path / "repository-b"),
            approved_base_sha=None,
            protected_branches=set(),
        )
    with pytest.raises(registry_module.RegistryError, match="non-empty strings"):
        _protected_branches('["main", ""]')
    with pytest.raises(registry_module.RegistryError, match="batch workers"):
        _approved_batch_workers(
            ["not-an-object"],
            approved_repository_path=None,
            approved_base_sha=None,
            protected_branches=set(),
        )


def test_cli_dispatch_forwards_approval_inputs(tmp_path, capsys, monkeypatch):
    captured = {}

    def fake_dispatch_batch(self, root_task_id, **kwargs):
        captured["root_task_id"] = root_task_id
        captured.update(kwargs)
        return {"manager_runs": [], "worker_runs": []}

    monkeypatch.setattr(registry_module.Registry, "dispatch_batch", fake_dispatch_batch)
    base_sha = "a57b1d4cb8f18432dfb1d2f7f64be5b19c20ff5d"
    worker = {
        "task_id": "task-worker",
        "manager_index": 0,
        "identity": {"source": "native", "value": "worker"},
        "writer": {
            "branch_name": "feature/worker",
            "base_sha": base_sha,
            "worktree_path": str(tmp_path / "worktree"),
            "worktree_id": "wt-worker",
        },
    }
    assert (
        main(
            [
                "--db",
                str(tmp_path / "control.db"),
                "dispatch",
                "root-task",
                "--managers-json",
                "[]",
                "--workers-json",
                json.dumps([worker]),
                "--approved-repository-path",
                str(tmp_path / "repository"),
                "--approved-base-sha",
                base_sha,
                "--protected-branches-json",
                '["main"]',
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["worker_runs"] == []
    assert captured["root_task_id"] == "root-task"
    assert captured["workers"][0]["repository_path"] == str(tmp_path / "repository")
    assert captured["workers"][0]["writer"]["approved_base_sha"] == base_sha


@pytest.mark.parametrize(
    "forbidden",
    [
        ["--current"],
        ["--direction", "right"],
    ],
)
def test_cli_parser_rejects_forbidden_pane_inputs(forbidden):
    with pytest.raises(SystemExit) as exc_info:
        _parser().parse_args(["run", "start-worker", "task-1", *forbidden])
    assert exc_info.value.code == 2
