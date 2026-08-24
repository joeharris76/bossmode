# Bossmode

Stop babysitting your agents. Bossmode makes you the CEO of your agent fleet. A supervisor agent
manages specialist workers such as Claude, Codex, Grok, Pi, and Muse while they carry out bounded
tasks.

Instead of retraining models, Bossmode improves your agent fleet over time: it preserves context in memory, turns successful workflows into reusable skills, and enforces critical rules in deterministic code.

---

## Command Surface

Bossmode provides a minimalist, zero-config CLI. Invoking `bossmode` with no arguments automatically initializes the database if needed, reconciles active work, and returns the next action.

```text
bossmode                        # Default: Reconcile project session and return next action
├── task
│   ├── create                  # Create a new task with success criteria
│   ├── list                    # List tasks by state
│   ├── show <id>               # Show task details and event history
│   └── transition <id> <state> # Move task between lifecycle states
├── run
│   ├── start <task_id>         # Start an execution run for a worker
│   ├── finish <run_id>         # Complete a run (moves task to evaluating or error)
│   └── show <run_id>           # Show run details and turn records
├── herdr
│   ├── bind <run_id>           # Link a live Herdr worker pane to a run
│   └── show <run_id>           # Show Herdr binding and native session info
├── turn
│   ├── start <run_id>          # Open a prompt turn and allocate result path
│   ├── finish <turn_id>        # Validate turn JSON artifact and mark completed
│   └── show <turn_id>          # Show prompt text, digest, and validated result
├── evaluate <task_id>          # Record independent evaluation (reviewer != worker)
├── feedback <task_id>          # Record user or system feedback with recurrence key
├── promotion
│   ├── propose                 # Scan feedback and generate candidate proposals
│   ├── list                    # List promotion proposals by status
│   ├── accept <id>             # Accept a proposal for implementation
│   ├── reject <id>             # Reject a proposal
│   └── apply <id>              # Mark an accepted proposal as verified and applied
├── maintenance                 # Run telemetry analytics, health checks, & promotion scan
└── schedule
    ├── install                 # Register OS scheduler job (launchd on macOS, crontab on Linux)
    ├── status                  # Inspect registration and available log activity
    └── uninstall               # Cleanly remove OS scheduler job
```

---

## Key Features

- **Zero-Config Task Registry**: Automatically creates and migrates `.bossmode/control.db` on first read or write.
- **Single-Flight Dispatch**: Schedules one ready task at a time (`bossmode`), preventing conflicting edits and race conditions.
- **Flexible Multi-Agent Roles**: Any supported agent (Antigravity/AGY, Codex, Claude, Pi, Grok, Muse) can supervise workflows, execute worker tasks, or act as an independent reviewer.
- **Dual Execution Options**:
  - _Native subagents_: Dispatched directly through host runtime tools (such as Codex subagents or AGY `invoke_subagent`).
  - _External workers_: Interactive CLI agents running in Herdr terminal panes with native session recovery.
- **Verified Turn Results**: Validates structured JSON outputs at exact file paths (`.bossmode/turns/<turn_id>.json`) rather than guessing success from terminal text.
- **Independent Evaluation**: Requires a separate reviewer (`evaluator != worker`) to verify results against task criteria before marking a task as succeeded.
- **Safe Learning Ladder**:
  - _Context & preferences_ $\rightarrow$ **Memory** proposals.
  - _Repeated corrections with passing evaluations_ ($N \ge 2$) $\rightarrow$ **Skill** proposals.
  - _Repeated operational failures_ ($N \ge 2$) $\rightarrow$ **Code / Control** proposals.
  - All proposals remain pending until the user explicitly accepts and applies them.

---

## Supported Agents

| Agent                 | Supervisor | Native Subagent Dispatch | Herdr External Worker | Independent Reviewer |
| --------------------- | :--------: | :----------------------: | :-------------------: | :------------------: |
| **Antigravity (AGY)** |     ✅     |  ✅ (`invoke_subagent`)  |   ✅ (`herdr:agy`)    |          ✅          |
| **Codex**             |     ✅     |  ✅ (native task tools)  |  ✅ (`herdr:codex`)   |          ✅          |
| **Claude**            |     ✅     |            —             |  ✅ (`herdr:claude`)  |          ✅          |
| **Pi**                |     ✅     |            —             |    ✅ (`herdr:pi`)    |          ✅          |
| **Grok**              |     ✅     |            —             |   ✅ (`herdr:grok`)   |          ✅          |
| **Muse**              |     ✅     |            —             |   ✅ (`herdr:muse`)   |          ✅          |

---

## Quick Start

### 1. Check Session Status

Run `bossmode` with no arguments to start or inspect the project session:

```bash
cd ~/Developer/bossmode
uv sync
uv run bossmode
```

### 2. Create a Task

Define a task with clear success criteria and permission limits:

```bash
uv run bossmode task create \
  --title "Generate OpenAPI Spec" \
  --goal "Create OpenAPI 3.0 specification for auth service" \
  --success-criteria "Valid OpenAPI JSON file exists at specs/auth.json" \
  --priority 10 \
  --permissions-json '{"filesystem":"workspace-write","network":false}'
```

### 3. Check Session Dispatch

Run `bossmode` again to see the newly dispatched task:

```bash
uv run bossmode
```

---

## Lifecycle Walkthrough

```bash
# 1. Start an execution run
uv run bossmode run start TASK_ID \
  --role worker_claude \
  --model claude-3-7-sonnet

# 2. Complete the run (moves task from 'running' to 'evaluating')
uv run bossmode run finish RUN_ID \
  --outcome succeeded \
  --summary "Generated and validated specs/auth.json" \
  --artifacts-json '[{"path":"specs/auth.json","kind":"spec"}]'

# 3. Perform an independent evaluation (moves task to 'succeeded')
uv run bossmode evaluate TASK_ID \
  --run-id RUN_ID \
  --evaluator reviewer_grok \
  --passed \
  --score 1.0 \
  --evidence "specs/auth.json validated against OpenAPI 3.0 schema"

# 4. Record feedback from the task
uv run bossmode feedback TASK_ID \
  --kind preference \
  --key api.spec-format \
  --content "Always include example payloads in schema definitions"

# 5. Check for new learning proposals
uv run bossmode

# 6. Review and approve a promotion proposal
uv run bossmode promotion accept PROMOTION_ID
uv run bossmode promotion apply PROMOTION_ID
```

For an end-to-end annotated example covering worker remediation, Herdr session recovery, and skill promotion, see [**`docs/example-walkthrough.md`**](docs/example-walkthrough.md).

---

## System Architecture

```text
User
  │
  ▼
Supervisor Agent (AGY, Codex, Claude, Pi, Grok, or Muse)
  │
  ├──► bossmode CLI / SQLite Registry (.bossmode/control.db)
  │
  ├──► Native Subagent Tools ──────► Native Subagent (Codex, AGY)
  │
  ├──► Official Herdr CLI ─────────► External Interactive Agent (pane)
  │
  └──► Independent Reviewer ───────► Evaluation Gate (evaluator != worker)
```

### Roles and Boundaries

| Role               | Responsibilities                                                               | Out of Scope                                 |
| ------------------ | ------------------------------------------------------------------------------ | -------------------------------------------- |
| **User**           | Defines goals, expands permissions, makes trust decisions, approves promotions | Manual registry bookkeeping                  |
| **Supervisor**     | Reconciles state, dispatches single tasks, formats prompts, tracks evaluations | Modifying vendor session internals           |
| **Registry**       | Stores task, run, turn, evaluation, and feedback records durably               | Managing live process lifecycles             |
| **Native Runtime** | Manages subagent creation, messaging, and thread IDs                           | Storing task control state                   |
| **Herdr**          | Manages external agent processes, terminal panes, and session recovery         | Validating turn results or task criteria     |
| **Worker**         | Executes assigned tasks and produces declared artifacts                        | Self-approving work or changing policy       |
| **Reviewer**       | Checks task artifacts objectively against success criteria                     | Editing failed results without authorization |

---

## State Models

### Task States

```text
backlog ──► ready ──► running ──► evaluating ──► succeeded ──► archived
                         │              │
                         │              └──► failed ──► ready
                         │
                         ├──► blocked ──► ready
                         │
                         └──► waiting_user ──► ready
```

- `run start`: Moves a `ready` task into `running`.
- `run finish`: Moves a `running` task into `evaluating`, `failed`, `blocked`, or `waiting_user`.
- `evaluate`: Only a passing evaluation (`--passed`) moves a task from `evaluating` to `succeeded`.
- All database state transitions use atomic SQLite transactions (`BEGIN IMMEDIATE`).

### Promotion States

```text
proposed ──► accepted ──► applied
   │
   └──► rejected (can be re-proposed upon new feedback)
```

- Bossmode proposes improvements based on feedback patterns, but never applies them automatically.
- The user reviews proposals and advances them through explicit approval gates (`bossmode promotion accept` $\rightarrow$ `bossmode promotion apply`).

---

## Execution Modes

### 1. Native Subagent Path (Codex, AGY)

Use native subagents when running inside environments with built-in subagent tools:

1. Spawn a subagent with task parameters, success criteria, and output contracts.
2. Record the run with the subagent thread ID:
   ```bash
   uv run bossmode run start TASK_ID --role researcher --thread-id NATIVE_THREAD_ID
   ```
3. When the subagent finishes, record the result:
   ```bash
   uv run bossmode run finish RUN_ID --outcome succeeded --summary "Research complete"
   ```

### 2. Herdr External Worker Path (`pi`, `codex`, `claude`, `agy`, `grok`, `muse`)

Use Herdr when delegating to interactive agents in separate terminal panes:

1. **Reserve Run**:
   ```bash
   uv run bossmode run start TASK_ID --role claude
   ```
2. **Spawn Worker Pane**:
   ```bash
   herdr pane split PARENT_PANE --direction right --cwd "$PWD" --no-focus
   herdr agent start worker_api --kind claude --pane NEW_PANE
   ```
3. **Bind Live Worker**:
   ```bash
   uv run bossmode herdr bind RUN_ID \
     --herdr-session bossmode \
     --worker worker_api \
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
5. **Prompt Worker with Envelope**:
   ```bash
   herdr agent prompt worker_api \
     "Write JSON result to .bossmode/turns/turn_xxx.json with turn_id='turn_xxx', status='succeeded', summary='...', artifacts=[...]" \
     --wait
   ```
6. **Validate Turn Output**:
   ```bash
   uv run bossmode turn finish TURN_ID --status succeeded --lifecycle-evidence done
   ```
7. **Complete and Evaluate**: Finish the run and record an evaluation from an independent reviewer.

For full protocol details and fault-recovery rules, see [**`docs/agent-workflow.md`**](docs/agent-workflow.md).

---

## CLI Reference

All CLI commands output structured JSON, exiting `0` on success and `2` on error.

| Command Group | Subcommand   | Key Arguments                                                                                    | Description                                                            |
| ------------- | ------------ | ------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------- |
| `bossmode`    | _(default)_  | `[--db PATH]`                                                                                    | Reconciles session state, active work, and returns next action.        |
| `task`        | `create`     | `--title`, `--goal`, `--success-criteria`, `[--priority]`, `[--permissions-json]`                | Creates a new task in `ready` or `backlog` (alias: `add`).             |
| `task`        | `list`       | `[--state STATE]`                                                                                | Lists tasks filtered by state.                                         |
| `task`        | `show`       | `<task_id>`                                                                                      | Shows task details with runs, events, feedback, and evaluations.       |
| `task`        | `transition` | `<task_id>`, `<to_state>`, `--actor`, `--reason`, `[--evidence]`, `[--blocked-on]`               | Executes an explicit state transition.                                 |
| `run`         | `start`      | `<task_id>`, `--role`, `[--thread-id]`, `[--model]`, `[--reasoning-effort]`                      | Starts an execution run and sets task to `running`.                    |
| `run`         | `finish`     | `<run_id>`, `--outcome`, `--summary`, `[--artifacts-json]`, `[--tokens]`, `[--duration-seconds]` | Completes a run and transitions task to `evaluating` or error state.   |
| `run`         | `show`       | `<run_id>`                                                                                       | Shows run details, turn history, and Herdr bindings.                   |
| `herdr`       | `bind`       | `<run_id>`, `--herdr-session`, `--worker`, `--kind`, `[--pane-id]`, `[--session-*]`              | Links a live Herdr worker and native session reference to a run.       |
| `turn`        | `start`      | `<run_id>`, `--purpose`, `--prompt`                                                              | Opens a turn and allocates a result artifact path.                     |
| `turn`        | `finish`     | `<turn_id>`, `--status`, `[--summary]`, `[--lifecycle-evidence]`                                 | Validates the turn JSON result file and marks the turn finished.       |
| `turn`        | `show`       | `<turn_id>`                                                                                      | Shows turn record details, prompt text, and validated result JSON.     |
| `evaluate`    |              | `<task_id>`, `--run-id`, `--evaluator`, `--passed \| --failed`, `--evidence`, `[--score]`        | Records independent evaluation (required to reach `succeeded`).        |
| `feedback`    |              | `<task_id>`, `--kind`, `--key`, `--content`, `[--run-id]`                                        | Ingests user or system feedback with a recurrence key.                 |
| `promotion`   | `propose`    |                                                                                                  | Analyzes feedback history and generates promotion proposals.           |
| `promotion`   | `list`       | `[--status STATUS]`                                                                              | Lists promotion proposals filtered by status.                          |
| `promotion`   | `accept`     | `<promotion_id>`                                                                                 | Accepts a proposal for implementation.                                 |
| `promotion`   | `reject`     | `<promotion_id>`                                                                                 | Rejects a proposal.                                                    |
| `promotion`   | `apply`      | `<promotion_id>`                                                                                 | Marks an accepted proposal as verified and applied.                    |
| `maintenance` |              | `[--db PATH]`                                                                                    | Runs telemetry analytics, database health check, and promotion scan.   |
| `schedule`    | `install`    | `[--interval SECS]`, `[--cron EXPR]`, `[--target {maintenance,reconcile}]`                       | Registers native OS scheduler job (launchd on macOS, crontab on Linux) |
| `schedule`    | `status`     |                                                                                                  | Inspects OS scheduler registration and available log activity.         |
| `schedule`    | `uninstall`  |                                                                                                  | Cleanly unloads and removes the native OS scheduler job.               |

---

## Verification & Testing

```bash
# Check code style and formatting
uv run ruff check .
uv run ruff format --check .

# Run the full unit, concurrency, integration, and acceptance suite
uv run pytest

# Run the in-process functional acceptance loop (28 named checks)
uv run python scripts/run_uat.py
```

`uv run pytest` enforces branch coverage of at least 90%, fails tests that exceed 30 seconds, and
rejects unknown markers or configuration. CI repeats the suite on Python 3.12, 3.13, and 3.14 on
macOS and Linux, then builds and executes the wheel in an isolated environment.

The functional acceptance loop exercises registry and CLI behavior in process. It does not create
or prompt live Herdr workers, approve trust dialogs, or install host scheduler entries. Those
operations remain separate, user-authorized manual gates because live runtime identity and host
state are authoritative.
