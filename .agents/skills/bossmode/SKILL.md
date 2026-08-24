---
name: bossmode
description: Manage, dispatch, and continue tasks using the Bossmode durable control plane. Reconcile runtime state, orchestrate native subagents (AGY, Codex) or external Herdr workers (pi, codex, claude, agy, grok, muse), enforce independent evaluation gates, and propose gated promotions.
---

# Bossmode

Operate from the repository root. The registry is `.bossmode/control.db` unless the user names a
different database.

## 1. Reconcile

1. Run `uv run bossmode init` idempotently.
2. Run `uv run bossmode supervisor tick` and parse its JSON output.
3. Use the nested run, binding, and turn records in `active` and `needs_evaluation` to recover after
   interruption. Use `bossmode run show RUN_ID` or `bossmode turn show TURN_ID` for exact records.
4. For every active registry task, reconcile its executor against live state before sending,
   interrupting, completing, or closing it. Use live runtime state for native subagents (AGY, Codex)
   and `herdr agent get/list` for Herdr workers (`pi`, `codex`, `claude`, `agy`, `grok`, `muse`).
   Treat missing, foreign, or ambiguous identity as a blocker; stored IDs are indexes, not capabilities.
5. Surface `needs_user` and `blocked` items before starting new work when they affect priority or
   safety. Ask only for the decision the system cannot safely make.

## 2. Dispatch

1. Select only the single task returned in `tick.dispatch`.
2. Choose the narrowest role: `researcher` for read-heavy evidence, `worker` for authorized edits,
   or `reviewer` for independent evaluation.
3. Give the agent the task ID, goal, success criteria, permission limits, relevant evidence, and
   required structured return format.
4. For a native subagent (e.g. AGY, Codex), spawn it and record the returned thread or task ID with
   `bossmode run start TASK_ID --role ROLE --thread-id NATIVE_ID`.
5. For a Herdr worker, reserve the run with `bossmode run start` before creating any layout.
   Derive a unique worker name from the run ID, create it through the official Herdr CLI, then call
   `bossmode herdr bind`. If binding fails after creation, reconcile the deterministic name;
   do not create another worker or close by a stored pane ID.
6. Use Herdr only when the task explicitly requests an external interactive agent. Do not add an
   agent router or choose providers from historical scores in this spike.
7. Do not run parallel writers against the same paths.

## 3. Correlate Herdr Turns

Read `docs/agent-workflow.md` before the first external run. Its command sequence and result schema
are canonical.

1. Use the official, release-matched Herdr commands; never invoke legacy wrappers.
2. Require one unambiguous live worker matching the binding and wait until it is `idle` or `done`.
   `agent prompt --wait` is lifecycle-based and cannot identify a turn that began while the worker
   was already busy.
3. Call `bossmode turn start RUN_ID --purpose PURPOSE --prompt PROMPT`, put its `turn_id` and
   `artifact_path` in the worker prompt envelope, then prompt through Herdr. Only one turn may
   remain open per run.
4. Run `herdr agent prompt NAME ENVELOPE --wait` and inspect `blocked`, stalled, unknown, and error
   states separately. Never approve a trust or permission dialog without explicit user authority.
5. Call `bossmode turn finish TURN_ID --status succeeded` only after the worker settles. It reads
   and validates the exact result path; on rejection, preserve `failed` or `unknown` with explicit
   evidence. Never infer success or a path from terminal text.
6. Re-run `herdr agent get` after the first session-bearing event and bind the structured native
   session reference `{source, agent, kind, value}`. Refuse a different native reference for the
   same run.

## 4. Complete and Evaluate

1. When the agent finishes, record the run outcome, summary, artifact manifest, model/runtime
   information, retries, and timing with `bossmode run finish`.
2. A successful run is not a passing evaluation; it moves the task to `evaluating`.
3. Require an independent evaluation (`reviewer` role or user); self-evaluation is rejected.
4. Record the evaluation with `bossmode evaluate TASK_ID --run-id RUN_ID --evaluator REVIEWER --passed/--failed --evidence EVIDENCE`;
   only a passing evaluation moves the task to `succeeded`.
5. Record explicit user corrections or preferences with `bossmode feedback TASK_ID --kind KIND --key KEY --content CONTENT`.
   Choose a stable, narrowly scoped recurrence key.

## 5. Gated Promotion

1. Run `bossmode supervisor tick` after feedback or evaluation to compute promotion proposals.
2. Present promotion proposals with their evidence and intended layer:
   - `control`: deterministic code/rules/hooks for repeated failures (2+);
   - `skill`: reusable workflow for repeated corrections with passing evals (2+);
   - `memory`: contextual preference or observation for future retrieval.
3. Do not accept, apply, or implement a promotion without explicit user authorization.
4. Enforce the approval lifecycle: `proposed -> accepted -> applied`. Record `accepted` before
   implementation and `applied` only after verification.

## 6. Report

Return only material state changes: dispatched work, completed and evaluated results, user decisions,
new blockers, and promotion proposals. Include exact task, run, turn, evaluation, and promotion IDs.
