from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from bossmode.bootstrap import BootstrapError, install_project_skill
from bossmode.registry import (
    CREATE_TASK_STATES,
    FEEDBACK_CATEGORIES,
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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
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
            "The operational path is fixed by registry identity; explicit non-repository paths "
            "are ephemeral. `install-skill` touches no registry and rejects this option."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=False)

    registry = subparsers.add_parser(
        "registry", help="Create the repository's operational registry authority"
    )
    registry_commands = registry.add_subparsers(dest="registry_command", required=True)
    registry_commands.add_parser(
        "create",
        help="Create or upgrade the one primary-checkout operational registry",
    )

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
            "Requires an existing operational authority and materialises promotion proposals."
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
    sched_install.add_argument(
        "--repo-dir",
        help="Primary checkout path (default: the validated registry owner)",
    )
    sched_install.add_argument("--log-path", help="Path for output log file")

    sched_status = schedule_commands.add_parser("status", help="Check OS scheduler job status")
    sched_status.add_argument(
        "--repo-dir",
        help="Primary checkout path (default: the validated registry owner)",
    )
    sched_status.add_argument("--log-path", help="Path for output log file")

    sched_uninstall = schedule_commands.add_parser("uninstall", help="Uninstall OS scheduler job")
    sched_uninstall.add_argument(
        "--repo-dir",
        help="Primary checkout path (default: the validated registry owner)",
    )

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


def _resolve_db(value: str | None) -> str:
    return value if value is not None else os.environ.get("BOSSMODE_DB", str(DEFAULT_DB))


def _scheduler_owner(
    registry: Registry, requested_repo_dir: str | None
) -> tuple[Path, dict[str, Any]]:
    identity = registry.get_registry_identity()
    if identity["registry_role"] != "operational":
        raise RegistryError("scheduler commands require an operational registry authority")
    primary_checkout = Path(identity["primary_checkout"])
    if requested_repo_dir is not None and Path(requested_repo_dir).resolve() != primary_checkout:
        raise RegistryError(
            "scheduler repository must match the operational registry owner: "
            f"expected={primary_checkout}, actual={Path(requested_repo_dir).resolve()}"
        )
    return primary_checkout, identity


def _run(args: argparse.Namespace) -> Any:
    if args.command == "install-skill":
        # `--db` used to be accepted here and silently discarded.
        if args.db is not None:
            raise RegistryError("install-skill does not use a registry; remove --db")
        return install_project_skill(args.project_dir)

    database = _resolve_db(args.db)
    if args.command == "registry":
        registry = Registry.create_operational(database)
        return registry.get_registry_identity()

    registry = Registry.open_for_command(
        database,
        explicit_path=args.db is not None or "BOSSMODE_DB" in os.environ,
    )

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

    if args.command == "run":
        if args.run_command == "start":
            return registry.start_run(
                args.task_id,
                agent_role=args.role,
                thread_id=args.thread_id,
                model=args.model,
                reasoning_effort=args.reasoning_effort,
            )
        if args.run_command == "show":
            return registry.get_run(args.run_id)
        return registry.finish_run(
            args.run_id,
            outcome=args.outcome,
            summary=args.summary,
            artifacts=_load_json(args.artifacts_json, list),
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
            evaluator=args.evaluator,
            passed=args.passed,
            score=args.score,
            evidence=args.evidence,
            notes=args.notes,
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
        repo_dir, identity = _scheduler_owner(registry, args.repo_dir)
        if args.schedule_command == "install":
            result = install_schedule(
                repo_dir,
                target=args.target,
                interval_seconds=args.interval,
                cron_expr=args.cron,
                log_path=args.log_path,
            )
        elif args.schedule_command == "status":
            result = get_schedule_status(repo_dir, log_path=args.log_path)
        else:
            result = uninstall_schedule(repo_dir)
        return {
            **result,
            "registry_id": identity["registry_id"],
            "repository_url": identity["repository_url"],
        }

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
