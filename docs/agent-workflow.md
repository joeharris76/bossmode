# Supervisor protocol

This is the canonical implementation guide for supervisor agents and operators. It is intentionally
precise about commands and state transitions. Most users should start with the
[prompt guide](prompt-guide.md) and let their supervisor perform this protocol.

| User intent | Supervisor responsibility |
|---|---|
| Start bounded work | Record the goal, success criteria, permissions, and next action before delegation. |
| Resume | Reconcile stored records against authoritative live runtime identity. |
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

Live native runtime or Herdr state is authoritative for executor identity. Registry identities are durable
indexes that must be reconciled before every prompt, interruption, continuation, or close.

## Common task lifecycle

1. The supervisor runs `uv run bossmode` to inspect session state and ready tasks.
2. It records user requests with `bossmode task create`, including success criteria and permission limits.
3. For singleton work it selects only the task returned in `dispatch` and
   starts one run. For team work it uses the atomic `dispatch batch` path for
   disjoint child tasks; singleton dispatch remains serialized while another
   task is running or awaiting evaluation.
4. It delegates through either the native subagent path (e.g. AGY, Codex), the
   legacy singleton Herdr path, or the parallel team path below.
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

3. Wait or continue through the runtime's native subagent tools. Before every later message, reconcile
   the stored ID against live runtime state.
4. Record the terminal result with `run finish`. Do not translate a subagent's self-reported success
   into a passing evaluation.

Native subagent tasks do not need `herdr bind` or turn records. Those records close a specific
correlation gap in Herdr's interactive-agent transport.

## Herdr worker path

### 1. Reserve and admit before creating runtime state

Validate the task hierarchy and live Git writer before creating any worker. A
manager run must exist before a worker run; a team tab must be created and
reconciled before any team pane:

```bash
uv run bossmode run manager-start TEAM_ID \
  --identity-json '{"source":"herdr","value":"manager"}'

herdr tab create --workspace WORKSPACE_ID --label "TEAM_TAB_LABEL" --cwd "$PWD" --no-focus
herdr tab list --workspace WORKSPACE_ID
herdr tab get TEAM_TAB_ID

uv run bossmode team bind-tab TEAM_ID \
  --herdr-session bossmode \
  --workspace-id WORKSPACE_ID \
  --tab-id TEAM_TAB_ID \
  --observed-tab-label "TEAM_TAB_LABEL"
```

The observed label must equal the team's durable `expected_tab_label`. The
reconciliation is idempotent and refuses to replace a different live tab.
Derive a deterministic lowercase worker name from the returned run ID, such as
`worker_1234abcd`. For every agent invocation, split an explicit anchor pane
inside the reconciled team tab, then start the agent in the returned pane with
the release-matched official CLI:

```bash
herdr pane split TEAM_ANCHOR_PANE_ID --direction down --cwd "$PWD" --no-focus
herdr agent start worker_1234abcd --kind claude --pane NEW_PANE_ID
```

`TEAM_ANCHOR_PANE_ID` must have the same `workspace_id` and `tab_id` recorded by
`team bind-tab`. Keep the manager/control pane at the top and stack every
worker/reviewer pane below it with horizontal dividers. Never use `--current`,
the focused tab, or a pane from another tab as the parent. Repeat the
down-split-before-start sequence for manager, worker, and reviewer agents; the
first `tab create` is the only tab creation.

Do not start a second worker if either command returns an uncertain result. Reconcile the
deterministic name with `herdr agent get worker_1234abcd` and `herdr agent list`.

Before `run worker-start` or `dispatch batch`, collect live Git evidence for
the proposed writer. It must use a dedicated non-protected branch, a separate
linked worktree rather than the primary checkout, a clean worktree including
untracked files, a unique branch/path/worktree ID, and a base SHA that resolves
to an existing commit in this repository. Reject protected branches
(`main`, `master`, `develop`, and repository-configured protected branches),
primary or dirty worktrees, duplicate writer identities, and invalid base SHAs.
Only after these checks pass may the supervisor persist the writer reservation
and create the worker. The current CLI carries the reservation in
`--writer-json`; it does not provide a flag to bypass live admission.

### 2. Bind only observed identity

After confirming one matching live worker, record its observed Herdr location:

```bash
uv run bossmode herdr bind RUN_ID \
  --herdr-session bossmode \
  --worker worker_1234abcd \
  --kind claude \
  --pane-id LIVE_PANE_ID \
  --tab-id LIVE_TAB_ID \
  --workspace-id LIVE_WORKSPACE_ID
```

After `herdr agent get` reports a native session, reconcile the same binding with all four fields:

```bash
uv run bossmode herdr bind RUN_ID \
  --herdr-session bossmode \
  --worker worker_1234abcd \
  --kind claude \
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

Allowed terminal statuses are `succeeded`, `blocked`, `failed`, and `unknown`. For success,
`turn finish` reads at most 1 MiB from the exact generated path, validates the JSON object, requires
the matching `turn_id` and `succeeded` status, and stores the validated result:

```bash
uv run bossmode turn finish TURN_ID \
  --status succeeded \
  --lifecycle-evidence done
```

The supervisor then checks that each declared artifact exists and satisfies the task before
finishing the run. For a missing or invalid result, record `failed` or `unknown` with an explicit
`--summary`; successful finish fails closed instead of trusting a supervisor assertion.

Herdr's `--wait` observes lifecycle state, not a prompt ID. If the agent was already working, a
different active turn could satisfy the wait. This is why the supervisor requires a settled worker
before submission and treats the exact result file—not terminal text—as correlation evidence.

### 4. Continue the same agent

For a clarification before run completion, reconcile the same worker and start another turn with
`--purpose clarification`, `correction`, or `review_follow_up`. If evaluation requires a later run,
transition the task back to `ready`, start a new run, and bind the same live worker and native
session. Finished-run bindings become `stale`, so they retain history without reserving the live
worker name. Do not replace a worker merely because its pane moved or the server restarted.

## Recover after interruption

Run `uv run bossmode` (or `bossmode`). Its `active` and `needs_evaluation` entries contain nested runs, Herdr
bindings, turns, output paths, and validated results. Use `bossmode run show RUN_ID` or
`bossmode turn show TURN_ID` when you need one exact record, then reconcile that stored
identity against live native runtime (AGY, Codex) or Herdr state before continuing.

## Reconciliation and failure rules

### Parallel manager teams

When a task has disjoint bounded slices, use the team workflow:

1. Record the root task and child tasks with `parent_task_id`, `team_id`, and a
   declarative scope. Validate every hierarchy relation before persistence.
   Give each team one unique `--tab-label`.
2. Create and reconcile one named Herdr tab per team. Reserve one manager
   identity per team. Start manager runs before dispatching
   workers.
3. Dispatch workers through `dispatch batch` or `run worker-start` only after
   live Git admission. Every writer must provide a dedicated branch, base SHA,
   worktree path, and worktree ID. Every file or non-file resource must be
   claimed atomically before the worker is created.
   Before each manager, worker, or reviewer start, create its pane with
   `herdr pane split TEAM_ANCHOR_PANE_ID --direction down` and start it in
   that pane, keeping all worker/reviewer panes below the manager/control pane.
4. If a claim lease expires, stop and reconcile it with live owner evidence.
   The claim is not available for reuse until the owner and exact fence token
   explicitly release it; never auto-steal it. Normal successful run
   finalization releases still-active claims.
5. Start a reviewer run linked to the worker run. It must finish successfully,
   and its evaluator run ID must be supplied to the evaluation. The reviewer
   checks the worker's exact Git head SHA; a worker cannot evaluate itself.
6. Finalize deterministically: settle turns, finish workers, release claims,
   finish linked reviewers, record exact-head evaluations, then finish the
   manager. The manager cannot finish while child workers are active.
7. Report `status executive TASK_ID` to leadership. This view contains only
   aggregate outcomes, signals, approvals, blockers, and team progress; it
   excludes prompts, transcripts, turn artifacts, and low-level worker activity.

Use the legacy `run start` path for singleton tasks. It remains bounded
compatible and does not require team or writer metadata. A string evaluator is
accepted only on that legacy singleton path; team workers require the linked
successful reviewer run.

The parallel acceptance gate requires at least three overlapping workers under
two managers and an exact-head review for every accepted worker.

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
- A team binding with a missing, foreign, or different observed workspace/tab is rejected. Reconcile
  the team tab first; do not repair the mismatch by focusing or moving an existing pane.

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
