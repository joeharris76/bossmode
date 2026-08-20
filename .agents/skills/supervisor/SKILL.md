---
name: supervisor
description: Reconcile the continual-agent MVP registry, dispatch one bounded Codex subagent, record evidence, and propose gated learning. Use when managing, continuing, or checking MVP tasks.
---

# Supervisor

Operate from the repository root. The registry is `.continual/control.db` unless the user names a
different database.

## Reconcile

1. Run `uv run continual-agent init` idempotently.
2. Run `uv run continual-agent supervisor tick` and parse its JSON.
3. For every active registry task, inspect live Codex task state before sending, interrupting,
   completing, or closing it. Treat a missing, foreign, or ambiguous thread as a blocker; do not
   infer ownership from the stored ID.
4. Surface `needs_user` and `blocked` items before starting new work when they affect priority or
   safety. Ask only for the decision the system cannot safely make.

## Dispatch

1. Select only the single task returned in `dispatch`.
2. Choose the narrowest role: `researcher` for read-heavy evidence, `worker` for authorized edits,
   or `reviewer` for independent evaluation.
3. Give the agent the task ID, goal, success criteria, permission limits, relevant evidence, and
   required structured return fields.
4. After a successful spawn, record it with `continual-agent run start`. If the registry rejects
   dispatch, do not work around the state machine.
5. Do not run parallel writers against the same paths.

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
new blockers, and promotion proposals. Include exact task, run, evaluation, and promotion IDs.
