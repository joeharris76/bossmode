---
name: supervisor
description: Reconcile the continual-agent MVP registry, dispatch one bounded Codex or Herdr worker, record correlated evidence, and propose gated learning. Use when managing, continuing, or checking MVP tasks.
---

# Supervisor

Operate from the repository root. The registry is `.continual/control.db` unless the user names a
different database.

## Reconcile

1. Run `uv run continual-agent init` idempotently.
2. Run `uv run continual-agent supervisor tick` and parse its JSON.
3. Use the nested run, binding, and turn records in `active` and `needs_evaluation` to recover after
   interruption. Use `run show` or `turn show` for one exact record.
4. For every active registry task, reconcile its executor against live state before sending,
   interrupting, completing, or closing it. Use live Codex task state for Codex subagents and
   `herdr agent get/list` for Herdr workers. Treat missing, foreign, or ambiguous identity as a
   blocker; stored IDs are indexes, not capabilities.
5. Surface `needs_user` and `blocked` items before starting new work when they affect priority or
   safety. Ask only for the decision the system cannot safely make.

## Dispatch

1. Select only the single task returned in `dispatch`.
2. Choose the narrowest role: `researcher` for read-heavy evidence, `worker` for authorized edits,
   or `reviewer` for independent evaluation.
3. Give the agent the task ID, goal, success criteria, permission limits, relevant evidence, and
   required structured return fields.
4. For a Codex subagent, spawn it and record the returned task with `continual-agent run start`.
5. For a Herdr worker, reserve the run with `continual-agent run start` before creating any layout.
   Derive a unique worker name from the run ID, create it through the official Herdr CLI, then call
   `continual-agent herdr bind`. If binding fails after creation, reconcile the deterministic name;
   do not create another worker or close by a stored pane ID.
6. Use Herdr only when the task explicitly requests an external interactive agent. Do not add an
   agent router or choose providers from historical scores in this spike.
7. Do not run parallel writers against the same paths.

## Herdr turns

Read `docs/agent-workflow.md` before the first external run. Its command sequence and result schema
are canonical.

1. Use the official, release-matched Herdr commands; never invoke `herdr-orch`.
2. Require one unambiguous live worker matching the binding and wait until it
   is `idle` or `done`. `agent prompt --wait` is lifecycle-based and cannot identify a turn that
   began while the worker was already busy.
3. Call `turn start`, put its `turn_id` and `artifact_path` in the worker prompt, then prompt through
   Herdr. Only one turn may remain open.
4. Run `herdr agent prompt NAME TEXT --wait` and inspect `blocked`, stalled, unknown, and error
   states separately. Never approve a trust or permission dialog without explicit user authority.
5. Call `turn finish --status succeeded` only after the worker settles. It reads and validates the
   exact result path; on rejection, preserve `failed` or `unknown` with explicit evidence. Never
   infer success or a path from terminal text.
6. Re-run `herdr agent get` after the first session-bearing event and bind the structured native
   session reference `{source, agent, kind, value}`. Refuse a different native reference for the
   same run.

## Complete and evaluate

1. When the agent finishes, record the run outcome, summary, artifact manifest, model/runtime
   information available to you, retries, and timing.
2. A successful run is not a passing evaluation. Use deterministic checks when possible; otherwise
   ask a separate `reviewer` agent or the user to evaluate it.
3. A successful run moves the task to `evaluating`. Record the evaluation with
   `continual-agent evaluate`, citing the evidence actually checked; only a passing evaluation
   moves the task to `succeeded`.
4. Record explicit user corrections or preferences with `continual-agent feedback`. Choose a
   stable, narrowly scoped recurrence key.

## Promote carefully

1. Run another supervisor tick after feedback or evaluation.
2. Present promotion proposals with their evidence and intended layer.
3. Do not accept, apply, or implement a promotion without explicit user authorization.
4. Once authorized, prefer the smallest durable owner:
   - memory for contextual preference or observation;
   - a tested skill for a repeatable judgment or procedure;
   - deterministic code, tests, or hooks for a recurring operational failure.
5. Record `accepted` before implementation and `applied` only after verification.

## Report

Return only material state changes: dispatched work, completed and evaluated results, user decisions,
new blockers, and promotion proposals. Include exact task, run, turn, evaluation, and promotion IDs.
