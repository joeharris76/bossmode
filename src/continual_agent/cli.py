from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from continual_agent.registry import TASK_STATES, Registry, RegistryError

DEFAULT_DB = Path(".continual/control.db")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="continual-agent",
        description="Durable control plane for a Codex-native continual-agent spike.",
    )
    parser.add_argument(
        "--db",
        default=os.environ.get("CONTINUAL_AGENT_DB", str(DEFAULT_DB)),
        help="SQLite registry path (default: .continual/control.db)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", help="Initialize the registry")

    task = subparsers.add_parser("task", help="Create and manage tasks")
    task_commands = task.add_subparsers(dest="task_command", required=True)
    task_add = task_commands.add_parser("add")
    task_add.add_argument("--title", required=True)
    task_add.add_argument("--goal", required=True)
    task_add.add_argument("--success-criteria", required=True)
    task_add.add_argument("--state", choices=sorted(TASK_STATES), default="ready")
    task_add.add_argument("--priority", type=int, default=0)
    task_add.add_argument("--permissions-json", default="{}")
    task_add.add_argument("--next-action")

    task_list = task_commands.add_parser("list")
    task_list.add_argument("--state", action="append", choices=sorted(TASK_STATES))

    task_show = task_commands.add_parser("show")
    task_show.add_argument("task_id")

    task_transition = task_commands.add_parser("transition")
    task_transition.add_argument("task_id")
    task_transition.add_argument("to_state", choices=sorted(TASK_STATES))
    task_transition.add_argument("--actor", required=True)
    task_transition.add_argument("--reason", required=True)
    task_transition.add_argument("--evidence")
    task_transition.add_argument("--next-action")
    task_transition.add_argument("--blocked-on")

    run = subparsers.add_parser("run", help="Record delegated agent runs")
    run_commands = run.add_subparsers(dest="run_command", required=True)
    run_start = run_commands.add_parser("start")
    run_start.add_argument("task_id")
    run_start.add_argument("--role", required=True)
    run_start.add_argument("--thread-id")
    run_start.add_argument("--model")
    run_start.add_argument("--reasoning-effort")

    run_finish = run_commands.add_parser("finish")
    run_finish.add_argument("run_id")
    run_finish.add_argument(
        "--outcome", choices=["waiting_user", "blocked", "succeeded", "failed"], required=True
    )
    run_finish.add_argument("--summary", required=True)
    run_finish.add_argument("--artifacts-json", default="[]")
    run_finish.add_argument("--tokens", type=int)
    run_finish.add_argument("--duration-seconds", type=float)
    run_finish.add_argument("--retries", type=int, default=0)
    run_finish.add_argument("--blocked-on")

    evaluate = subparsers.add_parser("evaluate", help="Record an independent evaluation")
    evaluate.add_argument("task_id")
    evaluate.add_argument("--run-id")
    evaluate.add_argument("--evaluator", required=True)
    result = evaluate.add_mutually_exclusive_group(required=True)
    result.add_argument("--passed", action="store_true")
    result.add_argument("--failed", action="store_true")
    evaluate.add_argument("--score", type=float)
    evaluate.add_argument("--evidence", required=True)
    evaluate.add_argument("--notes")

    feedback = subparsers.add_parser("feedback", help="Record structured user or system feedback")
    feedback.add_argument("task_id")
    feedback.add_argument("--run-id")
    feedback.add_argument(
        "--kind", choices=["preference", "correction", "failure", "observation"], required=True
    )
    feedback.add_argument("--key", required=True, help="Stable recurrence key")
    feedback.add_argument("--content", required=True)

    promotion = subparsers.add_parser("promotion", help="Inspect and gate proposed learning")
    promotion_commands = promotion.add_subparsers(dest="promotion_command", required=True)
    promotion_propose = promotion_commands.add_parser("propose")
    promotion_propose.set_defaults(promotion_command="propose")
    promotion_list = promotion_commands.add_parser("list")
    promotion_list.add_argument("--status", choices=["proposed", "accepted", "rejected", "applied"])
    promotion_set = promotion_commands.add_parser("set")
    promotion_set.add_argument("promotion_id")
    promotion_set.add_argument("status", choices=["accepted", "rejected", "applied"])

    supervisor = subparsers.add_parser("supervisor", help="Compute the next supervisor action")
    supervisor_commands = supervisor.add_subparsers(dest="supervisor_command", required=True)
    supervisor_commands.add_parser("tick")
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


def _run(args: argparse.Namespace) -> Any:
    registry = Registry(args.db)
    if args.command == "init":
        registry.initialize()
        return {"database": str(Path(args.db).resolve()), "initialized": True}

    if args.command == "task":
        if args.task_command == "add":
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
            kind=args.kind,
            recurrence_key=args.key,
            content=args.content,
        )

    if args.command == "promotion":
        if args.promotion_command == "propose":
            return registry.propose_promotions()
        if args.promotion_command == "list":
            return registry.list_promotions(args.status)
        return registry.set_promotion_status(args.promotion_id, args.status)

    return registry.supervisor_tick()


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        _emit(_run(args))
    except RegistryError as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
