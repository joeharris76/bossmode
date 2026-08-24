# Bossmode MVP

A lightweight, durable control plane and supervisor harness for autonomous agents. Bossmode coordinates long-running supervisor workflows, bounded specialist tasks, turn-correlated execution, independent evaluation, and gated promotion of feedback into memories, skills, or deterministic controls.

The model does not update its weights. The surrounding system improves by persisting context, recording evidence-backed outcomes, refining reusable procedures, and enforcing recurring rules in code.

---

## Core Capabilities

- **Durable Control Plane**: Transactional SQLite registry (`.bossmode/control.db`) with zero external runtime dependencies.
- **Supervisor Orchestration**: Single-flight task dispatch (`bossmode supervisor tick`) preventing concurrent write collisions.
- **Runtime & Coordinator Agnostic**: Any agent (Antigravity/AGY, Codex, Claude, Pi, Grok, Muse) can act as coordinator or execute bounded worker runs.
- **Dual Execution Modes**:
  - _Native Subagents_: Dispatched via host runtime tools (Codex subagents, AGY `invoke_subagent`).
  - _External Workers_: Interactive agents (`pi`, `codex`, `claude`, `agy`, `grok`, `muse`) managed via the official Herdr CLI with native session recovery.
- **Correlated Turn Protocol**: Enforces bounded JSON result validation (`.bossmode/turns/<turn_id>.json`), eliminating brittle terminal screen scraping.
- **Independent Evaluation Gate**: Tasks require third-party verification (`evaluator != run.agent_role`) before reaching `succeeded`.
- **Gated Continual Learning Ladder**:
  - _Preference / Observation_ $\rightarrow$ `memory` proposal
  - _Repeated correction_ ($N \ge 2$) with passing evaluation $\rightarrow$ `skill` proposal
  - _Repeated failure_ ($N \ge 2$) $\rightarrow$ deterministic `control` proposal
  - All promotions require explicit user approval (`proposed -> accepted -> applied`).

---

## Supported Agent Matrix

| Agent                 | Session Coordinator | Native Subagent Dispatch | Herdr External Worker | Independent Reviewer |
| --------------------- | :-----------------: | :----------------------: | :-------------------: | :------------------: |
| **Antigravity (AGY)** |         ✅          |  ✅ (`invoke_subagent`)  |   ✅ (`herdr:agy`)    |          ✅          |
| **Codex**             |         ✅          |  ✅ (native task tools)  |  ✅ (`herdr:codex`)   |          ✅          |
| **Claude**            |         ✅          |            —             |  ✅ (`herdr:claude`)  |          ✅          |
| **Pi**                |         ✅          |            —             |    ✅ (`herdr:pi`)    |          ✅          |
| **Grok**              |         ✅          |            —             |   ✅ (`herdr:grok`)   |          ✅          |
| **Muse**              |         ✅          |            —             |   ✅ (`herdr:muse`)   |          ✅          |

---

## Quick Start

```bash
# 1. Setup workspace
cd ~/Developer/bossmode
uv sync

# 2. Initialize the registry
uv run bossmode init

# 3. Create a task with explicit criteria and permission bounds
uv run bossmode task add \
  --title "Generate OpenAPI Spec" \
  --goal "Create OpenAPI 3.0 specification for auth service" \
  --success-criteria "Valid OpenAPI JSON at specs/auth.json" \
  --priority 10 \
  --permissions-json '{"filesystem":"workspace-write","network":false}'

# 4. Run supervisor tick to determine next action
uv run bossmode supervisor tick
```

### Complete Lifecycle Walkthrough

```bash
# 5. Start a worker run
uv run bossmode run start TASK_ID \
  --role worker_claude \
  --model claude-3-7-sonnet

# 6. Record run completion (moves task to 'evaluating')
uv run bossmode run finish RUN_ID \
  --outcome succeeded \
  --summary "Generated and validated specs/auth.json" \
  --artifacts-json '[{"path":"specs/auth.json","kind":"spec"}]'

# 7. Record an independent evaluation (moves task to 'succeeded')
uv run bossmode evaluate TASK_ID \
  --run-id RUN_ID \
  --evaluator reviewer_grok \
  --passed \
  --score 1.0 \
  --evidence "specs/auth.json matches OpenAPI 3.0 schema validator"

# 8. Record user or system feedback
uv run bossmode feedback TASK_ID \
  --kind preference \
  --key api.spec-format \
  --content "Always include example payloads in schema definitions"

# 9. Supervisor tick proposes candidate promotions
uv run bossmode supervisor tick

# 10. User reviews and approves promotion
uv run bossmode promotion set PROMOTION_ID accepted
uv run bossmode promotion set PROMOTION_ID applied
```

For a comprehensive, annotated multi-turn scenario covering failure remediation, Herdr worker reuse, and skill promotion, see [**`docs/example-walkthrough.md`**](docs/example-walkthrough.md).

---

## Architecture & Responsibilities

```text
User
  -> Supervisor Coordinator (Any Agent: AGY, Codex, Claude, Pi, Grok, Muse)
       -> bossmode CLI / SQLite registry (.bossmode/control.db)
       -> Native subagent tools ------> Native subagents (e.g. Codex, AGY)
       -> Official Herdr CLI ---------> External interactive agents
       -> Independent Reviewer -------> Evaluation (evaluator != worker)
  <- Material results, blockers, and promotion proposals
```

| Actor              | Owns                                                                                   | Does Not Own                           |
| ------------------ | -------------------------------------------------------------------------------------- | -------------------------------------- |
| **User**           | Goals, permission expansions, trust decisions, promotion approvals                     | Registry bookkeeping                   |
| **Supervisor**     | Tick reconciliation, single-flight dispatch, prompt envelopes, evaluation tracking     | Vendor session internals               |
| **Registry**       | Durable task/run/turn IDs, state machines, digests, artifact manifests, feedback       | Live agent process liveness            |
| **Native Runtime** | Subagent lifecycle, execution threads, and live task IDs                               | MVP registry state                     |
| **Herdr**          | Process panes, terminal lifecycle, native session tuple `{source, agent, kind, value}` | Turn correlation, MVP success criteria |
| **Worker**         | Bounded task execution and declared artifact generation                                | Self-evaluation or state overrides     |
| **Reviewer**       | Objective evaluation against task success criteria                                     | Rewriting failed results               |

---

## State Models

### Task Lifecycle

```text
backlog -> ready -> running -> evaluating -> succeeded -> archived
                   |              \-> failed -> ready
                   |-> blocked -> ready
                   \-> waiting_user -> ready
```

- Only `run start` transitions tasks into `running`.
- Only `run finish` transitions tasks into `evaluating`, `failed`, `blocked`, or `waiting_user`.
- Only a passing `evaluate` moves tasks from `evaluating` to `succeeded`.
- All transitions use atomic SQLite updates inside `BEGIN IMMEDIATE` transactions.

### Promotion Lifecycle

```text
proposed -> accepted -> applied
        \-> rejected
```

- `bossmode promotion propose` (and `supervisor tick`) generate proposals heuristically from feedback.
- Applying promotions requires explicit human authorization and reviewable file/code edits.

---

## Execution Modes

### 1. Native Subagent Path (Codex, AGY)

For runtimes with built-in subagent toolsets:

1. Spawn bounded subagent with task parameters and output schema.
2. Record run with live thread/task ID:
   ```bash
   uv run bossmode run start TASK_ID --role researcher --thread-id NATIVE_THREAD_ID
   ```
3. Await subagent completion and record outcome:
   ```bash
   uv run bossmode run finish RUN_ID --outcome succeeded --summary "Task finished"
   ```

### 2. Herdr External Worker Path (`pi`, `codex`, `claude`, `agy`, `grok`, `muse`)

For external interactive agents managed via Herdr:

1. **Reserve Run**: `uv run bossmode run start TASK_ID --role claude`
2. **Spawn Worker**: Create pane and start agent via Herdr CLI:
   ```bash
   herdr pane split PARENT_PANE --direction right --cwd "$PWD" --no-focus
   herdr agent start worker_1234abcd --kind claude --pane NEW_PANE
   ```
3. **Bind Live Worker**:
   ```bash
   uv run bossmode herdr bind RUN_ID \
     --herdr-session bossmode \
     --worker worker_1234abcd \
     --kind claude \
     --pane-id PANE_ID \
     --session-source "herdr:claude" \
     --session-agent "claude" \
     --session-ref-kind "id" \
     --session-value "session-uuid"
   ```
4. **Start Correlated Turn**:
   ```bash
   uv run bossmode turn start RUN_ID --purpose task --prompt "Generate OpenAPI spec"
   ```
5. **Prompt with Envelope**:
   ```bash
   herdr agent prompt worker_1234abcd \
     "Write exactly one JSON result to .bossmode/turns/turn_xxx.json with turn_id='turn_xxx', status='succeeded', summary='...', artifacts=[...]" \
     --wait
   ```
6. **Validate and Finish Turn**:
   ```bash
   uv run bossmode turn finish TURN_ID --status succeeded --lifecycle-evidence done
   ```
7. **Finish Run & Evaluate**: Finish the run and record an independent reviewer evaluation.

Detailed protocol specifications and recovery rules are documented in [`docs/agent-workflow.md`](docs/agent-workflow.md).

---

## CLI Reference

All CLI commands output structured JSON and exit `0` on success, `2` on structured error.

| Command Group | Subcommand   | Key Arguments                                                                                    | Description                                                                               |
| ------------- | ------------ | ------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------- |
| `bossmode`    | `init`       | `[--db PATH]`                                                                                    | Initializes registry schema (idempotent, auto-migrating).                                 |
| `task`        | `add`        | `--title`, `--goal`, `--success-criteria`, `[--priority]`, `[--permissions-json]`                | Creates a new task in `ready` or `backlog`.                                               |
| `task`        | `list`       | `[--state STATE]`                                                                                | Lists tasks filtered by state.                                                            |
| `task`        | `show`       | `<task_id>`                                                                                      | Returns task details with runs, events, feedback, and evaluations.                        |
| `task`        | `transition` | `<task_id>`, `<to_state>`, `--actor`, `--reason`, `[--evidence]`, `[--blocked-on]`               | Executes an explicit state transition.                                                    |
| `run`         | `start`      | `<task_id>`, `--role`, `[--thread-id]`, `[--model]`, `[--reasoning-effort]`                      | Starts an execution run and sets task to `running`.                                       |
| `run`         | `finish`     | `<run_id>`, `--outcome`, `--summary`, `[--artifacts-json]`, `[--tokens]`, `[--duration-seconds]` | Finishes run and transitions task to `evaluating` or terminal outcome.                    |
| `run`         | `show`       | `<run_id>`                                                                                       | Returns run details including turns and Herdr bindings.                                   |
| `herdr`       | `bind`       | `<run_id>`, `--herdr-session`, `--worker`, `--kind`, `[--pane-id]`, `[--session-*]`              | Records live Herdr worker binding and native session metadata.                            |
| `turn`        | `start`      | `<run_id>`, `--purpose`, `--prompt`                                                              | Registers a single open turn and allocates result artifact path.                          |
| `turn`        | `finish`     | `<turn_id>`, `--status`, `[--summary]`, `[--lifecycle-evidence]`                                 | Validates turn JSON result file and marks turn complete.                                  |
| `turn`        | `show`       | `<turn_id>`                                                                                      | Inspects turn record, prompt digest, and validated result JSON.                           |
| `evaluate`    |              | `<task_id>`, `--run-id`, `--evaluator`, `--passed \| --failed`, `--evidence`, `[--score]`        | Records independent evaluation (required to reach `succeeded`).                           |
| `feedback`    |              | `<task_id>`, `--kind`, `--key`, `--content`, `[--run-id]`                                        | Ingests structured user/system feedback with recurrence key.                              |
| `promotion`   | `propose`    |                                                                                                  | Scans feedback and generates candidate promotion proposals.                               |
| `promotion`   | `list`       | `[--status STATUS]`                                                                              | Lists promotion proposals filtered by status.                                             |
| `promotion`   | `set`        | `<promotion_id>`, `<status>` (`accepted`, `rejected`, `applied`)                                 | Advances promotion through user approval gates.                                           |
| `supervisor`  | `tick`       |                                                                                                  | Computes single-flight dispatch, active work, evaluation queue, blockers, and promotions. |

---

## Verification & Testing

```bash
# Check code style and formatting
uv run ruff check .
uv run ruff format --check .

# Run full unit and integration test suite (52 tests)
uv run pytest

# Run automated end-to-end UAT evaluation loop (28 checks across 5 scenarios)
uv run python scripts/run_uat.py
```
