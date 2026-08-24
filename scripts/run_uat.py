#!/usr/bin/env python3
"""Automated in-process functional acceptance loop for Bossmode.

Exercises the full lifecycle across:
1. Scenario 1: Happy Path End-to-End Task Execution & Independent Evaluation Pass
2. Scenario 2: Evaluator Rejection, Feedback Ingestion & Multi-Turn Remediation
3. Scenario 3: Feedback Promotion Ladder with User Gating
4. Scenario 4: Adversarial Traps, Rejection Safeguards & Fault Injection
5. Scenario 5: Single-Flight Scheduling & Atomic Snapshot Read Consistency
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bossmode.cli import main as cli_main
from bossmode.registry import MAX_TURN_RESULT_BYTES, SCHEMA_VERSION, Registry, RegistryError

EXPECTED_UAT_CHECKS = (
    "1.1 Initialize registry database",
    "1.2 Create task with explicit criteria and permissions",
    "1.3 Reconcile session state and select dispatch task",
    "1.4 Start run and bind Herdr worker with native session",
    "1.5 Execute turn with correlated artifact validation",
    "1.6 Finish run into evaluating state & stale Herdr binding",
    "1.7 Independent evaluator verifies and moves task to succeeded",
    "2.1 Create task and dispatch Run 1",
    "2.2 Run 1 finishes with subtle defect",
    "2.3 Independent evaluator rejects Run 1 -> task marked failed",
    "2.4 Ingest correction feedback and transition task to ready",
    "2.5 Run 2 executes correction turn and finishes into evaluating",
    "2.6 Evaluator passes Run 2 -> task succeeded",
    "3.1 Log feedback across control, skill, and memory categories",
    "3.2 Verify skill proposal requires passing evaluation evidence",
    "3.3 Add passing evaluation and verify skill proposal generated",
    "3.4 User approval gate enforces proposed -> accepted -> applied",
    "3.5 Re-propose rejected promotion upon new feedback",
    "4.1 Reject self-evaluation when evaluator == run.agent_role",
    "4.2 Reject evaluation when task is not in evaluating state",
    "4.3 Reject finish_run(succeeded) when all turns failed",
    "4.4 Detect and reject markdown code fences in turn result JSON",
    "4.5 Reject turn results exceeding 1 MiB limit",
    "4.6 Reject duplicate worker name binding to concurrent active runs",
    "4.7 Reject manual stale status assignment via bind_herdr_run",
    "5.1 Verify single-flight pause during active execution",
    "5.2 Dispatch selects highest priority ready task when queue clears",
    "5.3 Exercise naked CLI and subcommands against in-process registry",
)


@dataclass
class UATResult:
    scenario: str
    name: str
    passed: bool
    duration_ms: float
    details: str = ""
    error: str | None = None


@dataclass
class UATReport:
    total: int = 0
    passed: int = 0
    failed: int = 0
    results: list[UATResult] = field(default_factory=list)

    def record(
        self,
        scenario: str,
        name: str,
        passed: bool,
        duration_ms: float,
        details: str = "",
        error: str | None = None,
    ) -> None:
        self.total += 1
        if passed:
            self.passed += 1
        else:
            self.failed += 1
        self.results.append(
            UATResult(
                scenario=scenario,
                name=name,
                passed=passed,
                duration_ms=duration_ms,
                details=details,
                error=error,
            )
        )


class UATHarness:
    """Executes in-process acceptance scenarios against a fresh control plane."""

    def __init__(self, workspace_dir: Path) -> None:
        self.workspace = workspace_dir
        self.orig_cwd = Path.cwd()
        os.chdir(self.workspace)
        self.db_path = self.workspace / ".bossmode" / "control.db"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.registry = Registry(self.db_path)
        self.report = UATReport()

    def cleanup(self) -> None:
        os.chdir(self.orig_cwd)

    def run_step(self, scenario: str, name: str, fn: Any) -> Any:
        start = time.perf_counter()
        try:
            val = fn()
            duration_ms = (time.perf_counter() - start) * 1000.0
            details = val if isinstance(val, str) else ""
            self.report.record(scenario, name, True, duration_ms, details)
            print(f"  [PASS] {name} ({duration_ms:.1f}ms)")
            return val
        except Exception as exc:
            duration_ms = (time.perf_counter() - start) * 1000.0
            self.report.record(scenario, name, False, duration_ms, error=str(exc))
            print(f"  [FAIL] {name} ({duration_ms:.1f}ms) -> {exc}")
            raise

    def run_all(self) -> bool:
        print("\n==================================================")
        print(" BOSSMODE MVP: IN-PROCESS FUNCTIONAL ACCEPTANCE")
        print("==================================================")
        print(f"Workspace: {self.workspace}")
        print(f"Control DB: {self.db_path}\n")

        try:
            self.scenario_1_happy_path()
            self.scenario_2_rejection_and_remediation()
            self.scenario_3_feedback_promotions()
            self.scenario_4_adversarial_safeguards()
            self.scenario_5_scheduling_and_snapshot_reads()
        except Exception as exc:
            print(f"\n[FATAL] Functional acceptance interrupted by failure: {exc}")
            return False

        self._print_summary()
        return self.report.failed == 0

    def scenario_1_happy_path(self) -> None:
        scenario = "Scenario 1: Happy Path End-to-End Execution"
        print(f"\n--- {scenario} ---")

        # Step 1.1: Initialize Registry
        def step_init() -> str:
            self.registry.initialize()
            assert self.db_path.exists()
            return f"Initialized schema version {SCHEMA_VERSION}"

        self.run_step(scenario, "1.1 Initialize registry database", step_init)

        # Step 1.2: Add Task with Success Criteria
        task: dict[str, Any] = {}

        def step_add_task() -> str:
            nonlocal task
            task = self.registry.create_task(
                title="Generate API Schema",
                goal="Produce OpenAPI 3.0 specification for user service",
                success_criteria="Schema file exists at specs/openapi.json and is valid JSON",
                priority=10,
                permissions={"read": ["docs/*"], "write": ["specs/*"]},
            )
            assert task["state"] == "ready"
            assert task["priority"] == 10
            return f"Task ID: {task['id']}"

        self.run_step(
            scenario, "1.2 Create task with explicit criteria and permissions", step_add_task
        )

        # Step 1.3: Session Reconciliation & Dispatch
        def step_reconcile_dispatch() -> str:
            state = self.registry.reconcile()
            assert state["dispatch"] is not None
            assert state["dispatch"]["id"] == task["id"]
            return f"Dispatched task: {state['dispatch']['id']}"

        self.run_step(
            scenario,
            "1.3 Reconcile session state and select dispatch task",
            step_reconcile_dispatch,
        )

        # Step 1.4: Start Run & Bind Herdr Worker
        run: dict[str, Any] = {}
        binding: dict[str, Any] = {}

        def step_start_and_bind() -> str:
            nonlocal run, binding
            run = self.registry.start_run(
                task["id"],
                agent_role="claude_worker",
                thread_id="claude-thread-001",
                model="claude-3-7-sonnet",
            )
            assert run["status"] == "running"
            task_state = self.registry.get_task(task["id"])["state"]
            assert task_state == "running"

            binding = self.registry.bind_herdr_run(
                run["id"],
                herdr_session="bossmode-session",
                worker_name="worker_api_gen",
                agent_kind="claude",
                status="live",
                session_source="herdr:claude",
                session_agent="claude",
                session_ref_kind="id",
                session_value="claude-native-sess-1",
                pane_id="pane-1",
                tab_id="tab-1",
                workspace_id="ws-main",
            )
            assert binding["native_session"]["value"] == "claude-native-sess-1"
            return f"Run ID: {run['id']}, Worker: {binding['worker_name']}"

        self.run_step(
            scenario, "1.4 Start run and bind Herdr worker with native session", step_start_and_bind
        )

        # Step 1.5: Start Turn & Produce Correlated Turn Result Artifact
        turn: dict[str, Any] = {}

        def step_execute_turn() -> str:
            nonlocal turn
            turn = self.registry.start_turn(
                run["id"],
                purpose="task",
                prompt="Generate the OpenAPI schema for user service",
            )
            assert turn["status"] == "running"
            assert turn["prompt"] == "Generate the OpenAPI schema for user service"

            spec_file = self.workspace / "specs" / "openapi.json"
            spec_file.parent.mkdir(parents=True, exist_ok=True)
            spec_content = {
                "openapi": "3.0.0",
                "info": {"title": "User Service", "version": "1.0.0"},
            }
            spec_file.write_text(json.dumps(spec_content, indent=2))

            result_file = Path(turn["artifact_path"])
            result_file.parent.mkdir(parents=True, exist_ok=True)
            result_payload = {
                "turn_id": turn["id"],
                "status": "succeeded",
                "summary": "Generated valid OpenAPI 3.0 schema at specs/openapi.json",
                "artifacts": [{"path": str(spec_file), "kind": "schema"}],
            }
            result_file.write_text(json.dumps(result_payload, indent=2))

            finished_turn = self.registry.finish_turn(
                turn["id"],
                status="succeeded",
                summary="Generated valid OpenAPI 3.0 schema at specs/openapi.json",
                lifecycle_evidence="done",
            )
            assert finished_turn["status"] == "succeeded"
            assert finished_turn["result"]["turn_id"] == turn["id"]
            return f"Turn ID: {turn['id']} verified"

        self.run_step(
            scenario, "1.5 Execute turn with correlated artifact validation", step_execute_turn
        )

        # Step 1.6: Finish Run (Moves Task to evaluating)
        def step_finish_run() -> str:
            finished_run = self.registry.finish_run(
                run["id"],
                outcome="succeeded",
                summary="API schema generated and verified by worker",
                artifacts=[{"path": "specs/openapi.json", "kind": "schema"}],
            )
            assert finished_run["status"] == "finished"
            assert finished_run["herdr_binding"]["status"] == "stale"

            task_now = self.registry.get_task(task["id"])
            assert task_now["state"] == "evaluating"
            return f"Run finished; Task state: {task_now['state']}"

        self.run_step(
            scenario, "1.6 Finish run into evaluating state & stale Herdr binding", step_finish_run
        )

        # Step 1.7: Independent Evaluator Verification Pass
        def step_independent_eval_pass() -> str:
            eval_record = self.registry.add_evaluation(
                task["id"],
                run_id=run["id"],
                evaluator="codex_reviewer",
                passed=True,
                score=1.0,
                evidence="Verified specs/openapi.json against OpenAPI 3.0 schema validator",
                notes="Clean schema structure and complete field types",
            )
            assert eval_record["passed"] == 1

            task_final = self.registry.get_task(task["id"])
            assert task_final["state"] == "succeeded"
            assert [e["event_type"] for e in task_final["events"]] == [
                "created",
                "run_started",
                "run_finished",
                "evaluated",
            ]
            msg = (
                f"Evaluation passed by {eval_record['evaluator']}; "
                f"Task state: {task_final['state']}"
            )
            return msg

        self.run_step(
            scenario,
            "1.7 Independent evaluator verifies and moves task to succeeded",
            step_independent_eval_pass,
        )

    def scenario_2_rejection_and_remediation(self) -> None:
        scenario = "Scenario 2: Evaluator Rejection & Multi-Turn Remediation"
        print(f"\n--- {scenario} ---")

        task: dict[str, Any] = {}
        run_1: dict[str, Any] = {}

        # Step 2.1: Create Task and Dispatch Run 1
        def step_create_and_dispatch() -> str:
            nonlocal task, run_1
            task = self.registry.create_task(
                title="Implement Rate Limiting",
                goal="Implement token bucket algorithm in auth middleware",
                success_criteria="Token bucket handles burst and enforces rate limits",
            )
            run_1 = self.registry.start_run(task["id"], agent_role="worker_claude")
            self.registry.bind_herdr_run(
                run_1["id"],
                herdr_session="bossmode-session",
                worker_name="worker_ratelimit",
                agent_kind="claude",
            )
            return f"Task ID: {task['id']}, Run 1: {run_1['id']}"

        self.run_step(scenario, "2.1 Create task and dispatch Run 1", step_create_and_dispatch)

        # Step 2.2: Run 1 produces defective output
        def step_run_1_defect() -> str:
            turn_1 = self.registry.start_turn(
                run_1["id"], purpose="task", prompt="Write rate limiter"
            )
            result_file = Path(turn_1["artifact_path"])
            result_file.parent.mkdir(parents=True, exist_ok=True)
            result_file.write_text(
                json.dumps(
                    {
                        "turn_id": turn_1["id"],
                        "status": "succeeded",
                        "summary": "Implemented rate limiter without burst capacity",
                        "artifacts": [],
                    }
                )
            )
            self.registry.finish_turn(turn_1["id"], status="succeeded")
            self.registry.finish_run(
                run_1["id"], outcome="succeeded", summary="Worker claims rate limiter done"
            )
            assert self.registry.get_task(task["id"])["state"] == "evaluating"
            return "Run 1 finished into evaluating"

        self.run_step(scenario, "2.2 Run 1 finishes with subtle defect", step_run_1_defect)

        # Step 2.3: Evaluator rejects Run 1
        def step_evaluator_rejects() -> str:
            eval_record = self.registry.add_evaluation(
                task["id"],
                run_id=run_1["id"],
                evaluator="reviewer_codex",
                passed=False,
                evidence=(
                    "Burst capacity test failed: requests rejected prematurely during burst window"
                ),
            )
            assert eval_record["passed"] == 0
            task_state = self.registry.get_task(task["id"])["state"]
            assert task_state == "failed"
            return f"Task transitioned to {task_state}"

        self.run_step(
            scenario,
            "2.3 Independent evaluator rejects Run 1 -> task marked failed",
            step_evaluator_rejects,
        )

        # Step 2.4: Ingest Feedback & Transition Task for Retry
        def step_ingest_feedback_and_retry() -> str:
            self.registry.add_feedback(
                task["id"],
                run_id=run_1["id"],
                kind="correction",
                recurrence_key="ratelimit.burst-support",
                content="Token bucket must account for burst capacity parameter",
            )
            transitioned = self.registry.transition_task(
                task["id"],
                "ready",
                actor="supervisor",
                reason="Retry rate limiting with burst support",
                next_action="Re-run worker with burst support guidance",
            )
            assert transitioned["state"] == "ready"
            assert transitioned["next_action"] == "Re-run worker with burst support guidance"
            return "Feedback logged; task transitioned back to ready"

        self.run_step(
            scenario,
            "2.4 Ingest correction feedback and transition task to ready",
            step_ingest_feedback_and_retry,
        )

        # Step 2.5: Dispatch Run 2 with same worker & Remediate
        run_2: dict[str, Any] = {}

        def step_run_2_remediation() -> str:
            nonlocal run_2
            run_2 = self.registry.start_run(task["id"], agent_role="worker_claude")
            self.registry.bind_herdr_run(
                run_2["id"],
                herdr_session="bossmode-session",
                worker_name="worker_ratelimit",
                agent_kind="claude",
            )
            turn_2 = self.registry.start_turn(
                run_2["id"],
                purpose="correction",
                prompt="Fix rate limiter to support burst capacity parameter",
            )
            result_file = Path(turn_2["artifact_path"])
            result_file.write_text(
                json.dumps(
                    {
                        "turn_id": turn_2["id"],
                        "status": "succeeded",
                        "summary": "Fixed rate limiter: added burst bucket support and unit tests",
                        "artifacts": [{"path": "auth/ratelimit.py", "kind": "code"}],
                    }
                )
            )
            self.registry.finish_turn(
                turn_2["id"],
                status="succeeded",
                summary="Fixed rate limiter: added burst bucket support and unit tests",
            )
            self.registry.finish_run(
                run_2["id"], outcome="succeeded", summary="Fixed rate limiter with burst support"
            )
            assert self.registry.get_task(task["id"])["state"] == "evaluating"
            return f"Run 2 ({run_2['id']}) finished into evaluating"

        self.run_step(
            scenario,
            "2.5 Run 2 executes correction turn and finishes into evaluating",
            step_run_2_remediation,
        )

        # Step 2.6: Evaluator passes Run 2
        def step_evaluator_passes_run_2() -> str:
            eval_record = self.registry.add_evaluation(
                task["id"],
                run_id=run_2["id"],
                evaluator="reviewer_codex",
                passed=True,
                score=1.0,
                evidence="All burst capacity tests and concurrency tests passed",
            )
            assert eval_record["passed"] == 1
            assert self.registry.get_task(task["id"])["state"] == "succeeded"
            return "Task succeeded after remediation"

        self.run_step(
            scenario, "2.6 Evaluator passes Run 2 -> task succeeded", step_evaluator_passes_run_2
        )

    def scenario_3_feedback_promotions(self) -> None:
        scenario = "Scenario 3: Feedback Promotion Ladder"
        print(f"\n--- {scenario} ---")

        task: dict[str, Any] = {}

        # Step 3.1: Record diverse feedback kinds
        def step_log_feedback_ladder() -> str:
            nonlocal task
            task = self.registry.create_task(
                title="Learning Pipeline Task",
                goal="Trigger multi-layer promotion proposals",
                success_criteria="Proposals generated for control, skill, and memory",
            )
            # Control layer trigger: 2 failures
            self.registry.add_feedback(
                task["id"],
                kind="failure",
                recurrence_key="env.missing-key",
                content="API key missing in environment",
            )
            self.registry.add_feedback(
                task["id"],
                kind="failure",
                recurrence_key="env.missing-key",
                content="API key missing on retry",
            )

            # Memory layer trigger: preference
            self.registry.add_feedback(
                task["id"],
                kind="preference",
                recurrence_key="format.concise-diffs",
                content="Prefer concise diffs over full file rewrites",
            )

            # Skill layer trigger: 2 corrections + passing eval
            self.registry.add_feedback(
                task["id"],
                kind="correction",
                recurrence_key="git.rebase-first",
                content="Always rebase before creating branch",
            )
            self.registry.add_feedback(
                task["id"],
                kind="correction",
                recurrence_key="git.rebase-first",
                content="Rebase on main before opening PR",
            )
            return "Logged 2 failures (control), 1 preference (memory), 2 corrections (skill)"

        self.run_step(
            scenario,
            "3.1 Log feedback across control, skill, and memory categories",
            step_log_feedback_ladder,
        )

        # Step 3.2: Verify skill requires passing evaluation before proposal
        def step_verify_skill_gate() -> str:
            proposals = self.registry.propose_promotions()
            targets = {p["target_layer"] for p in proposals}
            assert "control" in targets
            assert "memory" in targets
            assert "skill" not in targets, "Skill proposed without passing evaluation!"
            return "Skill proposal correctly withheld until passing evaluation"

        self.run_step(
            scenario,
            "3.2 Verify skill proposal requires passing evaluation evidence",
            step_verify_skill_gate,
        )

        # Step 3.3: Add passing evaluation and verify skill proposal created
        def step_add_eval_and_propose_skill() -> str:
            run = self.registry.start_run(task["id"], agent_role="worker")
            self.registry.finish_run(run["id"], outcome="succeeded", summary="Workflow completed")
            self.registry.add_evaluation(
                task["id"],
                run_id=run["id"],
                evaluator="reviewer",
                passed=True,
                evidence="Rebase workflow tested cleanly",
            )
            proposals = self.registry.propose_promotions()
            skill_props = [p for p in proposals if p["target_layer"] == "skill"]
            assert len(skill_props) == 1
            assert (
                "Repeated correction appeared 2 times with 1 passing evaluation"
                in skill_props[0]["rationale"]
            )
            return f"Skill proposal created: {skill_props[0]['id']}"

        self.run_step(
            scenario,
            "3.3 Add passing evaluation and verify skill proposal generated",
            step_add_eval_and_propose_skill,
        )

        # Step 3.4: User Authorization Gating: proposed -> accepted -> applied
        def step_user_gating() -> str:
            proposals = self.registry.list_promotions("proposed")
            control_prop = next(p for p in proposals if p["target_layer"] == "control")

            # Cannot jump proposed -> applied directly
            try:
                self.registry.set_promotion_status(control_prop["id"], "applied")
                raise AssertionError("Allowed invalid promotion transition proposed -> applied")
            except RegistryError:
                pass

            accepted = self.registry.set_promotion_status(control_prop["id"], "accepted")
            assert accepted["status"] == "accepted"
            applied = self.registry.set_promotion_status(control_prop["id"], "applied")
            assert applied["status"] == "applied"
            return f"Control proposal {control_prop['id']} transitioned to applied"

        self.run_step(
            scenario,
            "3.4 User approval gate enforces proposed -> accepted -> applied",
            step_user_gating,
        )

        # Step 3.5: Re-propose rejected promotion upon new feedback
        def step_repropose_rejected() -> str:
            proposals = self.registry.list_promotions("proposed")
            memory_prop = next(p for p in proposals if p["target_layer"] == "memory")
            rejected = self.registry.set_promotion_status(memory_prop["id"], "rejected")
            assert rejected["status"] == "rejected"

            # Adding new feedback with same recurrence key allows re-proposing
            self.registry.add_feedback(
                task["id"],
                kind="preference",
                recurrence_key=memory_prop["recurrence_key"],
                content="Confirmed preference for concise diffs",
            )
            reproposed = self.registry.propose_promotions()
            reproposed_mem = [p for p in reproposed if p["id"] == memory_prop["id"]]
            assert len(reproposed_mem) == 1
            assert reproposed_mem[0]["status"] == "proposed"
            return "Previously rejected promotion re-proposed upon new feedback"

        self.run_step(
            scenario, "3.5 Re-propose rejected promotion upon new feedback", step_repropose_rejected
        )

    def scenario_4_adversarial_safeguards(self) -> None:
        scenario = "Scenario 4: Adversarial Traps & Rejection Safeguards"
        print(f"\n--- {scenario} ---")

        # Trap 4.1: Self-evaluation rejection
        def trap_self_eval() -> str:
            t = self.registry.create_task(
                title="Self Eval Task", goal="Test trap", success_criteria="Pass"
            )
            r = self.registry.start_run(t["id"], agent_role="worker_charlie")
            self.registry.finish_run(r["id"], outcome="succeeded", summary="done")
            try:
                self.registry.add_evaluation(
                    t["id"],
                    run_id=r["id"],
                    evaluator="worker_charlie",
                    passed=True,
                    evidence="Self review",
                )
                raise AssertionError("Self-evaluation was allowed!")
            except RegistryError as err:
                assert "independent" in str(err)
            finally:
                self.registry.transition_task(
                    t["id"], "archived", actor="supervisor", reason="cleanup"
                )
            return "Self-evaluation rejected"

        self.run_step(
            scenario, "4.1 Reject self-evaluation when evaluator == run.agent_role", trap_self_eval
        )

        # Trap 4.2: Premature evaluation rejection
        def trap_premature_eval() -> str:
            t = self.registry.create_task(
                title="Premature Eval Task", goal="Test trap", success_criteria="Pass"
            )
            r = self.registry.start_run(t["id"], agent_role="worker")
            try:
                self.registry.add_evaluation(
                    t["id"],
                    run_id=r["id"],
                    evaluator="reviewer",
                    passed=True,
                    evidence="Early eval",
                )
                raise AssertionError("Premature evaluation was allowed on running task!")
            except RegistryError as err:
                assert "evaluating state" in str(err)
            finally:
                self.registry.finish_run(r["id"], outcome="failed", summary="cleanup")
            return "Premature evaluation rejected"

        self.run_step(
            scenario,
            "4.2 Reject evaluation when task is not in evaluating state",
            trap_premature_eval,
        )

        # Trap 4.3: Succeeded run with zero succeeded turns
        def trap_succeeded_run_with_failed_turns() -> str:
            t = self.registry.create_task(
                title="Failed Turns Task", goal="Test trap", success_criteria="Pass"
            )
            r = self.registry.start_run(t["id"], agent_role="worker")
            self.registry.bind_herdr_run(
                r["id"], herdr_session="s", worker_name="w_fail", agent_kind="claude"
            )
            turn = self.registry.start_turn(r["id"], purpose="task", prompt="do work")
            self.registry.finish_turn(turn["id"], status="failed", summary="crashed")
            try:
                self.registry.finish_run(r["id"], outcome="succeeded", summary="fraudulent success")
                raise AssertionError("Allowed succeeded run when all turns failed!")
            except RegistryError as err:
                assert "requires at least one succeeded turn" in str(err)
            finally:
                self.registry.finish_run(r["id"], outcome="failed", summary="cleanup")
            return "Fraudulent succeeded run rejected"

        self.run_step(
            scenario,
            "4.3 Reject finish_run(succeeded) when all turns failed",
            trap_succeeded_run_with_failed_turns,
        )

        # Trap 4.4: Markdown code fence in turn result JSON
        def trap_markdown_fences() -> str:
            t = self.registry.create_task(
                title="Fence Turn Task", goal="Test trap", success_criteria="Pass"
            )
            r = self.registry.start_run(t["id"], agent_role="worker")
            self.registry.bind_herdr_run(
                r["id"], herdr_session="s", worker_name="w_fence", agent_kind="claude"
            )
            turn = self.registry.start_turn(r["id"], purpose="task", prompt="do work")
            result_file = Path(turn["artifact_path"])
            result_file.parent.mkdir(parents=True, exist_ok=True)
            result_file.write_text(
                f'```json\n{{"turn_id": "{turn["id"]}", "status": "succeeded"}}\n```'
            )
            try:
                self.registry.finish_turn(turn["id"], status="succeeded")
                raise AssertionError("Allowed markdown-fenced turn result!")
            except RegistryError as err:
                assert "markdown code fence" in str(err)
            finally:
                self.registry.finish_turn(turn["id"], status="failed", summary="fences rejected")
                self.registry.finish_run(r["id"], outcome="failed", summary="cleanup")
            return "Markdown-fenced turn result rejected with diagnostic message"

        self.run_step(
            scenario,
            "4.4 Detect and reject markdown code fences in turn result JSON",
            trap_markdown_fences,
        )

        # Trap 4.5: Oversized turn result payload
        def trap_oversized_payload() -> str:
            t = self.registry.create_task(
                title="Oversized Task", goal="Test trap", success_criteria="Pass"
            )
            r = self.registry.start_run(t["id"], agent_role="worker")
            self.registry.bind_herdr_run(
                r["id"], herdr_session="s", worker_name="w_size", agent_kind="claude"
            )
            turn = self.registry.start_turn(r["id"], purpose="task", prompt="do work")
            result_file = Path(turn["artifact_path"])
            result_file.write_bytes(b"x" * (MAX_TURN_RESULT_BYTES + 1))
            try:
                self.registry.finish_turn(turn["id"], status="succeeded")
                raise AssertionError("Allowed oversized turn result payload!")
            except RegistryError as err:
                assert "exceeds" in str(err)
            finally:
                self.registry.finish_turn(turn["id"], status="failed", summary="oversized payload")
                self.registry.finish_run(r["id"], outcome="failed", summary="cleanup")
            return f"Payload exceeding {MAX_TURN_RESULT_BYTES} bytes rejected"

        self.run_step(
            scenario, "4.5 Reject turn results exceeding 1 MiB limit", trap_oversized_payload
        )

        # Trap 4.6: Worker collision on concurrent runs
        def trap_worker_collision() -> str:
            t1 = self.registry.create_task(title="Task 1", goal="Goal 1", success_criteria="Pass")
            t2 = self.registry.create_task(title="Task 2", goal="Goal 2", success_criteria="Pass")
            r1 = self.registry.start_run(t1["id"], agent_role="worker")
            r2 = self.registry.start_run(t2["id"], agent_role="worker")
            self.registry.bind_herdr_run(
                r1["id"], herdr_session="s", worker_name="worker_shared", agent_kind="claude"
            )
            try:
                self.registry.bind_herdr_run(
                    r2["id"], herdr_session="s", worker_name="worker_shared", agent_kind="claude"
                )
                raise AssertionError("Allowed duplicate active worker binding!")
            except RegistryError as err:
                assert "already bound" in str(err)
            finally:
                self.registry.finish_run(r1["id"], outcome="failed", summary="cleanup")
                self.registry.finish_run(r2["id"], outcome="failed", summary="cleanup")
            return "Active worker collision rejected"

        self.run_step(
            scenario,
            "4.6 Reject duplicate worker name binding to concurrent active runs",
            trap_worker_collision,
        )

        # Trap 4.7: Reject manual stale status on bind_herdr_run
        def trap_manual_stale_binding() -> str:
            t = self.registry.create_task(
                title="Stale Bind Task", goal="Goal", success_criteria="Pass"
            )
            r = self.registry.start_run(t["id"], agent_role="worker")
            try:
                self.registry.bind_herdr_run(
                    r["id"],
                    herdr_session="s",
                    worker_name="w_stale",
                    agent_kind="claude",
                    status="stale",
                )
                raise AssertionError("Allowed manual stale status assignment!")
            except RegistryError as err:
                assert "cannot manually set binding status to stale" in str(err)
            finally:
                self.registry.finish_run(r["id"], outcome="failed", summary="cleanup")
            return "Manual stale status assignment rejected"

        self.run_step(
            scenario,
            "4.7 Reject manual stale status assignment via bind_herdr_run",
            trap_manual_stale_binding,
        )

    def scenario_5_scheduling_and_snapshot_reads(self) -> None:
        scenario = "Scenario 5: Scheduling & Snapshot Consistency"
        print(f"\n--- {scenario} ---")

        # Step 5.1: Verify single-flight dispatch (paused while active)
        t_active: dict[str, Any] = {}
        t_ready: dict[str, Any] = {}
        r_active: dict[str, Any] = {}

        def step_single_flight() -> str:
            nonlocal t_active, t_ready, r_active
            t_active = self.registry.create_task(
                title="Active Task", goal="Run", success_criteria="Pass", priority=1
            )
            t_ready = self.registry.create_task(
                title="Ready High Priority", goal="Wait", success_criteria="Pass", priority=100
            )
            r_active = self.registry.start_run(t_active["id"], agent_role="worker")

            state = self.registry.reconcile()
            assert state["dispatch"] is None, (
                f"Dispatched task {state['dispatch']} while another task was active!"
            )
            assert len(state["active"]) == 1
            assert state["active"][0]["id"] == t_active["id"]
            return "Dispatch paused while active task is running"

        self.run_step(
            scenario,
            "5.1 Verify single-flight pause during active execution",
            step_single_flight,
        )

        # Step 5.2: Priority sorting when queue clears
        def step_priority_dispatch() -> str:
            self.registry.finish_run(
                r_active["id"], outcome="failed", summary="completed active task"
            )

            state = self.registry.reconcile()
            assert state["dispatch"] is not None
            assert state["dispatch"]["id"] == t_ready["id"]
            assert state["dispatch"]["priority"] == 100
            msg = (
                f"Selected highest priority ready task: {state['dispatch']['id']} "
                f"(priority {state['dispatch']['priority']})"
            )
            return msg

        self.run_step(
            scenario,
            "5.2 Dispatch selects highest priority ready task when queue clears",
            step_priority_dispatch,
        )

        # Step 5.3: CLI naked and subcommand execution
        def step_cli_execution() -> str:
            db_arg = str(self.db_path)
            # Naked execution (default reconcile)
            assert cli_main(["--db", db_arg]) == 0
            assert cli_main(["--db", db_arg, "task", "list"]) == 0
            assert cli_main(["--db", db_arg, "promotion", "list"]) == 0
            return "Naked CLI and subcommands executed cleanly"

        self.run_step(
            scenario,
            "5.3 Exercise naked CLI and subcommands against in-process registry",
            step_cli_execution,
        )

    def _print_summary(self) -> None:
        print("\n==================================================")
        print(" FUNCTIONAL ACCEPTANCE SCORECARD")
        print("==================================================")
        print(f"Total Checks:  {self.report.total}")
        print(f"Passed:        {self.report.passed}")
        print(f"Failed:        {self.report.failed}")
        success_rate = (
            (self.report.passed / self.report.total * 100.0) if self.report.total else 0.0
        )
        print(f"Success Rate:  {success_rate:.1f}%\n")

        if self.report.failed == 0:
            print(">>> ALL FUNCTIONAL ACCEPTANCE CHECKS PASSED <<<")
        else:
            print(">>> FUNCTIONAL ACCEPTANCE FAILED <<<")
            for res in self.report.results:
                if not res.passed:
                    print(f"  FAILED: [{res.scenario}] {res.name} -> {res.error}")
        print("==================================================\n")


def main() -> int:
    temp_dir = tempfile.mkdtemp(prefix="bossmode_uat_")
    harness = None
    try:
        harness = UATHarness(Path(temp_dir))
        success = harness.run_all()
        return 0 if success else 1
    finally:
        if harness:
            harness.cleanup()
        shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
