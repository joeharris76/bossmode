# CLI reference

The Bossmode CLI is the precise registry interface used by supervisor agents and operators. Users
normally express the same intent through the [prompt guide](prompt-guide.md).

Parsed commands that reach the Bossmode handler write structured JSON to standard output and exit
`0` on success. Caught registry, JSON-value, SQLite, and scheduler errors write JSON to standard
error and exit `2`. Argparse help and usage remain text: help exits `0`, while missing arguments and
invalid choices exit `2` before the JSON handler runs.

The global `--db PATH` option selects the registry. It defaults to the current checkout's
`.bossmode/control.db` or the `BOSSMODE_DB` environment variable. Each linked worktree owns its
standard registry path: Bossmode rejects a path resolving to a sibling worktree's
`.bossmode/control.db` before opening SQLite, inspecting the schema, or applying migrations. Run
the command from the checkout that owns a newer registry; do not use `--db` to bypass a schema
mismatch.

## Installation

| Command | Purpose |
|---|---|
| `bossmode install-skill` | Install this version's Bossmode skill in the current project. |
| `bossmode install-skill --project-dir PATH` | Install the skill in another project directory. |

The command creates `.agents/skills/bossmode/SKILL.md`. It is idempotent when that file already
matches this Bossmode version, and it refuses to overwrite different content or follow a symlinked
skill directory.

`install-skill` never opens a registry. It rejects the global `--db` option rather than accepting
and discarding it, and it does not create `.bossmode/control.db`. The registry is created on the
first command that needs it.

## Reconciliation

| Command | Purpose |
|---|---|
| `bossmode` | Converge the registry and report control-plane state. |
| `bossmode reconcile` | The same command, named explicitly. |

These are the only two spellings. There are no aliases.

Reconciliation is read-shaped but performs real writes. It creates the registry and applies any
pending schema migration, then materialises promotion proposals from recorded feedback before
reporting. Treat it as a mutating command, not an inspection.

It returns one bucket per task state plus the promotion queues:

| Key | Contents |
|---|---|
| `next_task` | The single `ready` task to dispatch, or `null` while other work is in flight. |
| `running` | Tasks in `running`, with nested runs, bindings, and turns. |
| `evaluating` | Tasks in `evaluating`, awaiting an independent evaluation. |
| `waiting_user` | Tasks in `waiting_user`. |
| `blocked` | Tasks in `blocked`. |
| `new_promotion_proposals` | Proposals created by this invocation. |
| `promotion_proposals` | All proposals currently in `proposed`. |

## Tasks

| Command | Required arguments | Optional arguments |
|---|---|---|
| `task create` (`task add`) | `--title`, `--goal`, `--success-criteria` | `--state {backlog,ready}`, `--priority INT`, `--permissions-json OBJECT`, `--next-action` |
| `task list` | — | repeatable `--state STATE` |
| `task show TASK_ID` | `TASK_ID` | — |
| `task transition TASK_ID STATE` | `TASK_ID`, `STATE`, `--actor`, `--reason` | `--evidence`, `--next-action`, `--blocked-on` |

Task states are `backlog`, `ready`, `running`, `evaluating`, `succeeded`, `failed`, `blocked`,
`waiting_user`, and `archived`. Task creation accepts only `backlog` or `ready`; later transitions
must follow the registry state machine.

`task transition` accepts only `ready`, `blocked`, and `archived` as destinations. The other six
task states are never valid explicit targets, so they are not offered as choices. These are the
allowed changes:

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
moves `running` to `evaluating`, `waiting_user`, `blocked`, or `failed`; and evaluation moves
`evaluating` to `succeeded` or `failed`.

## Runs

| Command | Required arguments | Optional arguments |
|---|---|---|
| `run start TASK_ID` | `TASK_ID`, `--role` | `--thread-id`, `--model`, `--reasoning-effort` |
| `run show RUN_ID` | `RUN_ID` | — |
| `run finish RUN_ID` | `RUN_ID`, `--outcome`, `--summary` | `--artifacts-json ARRAY`, `--tokens`, `--duration-seconds`, `--retries`, `--blocked-on` |

Run outcomes are `waiting_user`, `blocked`, `succeeded`, and `failed`. A successful run moves its
task to `evaluating`, not final success.

## Herdr bindings

| Command | Required arguments | Optional arguments |
|---|---|---|
| `herdr bind RUN_ID` | `RUN_ID`, `--herdr-session`, `--worker`, `--agent-kind` | `--status {pending,live,blocked,unknown}`, `--session-source`, `--session-agent`, `--session-ref-kind {id,path}`, `--session-value`, `--pane-id`, `--tab-id`, `--workspace-id` |
| `herdr show RUN_ID` | `RUN_ID` | — |

`herdr bind` records an observed identity. It does not prove the worker is currently live. Reconcile
the binding against live Herdr state before prompting, interrupting, continuing, or closing.

## Correlated Herdr turns

| Command | Required arguments | Optional arguments |
|---|---|---|
| `turn start RUN_ID` | `RUN_ID`, `--purpose`, `--prompt` | — |
| `turn show TURN_ID` | `TURN_ID` | — |
| `turn finish TURN_ID` | `TURN_ID`, `--outcome` | `--summary`, `--lifecycle-evidence` |

Turn purposes are `task`, `correction`, `clarification`, and `review_follow_up`. Terminal outcomes
are `blocked`, `succeeded`, `failed`, and `unknown`. A successful finish validates the exact bounded
JSON result allocated by `turn start`; terminal text alone is not success evidence. `--summary` is
required when the outcome is `blocked`, `failed`, or `unknown`. For `succeeded`, the validated result
file supplies the summary; an optional `--summary` must match that file exactly.

A turn records `status` (`running` or `finished`) and `outcome` separately, matching runs. The
result-file contract uses `status` for the worker's self-reported terminal value, which the registry
validates before storing it as the turn's `outcome`.

## Evaluation and feedback

| Command | Required arguments | Optional arguments |
|---|---|---|
| `evaluate TASK_ID` | `TASK_ID`, `--run-id`, `--evaluator`, one of `--passed` or `--failed`, `--evidence` | `--score`, `--notes` |
| `feedback TASK_ID` | `TASK_ID`, `--category`, `--key`, `--content` | `--run-id` |

Feedback categories are `preference`, `correction`, `failure`, and `observation`. The key should be a
stable, narrowly scoped recurrence key. The evaluator must be independent of the worker. If
provided, `--score` must be between `0` and `1`, inclusive.

## Promotions

| Command | Purpose |
|---|---|
| `promotion propose` | Generate proposals from qualifying feedback and evaluation evidence. |
| `promotion list [--status STATUS]` | List all proposals or filter by `proposed`, `accepted`, `rejected`, or `applied`. |
| `promotion accept PROMOTION_ID` | Record user acceptance for implementation. |
| `promotion reject PROMOTION_ID` | Reject a proposal. |
| `promotion apply PROMOTION_ID` | Record that an accepted artifact was implemented and verified. |

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
- Dispatch only the single task returned in `next_task`.
- Reserve an external run before creating its Herdr worker.
- Treat live native-runtime or Herdr identity as authoritative; stored IDs are indexes.
- Preserve `blocked`, `failed`, `unknown`, and `waiting_user` as distinct outcomes.
- Require an independent evaluation before a task reaches `succeeded`.
- Treat promotion acceptance and verified artifact application as separate actions.

See the [supervisor protocol](agent-workflow.md) for the full command sequence, result schema, live
identity rules, and recovery behavior.
