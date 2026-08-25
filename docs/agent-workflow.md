# Supervisor protocol

This is the canonical implementation guide for supervisor agents and operators. It is intentionally
precise about commands and state transitions. Most users should start with the
[prompt guide](prompt-guide.md) and let their supervisor perform this protocol.

| User intent | Supervisor responsibility |
|---|---|
| Start bounded work | Record the goal, success criteria, permissions, and next action before delegation. |
| Resume | Verify stored records against authoritative live runtime identity. |
| Use an external agent | Reserve the run, create and bind one verified Herdr worker, and correlate each turn. |
| Call work complete | Record the run result and obtain independent evaluation. |
| Record a correction | Store sourced feedback and show any proposal without applying it. |
| Approve a promotion | Implement and verify the accepted artifact before recording it as applied. |

This MVP is a control record, not an agent transport. Any agent (such as Antigravity/AGY,
Codex, Claude, Pi, Grok, or Muse) can act as the session coordinator/supervisor, talking to
agents through native runtime task tools or the official Herdr CLI, and recording the resulting
task, run, turn, evaluation, and feedback state through `bossmode`.

```text
user
  -> supervisor (any coordinator: AGY, Codex, Claude, Pi, Grok, Muse)
       -> registry CLI / SQLite (durable control record)
       -> native subagent tools ------> native subagent (e.g. Codex, AGY)
       -> official Herdr CLI ---------> external agent (pi, codex, claude, agy, grok, muse)
       -> independent reviewer -------> evaluation
  <- material result, blocker, or approval request
```

There is no background MVP daemon and no generic executor interface. The registry never sends a
prompt, creates a pane, grants permission, or decides that terminal text means success.

## Registry and worktree boundary

Each checkout or linked worktree owns its own `.bossmode/control.db`. Worktree-local registries
keep task history and schema migrations tied to the source that understands them. A registry path
that resolves to a sibling worktree's standard `.bossmode/control.db` is rejected before Bossmode
opens SQLite, inspects the schema, or applies a migration.

Before running any registry command, verify the ownership boundary:

```bash
pwd
git rev-parse --show-toplevel
realpath .bossmode/control.db
```

Do not use `--db` or `BOSSMODE_DB` to point at another worktree's registry. If a database is newer
than the current checkout supports, run the command from the owning checkout and treat any
remaining mismatch as a blocker. A genuinely shared control plane needs a dedicated,
supervisor-owned registry location and an explicit adoption protocol; the MVP does not provide an
implicit shared-registry mode.

## Responsibilities

| Actor | Owns | Does not own |
|---|---|---|
| User | Goals, permission expansion, trust dialogs, and promotion approval | Runtime bookkeeping |
| Supervisor (Any Agent) | Reconciliation, one-at-a-time dispatch, prompt envelopes, evidence checks, and state transitions | Vendor session internals |
| Registry | Durable task/run/turn IDs, state machines, prompt digests, artifact paths, evaluations, and feedback | Live agent identity or liveness |
| Native runtime | Native subagent creation, messaging, waiting, and live task identity | MVP task state |
| Herdr | External agent processes (`pi`, `codex`, `claude`, `agy`, `grok`, `muse`), panes, lifecycle observations, and native session restoration | Turn correlation or MVP success criteria |
| Worker | The bounded task and its declared artifacts | Self-approval or policy changes |
| Reviewer | Independent checks against the task's success criteria | Rewriting a failed result unless separately authorized |

Live native runtime or Herdr state is authoritative for executor identity. Registry identities are
durable indexes that must be verified against live state before every prompt, interruption,
continuation, or close.

Two different operations are easy to confuse. `bossmode reconcile` converges the registry itself:
it migrates the schema, materialises promotion proposals, and reports task buckets. Verifying a
stored worker identity against live Herdr or native runtime state is a separate supervisor
responsibility that Bossmode never performs for you.

## Common task lifecycle

1. The supervisor runs `uv run bossmode` to converge the registry and read current state. The
   command writes: it creates or migrates the registry and materialises promotion proposals.
2. It records user requests with `bossmode task create`, including success criteria and permission limits.
3. It selects only the task returned in `next_task` and starts one run. `next_task` stays `null`
   while another task is running or awaiting evaluation.
4. It delegates through either the native subagent path (e.g. AGY, Codex) or the Herdr worker
   path (`pi`, `codex`, `claude`, `agy`, `grok`, `muse`) below.
5. It records the run result. `succeeded` moves the task to `evaluating`, not to final success.
6. A separate reviewer checks deterministic evidence or the produced artifacts. The supervisor
   records that verdict with `evaluate`.
7. Explicit corrections and preferences are recorded with `feedback`. Any resulting promotion is
   only a proposal until the user approves it.

## Native subagent path (Codex, AGY, etc.)

1. Create a bounded native subagent (via Codex task tools, AGY `invoke_subagent`, etc.) with the
   task ID, goal, success criteria, allowed actions, evidence, and required response fields.
2. Record the live thread or subagent task ID:

   ```bash
   uv run bossmode run start TASK_ID \
     --role researcher \
     --thread-id NATIVE_THREAD_OR_TASK_ID
   ```

3. Wait or continue through the runtime's native subagent tools. Before every later message,
   verify the stored ID against live runtime state.
4. Record the terminal result with `run finish`. Do not translate a subagent's self-reported success
   into a passing evaluation.

Native subagent tasks do not need `herdr bind` or turn records. Those records close a specific
correlation gap in Herdr's interactive-agent transport.

## Herdr worker path

### 1. Reserve before creating runtime state

Start the registry run first:

```bash
uv run bossmode run start TASK_ID --role claude
```

Derive a deterministic lowercase worker name from the returned run ID, such as
`worker_1234abcd`. Create a pane at an interactive shell in this repository, then start the agent
with the release-matched official CLI:

```bash
herdr pane split PARENT_PANE_ID --direction right --cwd "$PWD" --no-focus
herdr agent start worker_1234abcd --kind claude --pane NEW_PANE_ID
```

Do not start a second worker if either command returns an uncertain result. Verify the
deterministic name with `herdr agent get worker_1234abcd` and `herdr agent list`.

### 2. Bind only observed identity

After confirming one matching live worker, record its observed Herdr location:

```bash
uv run bossmode herdr bind RUN_ID \
  --herdr-session bossmode \
  --worker worker_1234abcd \
  --agent-kind claude \
  --pane-id LIVE_PANE_ID \
  --tab-id LIVE_TAB_ID \
  --workspace-id LIVE_WORKSPACE_ID
```

After `herdr agent get` reports a native session, rebind with all four fields:

```bash
uv run bossmode herdr bind RUN_ID \
  --herdr-session bossmode \
  --worker worker_1234abcd \
  --agent-kind claude \
  --session-source OBSERVED_SOURCE \
  --session-agent OBSERVED_AGENT \
  --session-ref-kind id \
  --session-value OBSERVED_VALUE
```

The registry accepts newly observed pane metadata and the first native session reference. It
rejects a different Herdr session, worker, agent kind, or native session tuple for that run. The
same session and worker name cannot be bound to another active run. The command records an
observation; it does not independently prove the worker is live.

### 3. Correlate every prompt

First require the worker to be unambiguously `idle` or `done`. Register the logical prompt before
sending it. A run may have only one open turn:

```bash
uv run bossmode turn start RUN_ID \
  --purpose task \
  --prompt "Produce the requested report"
```

The returned `prompt_digest` hashes this logical prompt. The supervisor then adds an envelope that
includes the returned `turn_id` and `artifact_path` and submits it through Herdr:

```text
Complete the bounded task below. Write exactly one JSON result to ARTIFACT_PATH.
The JSON turn_id must equal TURN_ID. Do not claim success unless the declared artifacts exist.

Logical task: Produce the requested report
```

```bash
herdr agent prompt worker_1234abcd "ENVELOPED_PROMPT" --wait
```

The result file contract is:

```json
{
  "turn_id": "turn_...",
  "status": "succeeded",
  "summary": "What changed and what was verified",
  "artifacts": [{"path": "reports/result.md", "kind": "report"}]
}
```

Allowed terminal outcomes are `succeeded`, `blocked`, `failed`, and `unknown`. The result file
reports the worker's own `status`; the registry validates it and stores it as the turn's `outcome`,
alongside a `status` of `finished`. For success,
`turn finish` reads at most 1 MiB from the exact generated path, validates the JSON object, requires
the matching `turn_id` and `succeeded` status, and stores the validated result:

```bash
uv run bossmode turn finish TURN_ID \
  --outcome succeeded \
  --lifecycle-evidence done
```

The supervisor then checks that each declared artifact exists and satisfies the task before
finishing the run. For a missing or invalid result, record `failed` or `unknown` with an explicit
`--summary`; successful finish fails closed instead of trusting a supervisor assertion.

Herdr's `--wait` observes lifecycle state, not a prompt ID. If the agent was already working, a
different active turn could satisfy the wait. This is why the supervisor requires a settled worker
before submission and treats the exact result file—not terminal text—as correlation evidence.

### 4. Continue the same agent

For a clarification before run completion, verify the same worker and start another turn with
`--purpose clarification`, `correction`, or `review_follow_up`. If evaluation requires a later run,
transition the task back to `ready`, start a new run, and bind the same live worker and native
session. Finished-run bindings become `stale`, so they retain history without reserving the live
worker name. Do not replace a worker merely because its pane moved or the server restarted.

## Recover after interruption

Run `uv run bossmode` (or `bossmode reconcile`). Its `running` and `evaluating` entries contain
nested runs, Herdr bindings, turns, output paths, and validated results. Use
`bossmode run show RUN_ID` or `bossmode turn show TURN_ID` when you need one exact record, then
verify that stored identity against live native runtime (AGY, Codex) or Herdr state before
continuing.

## Live-identity and failure rules

- Missing, duplicate, foreign, or ambiguous live identity: stop and report `blocked`; do not adopt
  or close anything.
- Trust or permission dialog: ask the user. Neither the supervisor nor worker may approve it from a
  general task permission.
- Herdr reports `blocked`, `unknown`, stalled, or timeout: preserve that result. Do not flatten it
  into failure or retry blindly.
- Missing, oversized, malformed, or wrong-turn result file: successful finish is rejected; finish
  the turn as `unknown` or `failed` with the exact evidence instead.
- Open turn: finish it with an explicit terminal status before finishing its run.
- Worker result passes its own checks: finish the run as `succeeded`, then obtain an independent
  evaluation before the task can become `succeeded`.
- Stored pane ID differs but the unique worker and full native session match: update the observed
  pane metadata through `herdr bind`; live Herdr state remains authoritative.

The MVP intentionally has no command that closes a Herdr worker. Destructive lifecycle actions stay
in the official runtime and require fresh live-identity reconciliation plus user authorization when
the action could affect ambiguous or foreign work.

## Maintenance and OS scheduling

To ensure database integrity, analyze token and model efficiency, and trigger recurring promotion scans without running a persistent daemon:

1. **On-demand Maintenance**:
   ```bash
   uv run bossmode maintenance
   ```
   Audits database integrity, active/stale bindings, orphaned turns, and outputs telemetry grouped by model and reasoning effort.

2. **Native OS Scheduling Adapter**:
   ```bash
   # Register automated background maintenance (LaunchAgent on macOS, crontab on Linux)
   uv run bossmode schedule install --interval 3600

   # Check scheduler registration and available log activity
   uv run bossmode schedule status

   # Cleanly remove the OS scheduler job
   uv run bossmode schedule uninstall
   ```

Automated tests validate generated commands and mocked success, fallback, and failure behavior.
They never mutate a developer or CI host's launchd or crontab state. Run an actual
install-status-uninstall smoke only after the user authorizes that host change. Likewise, live
Herdr identity reconciliation remains an explicit operational gate rather than an automated CI
test.
