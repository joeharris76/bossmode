from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from bossmode.bootstrap import BootstrapError, install_project_skill
from bossmode.registry import (
    CREATE_TASK_STATES,
    FEEDBACK_CATEGORIES,
    SIGNAL_CATEGORIES,
    TASK_STATES,
    TERMINAL_RUN_OUTCOMES,
    TERMINAL_TURN_OUTCOMES,
    TRANSITION_TARGET_STATES,
    TURN_PURPOSES,
    Registry,
    RegistryError,
)
from bossmode.scheduler import (
    SchedulerError,
    get_schedule_status,
    install_schedule,
    uninstall_schedule,
)

DEFAULT_DB = Path(".bossmode/control.db")


class _ExactArgumentParser(argparse.ArgumentParser):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("allow_abbrev", False)
        super().__init__(*args, **kwargs)


def _parser() -> argparse.ArgumentParser:
    parser = _ExactArgumentParser(
        prog="bossmode",
        description=(
            "Durable control plane for multi-agent supervisor workflows "
            "across AGY, Codex, Claude, Pi, Grok, and Muse."
        ),
    )
    parser.add_argument(
        "--db",
        default=None,
        help=(
            "SQLite registry path (default: $BOSSMODE_DB, else .bossmode/control.db). "
            "`install-skill` touches no registry and rejects this option."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=False)

    install_skill = subparsers.add_parser(
        "install-skill", help="Install the version-matched Bossmode skill into a project"
    )
    install_skill.add_argument(
        "--project-dir", default=".", help="Existing project directory (default: .)"
    )

    subparsers.add_parser(
        "reconcile",
        help=(
            "Converge the registry and report control-plane state (default command). "
            "Creates or migrates the registry and materialises promotion proposals."
        ),
    )

    task = subparsers.add_parser("task", help="Create and manage tasks")
    task_commands = task.add_subparsers(dest="task_command", required=True)
    task_create = task_commands.add_parser("create", help="Create a new task")
    task_create.add_argument("--title", required=True, help="Short task name")
    task_create.add_argument("--goal", required=True, help="The outcome this task must achieve")
    task_create.add_argument(
        "--success-criteria", required=True, help="Observable evidence that proves completion"
    )
    task_create.add_argument(
        "--state",
        choices=sorted(CREATE_TASK_STATES),
        default="ready",
        help="Initial state (default: ready)",
    )
    task_create.add_argument(
        "--priority", type=int, default=0, help="Dispatch priority; higher wins (default: 0)"
    )
    task_create.add_argument(
        "--permissions-json",
        default="{}",
        help="JSON object describing the task's permission scope (default: {})",
    )
    task_create.add_argument("--next-action", help="Free-text hint for the next step on this task")
    task_create.add_argument("--parent-task-id", help="Parent task in a manager team hierarchy")
    task_create.add_argument("--team-id", help="Team that owns this task")
    task_create.add_argument(
        "--task-kind", default="task", help="What sort of task this is (default: task)"
    )
    task_create.add_argument(
        "--scope-json", default="{}", help="JSON object bounding the task's file and resource scope"
    )
    task_create.add_argument(
        "--approved-base-sha",
        dest="approved_base_sha",
        help="Supervisor-approved base SHA that writer branches must start from",
    )

    task_list = task_commands.add_parser("list", help="List tasks by state")
    task_list.add_argument(
        "--state",
        action="append",
        choices=sorted(TASK_STATES),
        help="Filter by task state; repeat to include several states",
    )

    task_show = task_commands.add_parser("show", help="Show task details")
    task_show.add_argument("task_id", help="Task ID to show")

    task_transition = task_commands.add_parser(
        "transition",
        help="Move a task to an explicitly reachable state",
    )
    task_transition.add_argument("task_id", help="Task ID to transition")
    task_transition.add_argument(
        "to_state",
        choices=sorted(TRANSITION_TARGET_STATES),
        help=(
            "Destination state. Lifecycle commands own every other state change: "
            "`run start` moves ready to running, `run finish` leaves running, and "
            "`evaluate` resolves evaluating."
        ),
    )
    task_transition.add_argument("--actor", required=True, help="Who is making this change")
    task_transition.add_argument("--reason", required=True, help="Why the state is changing")
    task_transition.add_argument("--evidence", help="Supporting evidence for the change")
    task_transition.add_argument("--next-action", help="Replacement next-action hint")
    task_transition.add_argument("--blocked-on", help="What the task is blocked on")

    team = subparsers.add_parser("team", help="Create and inspect parallel manager teams")
    team_commands = team.add_subparsers(dest="team_command", required=True)
    team_create = team_commands.add_parser("create", help="Create a parallel manager team")
    team_create.add_argument("root_task_id", help="Root task the team hangs from")
    team_create.add_argument("--name", required=True, help="Human-readable team name")
    team_create.add_argument(
        "--manager-identity-json", required=True, help="JSON object identifying the team's manager"
    )
    team_create.add_argument(
        "--scope-json", default="{}", help="JSON object bounding the team's file and resource scope"
    )
    team_create.add_argument("--parent-team-id", help="Parent team, for nested teams")
    team_create.add_argument("--tab-label", help="Expected Herdr tab label for this team")
    team_bind_tab = team_commands.add_parser(
        "bind-tab", help="Reconcile a team's expected Herdr tab"
    )
    team_bind_tab.add_argument("team_id", help="Team to act on")
    team_bind_tab.add_argument(
        "--herdr-session", required=True, help="Herdr session that owns the tab"
    )
    team_bind_tab.add_argument("--workspace-id", required=True, help="Observed Herdr workspace ID")
    team_bind_tab.add_argument("--tab-id", required=True, help="Observed Herdr tab ID")
    team_bind_tab.add_argument(
        "--observed-tab-label",
        required=True,
        help="Tab label observed live; must match the expectation",
    )
    team_list = team_commands.add_parser("list", help="List parallel manager teams")
    team_list.add_argument("--root-task-id", help="Filter teams by root task")
    team_show = team_commands.add_parser("show", help="Show parallel manager team details")
    team_show.add_argument("team_id", help="Team to act on")

    run = subparsers.add_parser("run", help="Record delegated agent runs")
    run_commands = run.add_subparsers(dest="run_command", required=True)
    run_start = run_commands.add_parser("start", help="Start an execution run")
    run_start.add_argument("task_id", help="Task this run executes; must be ready")
    run_start.add_argument(
        "--role", required=True, help="Agent role, e.g. researcher, worker, or reviewer"
    )
    run_start.add_argument("--thread-id", help="Live native thread or subagent task ID")
    run_start.add_argument("--model", help="Model identifier, recorded for telemetry")
    run_start.add_argument("--reasoning-effort", help="Reasoning effort, recorded for telemetry")

    manager_start = run_commands.add_parser("start-manager", help="Start a manager run")
    manager_start.add_argument("team_id", help="Team to act on")
    manager_start.add_argument(
        "--identity-json", required=True, help="JSON object identifying this run's agent"
    )
    manager_start.add_argument("--model", help="Model identifier, recorded for telemetry")
    manager_start.add_argument(
        "--reasoning-effort", help="Reasoning effort, recorded for telemetry"
    )

    worker_start = run_commands.add_parser("start-worker", help="Start a fenced writer run")
    worker_start.add_argument("task_id", help="Task this fenced writer executes")
    worker_start.add_argument(
        "--manager-run-id", required=True, help="Manager run that owns this worker"
    )
    worker_start.add_argument(
        "--identity-json", required=True, help="JSON object identifying this run's agent"
    )
    worker_start.add_argument(
        "--writer-json", required=True, help="JSON object declaring branch, base SHA, and worktree"
    )
    worker_start.add_argument("--repository-path", help="Repository this run writes in")
    worker_start.add_argument(
        "--approved-repository-path",
        dest="approved_repository_path",
        help="Supervisor-approved repository path for live writer admission",
    )
    worker_start.add_argument(
        "--approved-base-sha",
        dest="approved_base_sha",
        help="Supervisor-approved base SHA; must match the writer declaration",
    )
    worker_start.add_argument(
        "--protected-branches-json",
        default="[]",
        help="JSON array of repository-configured protected branches",
    )
    worker_start.add_argument(
        "--resources-json", default="[]", help="JSON array of resources to lease (default: [])"
    )
    worker_start.add_argument(
        "--lease-seconds", type=int, default=300, help="Lease duration in seconds (default: 300)"
    )
    worker_start.add_argument("--model", help="Model identifier, recorded for telemetry")
    worker_start.add_argument("--reasoning-effort", help="Reasoning effort, recorded for telemetry")

    reviewer_start = run_commands.add_parser(
        "start-reviewer", help="Start an independent reviewer run"
    )
    reviewer_start.add_argument("task_id", help="Task whose worker result is reviewed")
    reviewer_start.add_argument(
        "--worker-run-id", required=True, help="Worker run this reviewer independently checks"
    )
    reviewer_start.add_argument(
        "--identity-json", required=True, help="JSON object identifying this run's agent"
    )
    reviewer_start.add_argument("--model", help="Model identifier, recorded for telemetry")
    reviewer_start.add_argument(
        "--reasoning-effort", help="Reasoning effort, recorded for telemetry"
    )

    run_show = run_commands.add_parser("show", help="Show run details")
    run_show.add_argument("run_id", help="Run ID to show")

    run_finish = run_commands.add_parser("finish", help="Complete a run")
    run_finish.add_argument("run_id", help="Run ID to finish")
    run_finish.add_argument(
        "--outcome",
        choices=sorted(TERMINAL_RUN_OUTCOMES),
        required=True,
        help="How the run ended; `succeeded` moves the task to evaluating, not to success",
    )
    run_finish.add_argument("--summary", required=True, help="What changed and what was verified")
    run_finish.add_argument(
        "--artifacts-json", default="[]", help="JSON array of declared artifacts (default: [])"
    )
    run_finish.add_argument("--tokens", type=int, help="Tokens consumed, recorded for telemetry")
    run_finish.add_argument(
        "--duration-seconds", type=float, help="Wall-clock duration, recorded for telemetry"
    )
    run_finish.add_argument(
        "--retries", type=int, default=0, help="Retry count for this run (default: 0)"
    )
    run_finish.add_argument("--blocked-on", help="What the task is blocked on, if blocked")
    run_finish.add_argument(
        "--accepted-head-sha",
        dest="accepted_head_sha",
        help="Exact Git head SHA accepted for this run",
    )

    run_record_head = run_commands.add_parser(
        "record-head",
        help="Record a missing accepted head for a finished team worker",
    )
    run_record_head.add_argument("run_id", help="Finished worker run to record a head for")
    run_record_head.add_argument(
        "--repository-path", required=True, help="Repository the accepted head belongs to"
    )
    run_record_head.add_argument(
        "--accepted-head-sha", required=True, help="Exact Git head SHA accepted for this run"
    )
    run_record_head.add_argument(
        "--evidence", required=True, help="Evidence that this head was reviewed and accepted"
    )

    herdr = subparsers.add_parser("herdr", help="Bind a run to an official Herdr session")
    herdr_commands = herdr.add_subparsers(dest="herdr_command", required=True)
    herdr_bind = herdr_commands.add_parser("bind", help="Bind Herdr worker to run")
    herdr_bind.add_argument("run_id", help="Run to bind; must be running")
    herdr_bind.add_argument("--herdr-session", required=True, help="Herdr session name")
    herdr_bind.add_argument(
        "--worker", required=True, help="Observed live Herdr worker name (lowercase)"
    )
    herdr_bind.add_argument(
        "--agent-kind", required=True, help="Agent product, e.g. pi, codex, claude, agy, grok, muse"
    )
    herdr_bind.add_argument(
        "--status",
        choices=["pending", "live", "blocked", "unknown"],
        default="live",
        help=(
            "Observed binding liveness (default: live). "
            "`stale` is set automatically when the run finishes"
        ),
    )
    herdr_bind.add_argument("--session-source", help="Native session source; requires the full set")
    herdr_bind.add_argument("--session-agent", help="Native session agent; requires the full set")
    herdr_bind.add_argument(
        "--session-ref-kind",
        choices=["id", "path"],
        help="How to interpret --session-value; requires the full set",
    )
    herdr_bind.add_argument("--session-value", help="Native session reference value")
    herdr_bind.add_argument("--pane-id", help="Observed Herdr pane ID")
    herdr_bind.add_argument("--tab-id", help="Observed Herdr tab ID")
    herdr_bind.add_argument("--workspace-id", help="Observed Herdr workspace ID")

    herdr_show = herdr_commands.add_parser("show", help="Show Herdr worker binding")
    herdr_show.add_argument("run_id", help="Run whose binding to show")

    turn = subparsers.add_parser("turn", help="Record a correlated Herdr prompt and result")
    turn_commands = turn.add_subparsers(dest="turn_command", required=True)
    turn_start = turn_commands.add_parser("start", help="Start an execution turn")
    turn_start.add_argument("run_id", help="Run to open a turn on; needs a live Herdr binding")
    turn_start.add_argument(
        "--purpose",
        choices=sorted(TURN_PURPOSES),
        required=True,
        help="Why this turn is being sent",
    )
    turn_start.add_argument(
        "--prompt", required=True, help="Logical prompt text; its digest correlates the turn"
    )

    turn_show = turn_commands.add_parser("show", help="Show turn details")
    turn_show.add_argument("turn_id", help="Turn ID to show")

    turn_finish = turn_commands.add_parser("finish", help="Complete and validate turn")
    turn_finish.add_argument("turn_id", help="Turn ID to finish")
    turn_finish.add_argument(
        "--outcome",
        choices=sorted(TERMINAL_TURN_OUTCOMES),
        required=True,
        help="How the turn ended; `succeeded` validates the exact JSON result artifact",
    )
    turn_finish.add_argument(
        "--summary", help="Required unless the outcome is succeeded, which reads the result file"
    )
    turn_finish.add_argument(
        "--lifecycle-evidence", help="Observed Herdr lifecycle state supporting this outcome"
    )

    evaluate = subparsers.add_parser("evaluate", help="Record an independent evaluation")
    evaluate.add_argument("task_id", help="Task being evaluated; must be in evaluating")
    evaluate.add_argument("--run-id", required=True, help="Run whose result is being evaluated")
    evaluate.add_argument(
        "--evaluator-run-id", help="Reviewer's own run, when the reviewer is a team member"
    )
    evaluate.add_argument(
        "--evaluator", required=True, help="Reviewer identity; must differ from the run's agent"
    )
    result = evaluate.add_mutually_exclusive_group(required=True)
    result.add_argument(
        "--passed", action="store_true", help="Evaluation passed; moves the task to succeeded"
    )
    result.add_argument(
        "--failed", action="store_true", help="Evaluation failed; moves the task to failed"
    )
    evaluate.add_argument("--score", type=float, help="Optional score between 0 and 1 inclusive")
    evaluate.add_argument(
        "--evidence", required=True, help="External evidence or checks supporting this verdict"
    )
    evaluate.add_argument(
        "--reviewed-head-sha",
        help="Exact Git head reviewed for a team worker; must match its accepted head",
    )
    evaluate.add_argument("--notes", help="Additional reviewer notes")

    feedback = subparsers.add_parser("feedback", help="Record structured user or system feedback")
    feedback.add_argument("task_id", help="Task this feedback relates to")
    feedback.add_argument("--run-id", help="Run this feedback relates to, if any")
    feedback.add_argument(
        "--category",
        choices=sorted(FEEDBACK_CATEGORIES),
        required=True,
        help="What kind of feedback this is",
    )
    feedback.add_argument(
        "--key", required=True, help="Stable, narrowly scoped recurrence key for grouping"
    )
    feedback.add_argument("--content", required=True, help="The feedback text itself")

    promotion = subparsers.add_parser("promotion", help="Inspect and gate proposed learning")
    promotion_commands = promotion.add_subparsers(dest="promotion_command", required=True)
    promotion_commands.add_parser("propose", help="Generate promotion proposals from feedback")
    promotion_list = promotion_commands.add_parser("list", help="List promotion proposals")
    promotion_list.add_argument(
        "--status",
        choices=["proposed", "accepted", "rejected", "applied"],
        help="Filter proposals by gate position",
    )
    promotion_accept = promotion_commands.add_parser(
        "accept", help="Accept a promotion for implementation"
    )
    promotion_accept.add_argument("promotion_id", help="Promotion to accept")
    promotion_reject = promotion_commands.add_parser("reject", help="Reject a promotion proposal")
    promotion_reject.add_argument("promotion_id", help="Promotion to reject")
    promotion_apply = promotion_commands.add_parser(
        "apply", help="Mark an accepted promotion as applied"
    )
    promotion_apply.add_argument("promotion_id", help="Promotion to mark applied")

    dispatch = subparsers.add_parser(
        "dispatch", help="Atomically dispatch a parallel manager batch"
    )
    dispatch.add_argument("root_task_id", help="Root task the batch hangs from")
    dispatch.add_argument(
        "--managers-json", required=True, help="JSON array of manager specifications"
    )
    dispatch.add_argument(
        "--workers-json", required=True, help="JSON array of worker specifications"
    )
    dispatch.add_argument(
        "--approved-repository-path",
        dest="approved_repository_path",
        help="Supervisor-approved repository path for all batch writers",
    )
    dispatch.add_argument(
        "--approved-base-sha",
        dest="approved_base_sha",
        help="Supervisor-approved base SHA for all batch writers",
    )
    dispatch.add_argument(
        "--protected-branches-json",
        default="[]",
        help="JSON array of repository-configured protected branches (default: [])",
    )

    resource = subparsers.add_parser("resource", help="Manage fenced resource leases")
    resource_commands = resource.add_subparsers(dest="resource_command", required=True)
    resource_reclaim = resource_commands.add_parser(
        "reclaim", help="Expire leases past their deadline and report reclaimable claims"
    )
    resource_reclaim.add_argument("--now", help="Override the evaluation time (ISO 8601, UTC)")
    resource_release = resource_commands.add_parser(
        "release", help="Release an expired claim after verifying live state"
    )
    resource_release.add_argument("claim_id", help="Resource claim to release")
    resource_release.add_argument("--run-id", required=True, help="Run that holds the claim")
    resource_release.add_argument(
        "--fence-token", required=True, help="Fence token proving the holder is current"
    )
    resource_release.add_argument(
        "--evidence", required=True, help="Evidence that the live resource was verified"
    )

    report = subparsers.add_parser(
        "report", help="Show a redacted executive roll-up for a task tree"
    )
    report.add_argument("task_id", help="Root task to report on")

    signal = subparsers.add_parser(
        "signal", help="Record an executive decision, blocker, or approval"
    )
    signal.add_argument("task_id", help="Task this signal relates to")
    signal.add_argument(
        "category", choices=sorted(SIGNAL_CATEGORIES), help="What sort of signal this is"
    )
    signal.add_argument("--content", required=True, help="The signal text itself")
    signal.add_argument("--source-run-id", help="Run that raised this signal, if any")
    signal.add_argument("--team-id", help="Team this signal belongs to")
    signal.add_argument(
        "--redacted", action="store_true", help="Mark the content as redacted in reports"
    )

    subparsers.add_parser(
        "maintenance",
        help="Run telemetry analytics, health checks, and promotion discovery",
    )

    schedule = subparsers.add_parser(
        "schedule", help="Manage native OS scheduling for routine tasks"
    )
    schedule_commands = schedule.add_subparsers(dest="schedule_command", required=True)

    sched_install = schedule_commands.add_parser("install", help="Install OS scheduler job")
    sched_install.add_argument(
        "--interval", type=int, default=3600, help="Interval in seconds (default: 3600)"
    )
    sched_install.add_argument("--cron", help="Cron expression (e.g. '0 * * * *')")
    sched_install.add_argument(
        "--target",
        choices=["maintenance", "reconcile"],
        default="maintenance",
        help="Target command to run (default: maintenance)",
    )
    sched_install.add_argument("--repo-dir", default=".", help="Repository path (default: .)")
    sched_install.add_argument("--log-path", help="Path for output log file")

    sched_status = schedule_commands.add_parser("status", help="Check OS scheduler job status")
    sched_status.add_argument("--repo-dir", default=".", help="Repository path (default: .)")
    sched_status.add_argument("--log-path", help="Path for output log file")

    sched_uninstall = schedule_commands.add_parser("uninstall", help="Uninstall OS scheduler job")
    sched_uninstall.add_argument("--repo-dir", default=".", help="Repository path (default: .)")

    return parser


def _emit(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def _load_json(value: str, expected_type: type[Any]) -> Any:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as error:
        raise RegistryError(f"invalid JSON: {error.msg}") from error
    if not isinstance(parsed, expected_type):
        raise RegistryError(f"expected JSON {expected_type.__name__}")
    return parsed


def _validate_sha(value: str, *, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{7,64}", value):
        raise RegistryError(f"{label} must be a hexadecimal Git SHA")
    return value


def _protected_branches(value: str) -> set[str]:
    branches = _load_json(value, list)
    normalized: set[str] = set()
    for branch in branches:
        if not isinstance(branch, str) or not branch.strip():
            raise RegistryError("protected branches must be non-empty strings")
        normalized.add(branch.strip().removeprefix("refs/heads/").removeprefix("origin/"))
    return normalized


def _approved_writer(
    writer: dict[str, Any],
    *,
    repository_path: str | None,
    approved_repository_path: str | None,
    approved_base_sha: str | None,
    protected_branches: set[str],
) -> tuple[dict[str, Any], str | None]:
    if not isinstance(writer, dict):
        raise RegistryError("expected JSON object")
    result = dict(writer)
    effective_repository = approved_repository_path or repository_path
    if (
        approved_repository_path is not None
        and repository_path is not None
        and os.path.realpath(os.path.abspath(approved_repository_path))
        != os.path.realpath(os.path.abspath(repository_path))
    ):
        raise RegistryError("repository path does not match the approved repository path")
    if approved_base_sha is not None:
        approved_base_sha = _validate_sha(approved_base_sha, label="approved base SHA")
        declared_base_sha = result.get("base_sha")
        if declared_base_sha != approved_base_sha:
            raise RegistryError("writer base SHA does not match the approved base SHA")
        result["approved_base_sha"] = approved_base_sha
    branch = result.get("branch_name")
    if protected_branches and isinstance(branch, str):
        normalized_branch = branch.strip().removeprefix("refs/heads/")
        if normalized_branch in protected_branches:
            raise RegistryError("writer branch is in the approved protected-branch set")
    if protected_branches:
        result["protected_branches"] = sorted(protected_branches)
    if effective_repository is not None:
        result["repository_path"] = effective_repository
    if approved_repository_path is not None:
        result["approved_repository_path"] = approved_repository_path
    return result, effective_repository


def _approved_batch_workers(
    workers: list[Any],
    *,
    approved_repository_path: str | None,
    approved_base_sha: str | None,
    protected_branches: set[str],
) -> list[dict[str, Any]]:
    approved: list[dict[str, Any]] = []
    for worker in workers:
        if not isinstance(worker, dict):
            raise RegistryError("batch workers must be JSON objects")
        spec = dict(worker)
        writer, repository_path = _approved_writer(
            spec.get("writer"),
            repository_path=spec.get("repository_path"),
            approved_repository_path=approved_repository_path,
            approved_base_sha=approved_base_sha,
            protected_branches=protected_branches,
        )
        spec["writer"] = writer
        if repository_path is not None:
            spec["repository_path"] = repository_path
        approved.append(spec)
    return approved


def _run_artifacts(artifacts_json: str, accepted_head_sha: str | None) -> list[Any]:
    artifacts = _load_json(artifacts_json, list)
    if accepted_head_sha is None:
        return artifacts
    accepted_head_sha = _validate_sha(accepted_head_sha, label="accepted head SHA")
    if any(
        isinstance(artifact, dict) and artifact.get("kind") == "accepted-head"
        for artifact in artifacts
    ):
        raise RegistryError("accepted head SHA is declared more than once")
    return [*artifacts, {"kind": "accepted-head", "sha": accepted_head_sha}]


def _resolve_db(value: str | None) -> str:
    return value if value is not None else os.environ.get("BOSSMODE_DB", str(DEFAULT_DB))


def _run(args: argparse.Namespace) -> Any:
    if args.command == "install-skill":
        # `--db` used to be accepted here and silently discarded.
        if args.db is not None:
            raise RegistryError("install-skill does not use a registry; remove --db")
        return install_project_skill(args.project_dir)

    registry = Registry(_resolve_db(args.db))

    # Default action: naked `bossmode` or the explicit `bossmode reconcile`.
    if args.command in (None, "reconcile"):
        return registry.reconcile()

    if args.command == "task":
        if args.task_command == "create":
            return registry.create_task(
                title=args.title,
                goal=args.goal,
                success_criteria=args.success_criteria,
                state=args.state,
                priority=args.priority,
                permissions=_load_json(args.permissions_json, dict),
                next_action=args.next_action,
                parent_task_id=args.parent_task_id,
                team_id=args.team_id,
                task_kind=args.task_kind,
                scope=_load_json(args.scope_json, dict),
                approved_base_sha=args.approved_base_sha,
            )
        if args.task_command == "list":
            return registry.list_tasks(args.state)
        if args.task_command == "show":
            return registry.get_task(args.task_id)
        return registry.transition_task(
            args.task_id,
            args.to_state,
            actor=args.actor,
            reason=args.reason,
            evidence=args.evidence,
            next_action=args.next_action,
            blocked_on=args.blocked_on,
        )

    if args.command == "team":
        if args.team_command == "create":
            return registry.create_team(
                args.root_task_id,
                name=args.name,
                manager_identity=_load_json(args.manager_identity_json, dict),
                scope=_load_json(args.scope_json, dict),
                parent_team_id=args.parent_team_id,
                tab_label=args.tab_label,
            )
        if args.team_command == "list":
            return registry.list_teams(args.root_task_id)
        if args.team_command == "bind-tab":
            return registry.bind_team_herdr_tab(
                args.team_id,
                herdr_session=args.herdr_session,
                workspace_id=args.workspace_id,
                tab_id=args.tab_id,
                observed_tab_label=args.observed_tab_label,
            )
        return registry.get_team(args.team_id)

    if args.command == "run":
        if args.run_command == "start":
            return registry.start_run(
                args.task_id,
                agent_role=args.role,
                thread_id=args.thread_id,
                model=args.model,
                reasoning_effort=args.reasoning_effort,
            )
        if args.run_command == "start-manager":
            return registry.start_manager_run(
                args.team_id,
                identity=_load_json(args.identity_json, dict),
                model=args.model,
                reasoning_effort=args.reasoning_effort,
            )
        if args.run_command == "start-worker":
            writer, repository_path = _approved_writer(
                _load_json(args.writer_json, dict),
                repository_path=args.repository_path,
                approved_repository_path=args.approved_repository_path,
                approved_base_sha=args.approved_base_sha,
                protected_branches=_protected_branches(args.protected_branches_json),
            )
            return registry.start_worker_run(
                args.task_id,
                manager_run_id=args.manager_run_id,
                identity=_load_json(args.identity_json, dict),
                writer=writer,
                repository_path=repository_path,
                resources=_load_json(args.resources_json, list),
                lease_seconds=args.lease_seconds,
                model=args.model,
                reasoning_effort=args.reasoning_effort,
            )
        if args.run_command == "start-reviewer":
            return registry.start_reviewer_run(
                args.task_id,
                worker_run_id=args.worker_run_id,
                identity=_load_json(args.identity_json, dict),
                model=args.model,
                reasoning_effort=args.reasoning_effort,
            )
        if args.run_command == "show":
            return registry.get_run(args.run_id)
        if args.run_command == "record-head":
            return registry.record_accepted_head(
                args.run_id,
                repository_path=args.repository_path,
                accepted_head_sha=args.accepted_head_sha,
                evidence=args.evidence,
            )
        return registry.finish_run(
            args.run_id,
            outcome=args.outcome,
            summary=args.summary,
            artifacts=_run_artifacts(args.artifacts_json, args.accepted_head_sha),
            accepted_head_sha=args.accepted_head_sha,
            tokens=args.tokens,
            duration_seconds=args.duration_seconds,
            retries=args.retries,
            blocked_on=args.blocked_on,
        )

    if args.command == "herdr":
        if args.herdr_command == "show":
            return registry.get_run(args.run_id)["herdr_binding"]
        return registry.bind_herdr_run(
            args.run_id,
            herdr_session=args.herdr_session,
            worker_name=args.worker,
            agent_kind=args.agent_kind,
            status=args.status,
            session_source=args.session_source,
            session_agent=args.session_agent,
            session_ref_kind=args.session_ref_kind,
            session_value=args.session_value,
            pane_id=args.pane_id,
            tab_id=args.tab_id,
            workspace_id=args.workspace_id,
        )

    if args.command == "turn":
        if args.turn_command == "start":
            return registry.start_turn(args.run_id, purpose=args.purpose, prompt=args.prompt)
        if args.turn_command == "show":
            return registry.get_turn(args.turn_id)
        return registry.finish_turn(
            args.turn_id,
            outcome=args.outcome,
            summary=args.summary,
            lifecycle_evidence=args.lifecycle_evidence,
        )

    if args.command == "evaluate":
        return registry.add_evaluation(
            args.task_id,
            run_id=args.run_id,
            evaluator_run_id=args.evaluator_run_id,
            evaluator=args.evaluator,
            passed=args.passed,
            score=args.score,
            evidence=args.evidence,
            reviewed_head_sha=args.reviewed_head_sha,
            notes=args.notes,
        )

    if args.command == "dispatch":
        workers = _approved_batch_workers(
            _load_json(args.workers_json, list),
            approved_repository_path=args.approved_repository_path,
            approved_base_sha=args.approved_base_sha,
            protected_branches=_protected_branches(args.protected_branches_json),
        )
        return registry.dispatch_batch(
            args.root_task_id,
            managers=_load_json(args.managers_json, list),
            workers=workers,
        )

    if args.command == "resource":
        if args.resource_command == "release":
            return registry.release_expired_resource_claim(
                args.claim_id,
                run_id=args.run_id,
                fence_token=args.fence_token,
                evidence=args.evidence,
            )
        return registry.reclaim_resource_claims(now=args.now)

    if args.command == "report":
        return registry.executive_report(args.task_id)

    if args.command == "signal":
        return registry.record_signal(
            args.task_id,
            category=args.category,
            content=args.content,
            source_run_id=args.source_run_id,
            team_id=args.team_id,
            redacted=args.redacted,
        )

    if args.command == "feedback":
        return registry.add_feedback(
            args.task_id,
            run_id=args.run_id,
            category=args.category,
            recurrence_key=args.key,
            content=args.content,
        )

    if args.command == "promotion":
        if args.promotion_command == "propose":
            return registry.propose_promotions()
        if args.promotion_command == "list":
            return registry.list_promotions(args.status)
        if args.promotion_command == "accept":
            return registry.accept_promotion(args.promotion_id)
        if args.promotion_command == "reject":
            return registry.reject_promotion(args.promotion_id)
        return registry.apply_promotion(args.promotion_id)

    if args.command == "maintenance":
        return registry.run_maintenance()

    if args.command == "schedule":
        if args.schedule_command == "install":
            return install_schedule(
                args.repo_dir,
                target=args.target,
                interval_seconds=args.interval,
                cron_expr=args.cron,
                log_path=args.log_path,
            )
        if args.schedule_command == "status":
            return get_schedule_status(args.repo_dir, log_path=args.log_path)
        return uninstall_schedule(args.repo_dir)

    # Defensive: every subparser above returns, and each subcommand group is
    # required, so this is unreachable. It replaces a silent fallthrough that
    # used to reconcile instead of failing.
    raise RegistryError(f"unhandled command: {args.command}")  # pragma: no cover


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        _emit(_run(args))
    except (BootstrapError, OSError, RegistryError, SchedulerError) as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        return 2
    except sqlite3.Error as error:
        print(json.dumps({"error": f"database error: {error}"}), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
