# Continual Agent MVP

A Codex-native spike for the system described in Will Brown's thread: one long-running
supervisor task, bounded specialist agents, durable task state, evidence-backed evaluation,
and gated promotion of repeated feedback into memories, skills, or deterministic controls.

The model does not update its weights. The surrounding system improves by retrieving prior
context, recording outcomes, refining reusable workflows, and enforcing recurring rules in code.

## What this spike implements

- A transactional SQLite task and thread registry.
- Enforced task and promotion state machines.
- Agent-run records with model, effort, token, retry, timing, and artifact fields.
- Independent evaluations and structured feedback.
- Conservative promotion proposals:
  - preference or observation -> memory
  - repeated correction with passing evidence -> skill
  - repeated failure -> deterministic control
- A `supervisor tick` that returns the next ready task, active work, user decisions, blockers,
  and proposed learning.
- Codex project instructions, a supervisor skill, and researcher/worker/reviewer agent roles.

It deliberately does **not** implement an LLM API wrapper, its own scheduler, or a generic agent
backend. Codex already supplies the chat, goal, subagent, task, tool, and scheduled-task runtime.

## Quick start

```bash
cd ~/Developer/continual-agent-mvp
uv sync
uv run continual-agent init

uv run continual-agent task add \
  --title "Create the first capability report" \
  --goal "Summarize what the spike can and cannot do" \
  --success-criteria "A source-backed report with explicit limitations" \
  --priority 10 \
  --permissions-json '{"filesystem":"workspace-write","network":false}'

uv run continual-agent supervisor tick
```

Every command returns JSON. Use the returned task and run IDs in later commands:

```bash
uv run continual-agent run start TASK_ID \
  --role researcher \
  --thread-id CODEX_THREAD_ID

uv run continual-agent run finish RUN_ID \
  --outcome succeeded \
  --summary "Produced and checked the capability report" \
  --artifacts-json '[{"path":"reports/capabilities.md","kind":"report"}]'

uv run continual-agent evaluate TASK_ID \
  --run-id RUN_ID \
  --evaluator reviewer \
  --passed \
  --score 0.9 \
  --evidence "All claims link to registry or test evidence"

uv run continual-agent feedback TASK_ID \
  --kind preference \
  --key reports.explicit-limitations \
  --content "Always separate demonstrated capability from inference"

uv run continual-agent supervisor tick
```

## Run it as a Codex experiment

1. Open this directory as a Codex project.
2. Start a long-running goal in one pinned task:

   ```text
   /goal Use $supervisor to manage the MVP registry. Dispatch at most one writing agent at a
   time, require an independent evaluation before calling work learned, and never apply a
   promotion without my explicit approval.
   ```

3. Give that task requests and corrections normally. The supervisor skill tells Codex how to
   translate them into registry operations and subagent work.
4. After the manual loop is trustworthy, schedule a recurrence inside that same task with a
   durable prompt such as: `Run one $supervisor reconciliation tick. Report only user decisions,
   new blockers, completed evaluations, or promotion proposals.`

The stored `owner_thread_id` is an index, not proof that an agent is still alive or owned by the
current task. The supervisor must reconcile it with live Codex task state before sending,
interrupting, or closing anything.

## State model

```text
backlog -> ready -> running -> evaluating -> succeeded -> archived
                   |              \-> failed -> ready
                   |-> blocked -> ready
                   \-> waiting_user -> ready/running
```

Task transitions and dispatch use conditional SQLite updates inside `BEGIN IMMEDIATE`
transactions. Concurrent supervisors therefore fail instead of silently overwriting state.

Promotions have a separate approval path:

```text
proposed -> accepted -> applied
        \-> rejected
```

Successful execution enters `evaluating`; only a recorded passing evaluation enters `succeeded`.

`propose` never edits memories, skills, `AGENTS.md`, or enforcement code. Applying an accepted
proposal remains an explicit, reviewable user-authorized change.

## Verification

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

The first meaningful product experiment should compare a repeated workflow with and without the
registry/skill loop. Measure correction rate, success rate, retries, time, and cost rather than
relying on the supervisor's self-description.
