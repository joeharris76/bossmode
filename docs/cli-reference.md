# CLI reference

The Bossmode CLI is the precise registry interface used by supervisor agents and operators. Users
normally express the same intent through the [prompt guide](prompt-guide.md).

Parsed commands that reach the Bossmode handler write structured JSON to standard output and exit
`0` on success. Caught registry, JSON-value, SQLite, and scheduler errors write JSON to standard
error and exit `2`. Argparse help and usage remain text: help exits `0`, while missing arguments and
invalid choices exit `2` before the JSON handler runs.

The global `--db PATH` option selects the registry. It defaults to `.bossmode/control.db` or the
`BOSSMODE_DB` environment variable.

## Installation

| Command | Purpose |
|---|---|
| `bossmode init` | Install this version's Bossmode skill in the current project. |
| `bossmode init --project-dir PATH` | Install the skill in another project directory. |

The command creates `.agents/skills/bossmode/SKILL.md`. It is idempotent when that file already
matches this Bossmode version, and it refuses to overwrite different content or follow a symlinked
skill directory.

## Reconciliation

| Command | Purpose |
|---|---|
| `bossmode` | Reconcile session state and return the next dispatchable task. |
| `bossmode reconcile` | Same as the default command. |
| `bossmode next` | Alias for `reconcile`. |
| `bossmode supervisor tick` | Supervisor-form alias for the default command. |
| `bossmode supervisor reconcile` | Alias for `supervisor tick`. |
| `bossmode supervisor next` | Alias for `supervisor tick`. |

## Tasks

| Command | Required arguments | Optional arguments |
|---|---|---|
| `task create` (`task add`) | `--title`, `--goal`, `--success-criteria` | `--state {backlog,ready}`, `--priority INT`, `--permissions-json OBJECT`, `--next-action`, `--parent-task-id`, `--team-id`, `--task-kind`, `--scope-json OBJECT`, `--approved-base-sha SHA` |
| `task list` | — | repeatable `--state STATE` |
| `task show TASK_ID` | `TASK_ID` | — |
| `task transition TASK_ID STATE` | `TASK_ID`, `STATE`, `--actor`, `--reason` | `--evidence`, `--next-action`, `--blocked-on` |

Task states are `backlog`, `ready`, `running`, `evaluating`, `succeeded`, `failed`, `blocked`,
`waiting_user`, and `archived`. Task creation accepts only `backlog` or `ready`; later transitions
must follow the registry state machine.

`task transition` allows these explicit changes:

| From | To |
|---|---|
| `backlog` | `ready`, `archived` |
| `ready` | `blocked`, `archived` |
| `evaluating` | `ready`, `blocked`, `archived` |
| `waiting_user` | `ready`, `blocked`, `archived` |
| `blocked` | `ready`, `archived` |
| `succeeded` | `archived` |
| `failed` | `ready`, `archived` |
| `running`, `archived` | no explicit `task transition` target |

Lifecycle commands own the remaining changes: `run start` moves `ready` to `running`; `run finish`
moves `running` to `evaluating`, `waiting_user`, `blocked`, or `failed`; team
`dispatch batch` reserves multiple managers and workers atomically; and
evaluation moves `evaluating` to `succeeded` or `failed`.

## Parallel manager teams and Herdr tabs

| Command | Required arguments | Optional arguments |
|---|---|---|
| `team create ROOT_TASK_ID` | `--name`, `--manager-identity-json` | `--scope-json`, `--parent-team-id`, `--tab-label` |
| `team bind-tab TEAM_ID` | `--herdr-session`, `--workspace-id`, `--tab-id`, `--observed-tab-label` | — |
| `team list` | — | `--root-task-id` |
| `team show TEAM_ID` | `TEAM_ID` | — |
| `run manager-start TEAM_ID` | `--identity-json` | `--model`, `--reasoning-effort` |
| `run worker-start TASK_ID` | `--manager-run-id`, `--identity-json`, `--writer-json` | `--repository-path PATH`, `--resources-json`, `--lease-seconds`, `--model`, `--reasoning-effort` |
| `run reviewer-start TASK_ID` | `--worker-run-id`, `--identity-json` | `--model`, `--reasoning-effort` |

| Command | Required arguments | Optional arguments |
|---|---|---|
| `dispatch batch ROOT_TASK_ID` | `--managers-json`, `--workers-json` | — |
| `resource reconcile` | — | `--now ISO_TIMESTAMP` |
| `resource release CLAIM_ID` | `--run-id`, `--fence-token`, `--evidence` | — |
| `status executive TASK_ID` | `TASK_ID` | — |
| `signal TASK_ID KIND` | `TASK_ID`, `KIND {decision,blocker,approval}`, `--content` | `--source-run-id`, `--team-id`, `--redacted` |

Create the Herdr tab with its durable label before the first agent, reconcile it
with `team bind-tab`, and check the observed workspace and tab IDs. The command
is idempotent for the same live location and rejects a different location or
observed label. Team manager, worker, and reviewer bindings then require that
same Herdr session, workspace, and tab. The legacy `run start` singleton path
does not require a team tab.

`run worker-start` and `dispatch batch` are admission points, not worker
creation shortcuts. Before invoking either command, the supervisor must
reconcile live Git evidence against the registry's repository: the branch is dedicated and not protected
(`main`, `master`, `develop`, or repository-configured protected branches); the
worktree is a clean linked worktree and not the primary checkout; the branch,
canonical real path, and worktree ID are not duplicates; and the base SHA is an
existing commit in that same repository. A supplied `--repository-path` cannot
redirect admission to an unrelated repository. A failed check rejects the
worker before its run, writer, or claims are persisted. `--writer-json` records
the branch, base SHA, worktree path, and worktree ID; it does not bypass live
admission.

Hierarchy validation is also before persistence: root, every parent in the
ancestor chain, team, manager, child, and reviewer links must all belong to the
same valid hierarchy. Batch dispatch is atomic and leaves no partial team or
worker records after a failed check.

For every team agent invocation, create a pane inside that one named tab before
starting the agent. Keep the manager/control pane at the top and stack
worker/reviewer panes vertically below it with horizontal dividers:

```bash
herdr pane split TEAM_ANCHOR_PANE_ID --direction down --cwd "$PWD" --no-focus
herdr agent start WORKER_NAME --kind claude --pane NEW_PANE_ID
```

The anchor must already be in the reconciled team tab. The `--current`
focused-pane option and rightward splits are mechanically forbidden; an unrelated pane or tab is
not a valid parent. If the tab or anchor is missing or ambiguous, stop and
reconcile it rather than moving or closing panes. Do not create a second team
tab to work around an ambiguity.

## Runs

| Command | Required arguments | Optional arguments |
|---|---|---|
| `run start TASK_ID` | `TASK_ID`, `--role` | `--thread-id`, `--model`, `--reasoning-effort` |
| `run show RUN_ID` | `RUN_ID` | — |
| `run finish RUN_ID` | `RUN_ID`, `--outcome`, `--summary` | `--artifacts-json ARRAY`, `--tokens`, `--duration-seconds`, `--retries`, `--blocked-on`, `--accepted-head-sha SHA` |
| `run reconcile-accepted-head RUN_ID` | `RUN_ID`, `--accepted-head-sha SHA`, `--evidence TEXT` | aliases: `accepted-head-reconcile`, `reconcile-head` |

Run outcomes are `waiting_user`, `blocked`, `succeeded`, and `failed`. A successful run moves its
task to `evaluating`, not final success.

For a legacy finished team worker whose schema-upgraded writer row has no accepted head, use
`run reconcile-accepted-head`. The command requires a finished successful team worker, the recorded
repository/worktree/branch to match live Git, the supplied SHA to be the existing live current
commit, and non-empty reconciliation evidence. It records that evidence in the worker task's
event history and performs a one-time NULL-to-SHA assignment; an existing accepted head cannot be
overwritten.

## Herdr bindings

| Command | Required arguments | Optional arguments |
|---|---|---|
| `herdr bind RUN_ID` | `RUN_ID`, `--herdr-session`, `--worker`, `--kind` | `--status {pending,live,blocked,unknown}`, `--session-source`, `--session-agent`, `--session-ref-kind {id,path}`, `--session-value`, `--pane-id`, `--tab-id`, `--workspace-id` |
| `herdr show RUN_ID` | `RUN_ID` | — |

`herdr bind` records an observed identity. For team runs, its observed tab and
workspace must match the team's reconciled tab. It does not prove the worker is
currently live. Reconcile the binding against live Herdr state before
prompting, interrupting, continuing, or closing.

## Correlated Herdr turns

| Command | Required arguments | Optional arguments |
|---|---|---|
| `turn start RUN_ID` | `RUN_ID`, `--purpose`, `--prompt` | — |
| `turn show TURN_ID` | `TURN_ID` | — |
| `turn finish TURN_ID` | `TURN_ID`, `--status` | `--summary`, `--lifecycle-evidence` |

Turn purposes are `task`, `correction`, `clarification`, and `review_follow_up`. Terminal statuses
are `blocked`, `succeeded`, `failed`, and `unknown`. A successful finish validates the exact bounded
JSON result allocated by `turn start`; terminal text alone is not success evidence. `--summary` is
required when the status is `blocked`, `failed`, or `unknown`. For `succeeded`, the validated result
file supplies the summary; an optional `--summary` must match that file exactly.

## Evaluation and feedback

| Command | Required arguments | Optional arguments |
|---|---|---|
| `evaluate TASK_ID` | `TASK_ID`, `--run-id`, `--evaluator`, one of `--passed` or `--failed`, `--evidence` | `--evaluator-run-id`, `--score`, `--notes` |
| `feedback TASK_ID` | `TASK_ID`, `--kind`, `--key`, `--content` | `--run-id` |

Feedback kinds are `preference`, `correction`, `failure`, and `observation`. The key should be a
stable, narrowly scoped recurrence key. Every team worker evaluation requires a
finished successful reviewer run linked with `--evaluator-run-id`; a reviewer
string alone is bounded compatibility only for a legacy singleton `run start`.
The evaluator must be independent of the worker. If provided, `--score` must be
between `0` and `1`, inclusive.

## Promotions

| Command | Purpose |
|---|---|
| `promotion propose` | Generate proposals from qualifying feedback and evaluation evidence. |
| `promotion list [--status STATUS]` | List all proposals or filter by `proposed`, `accepted`, `rejected`, or `applied`. |
| `promotion accept PROMOTION_ID` | Record user acceptance for implementation. |
| `promotion reject PROMOTION_ID` | Reject a proposal. |
| `promotion apply PROMOTION_ID` | Record that an accepted artifact was implemented and verified. |
| `promotion set PROMOTION_ID STATUS` | Explicitly set `accepted`, `rejected`, or `applied`. |

These commands change proposal state only. They do not create or edit the proposed memory, skill,
instruction, or control artifact.

## Maintenance and scheduling

| Command | Required arguments | Optional arguments |
|---|---|---|
| `maintenance` | — | global `--db PATH` |
| `schedule install` | — | `--interval SECONDS` (default `3600`), `--cron EXPR`, `--target {maintenance,reconcile}`, `--repo-dir PATH`, `--log-path PATH` |
| `schedule status` | — | `--repo-dir PATH`, `--log-path PATH` |
| `schedule uninstall` | — | `--repo-dir PATH` |

`--interval` works with launchd and crontab and must be at least 60 seconds. Linux crontab can
represent only exact supported intervals: below one hour, use a whole number of minutes that
divides evenly into an hour; from one hour through one day, use a whole number of hours that
divides evenly into a day. Custom `--cron` expressions are Linux-only and override the interval
there; launchd rejects them. Installation and removal mutate host state, so they require explicit
user authorization.

## Operational rules

- Run the naked `bossmode` command before dispatch and after material state changes.
- For singleton work, dispatch only the task returned in `dispatch`; use
  `dispatch batch` for disjoint team children.
- Reserve an external run before creating its Herdr worker.
- Admit live Git writer state from the registry's repository before creating a
  team worker; reject unrelated, protected, primary, dirty, duplicate, or
  invalid-base worktrees.
- Claims are owned and fenced. Expiry is `reconcile_required`, not availability;
  `resource release` requires live owner evidence and the matching owner/fence
  pair, and claims are never auto-stolen.
- Treat live native-runtime or Herdr identity as authoritative; stored IDs are indexes.
- Preserve `blocked`, `failed`, `unknown`, and `waiting_user` as distinct outcomes.
- Require an independent evaluation before a task reaches `succeeded`.
- Finalize a team only after all workers and reviewers are terminal, claims are
  released, every child has a passing exact-head evaluation, and every linked
  reviewer finished successfully. The manager cannot finish while child
  workers or reviewers are active or while any child lacks that evaluation.
- Acceptance evidence must show at least three overlapping workers under two
  managers and exact-head review.
- Treat promotion acceptance and verified artifact application as separate actions.

See the [supervisor protocol](agent-workflow.md) for the full command sequence, result schema, live
identity rules, and recovery behavior.
