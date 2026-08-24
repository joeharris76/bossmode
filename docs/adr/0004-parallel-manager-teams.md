# ADR 0004: Durable Parallel Manager Teams

**Status:** Accepted
**Date:** 2026-08-24
**Supersedes:** The deferred concurrency and manager provisions in ADR 0002

## Context

ADR 0002 selected the hybrid matrix as the post-MVP direction but deferred
implementation. A single-flight coordinator is safe for one writer, but it
cannot represent multiple bounded teams, preserve manager/worker/reviewer
identity, or explain why two otherwise-ready workers must not run together.

## Decision

Bossmode adds a durable team layer while retaining bounded legacy singleton
compatibility. The contract is fail-closed at every admission and finalization
boundary.

### Hierarchy and atomic admission

- A root task may have child tasks. Teams belong to a root task, carry a
  manager identity and scope, and own child task slices.
- Before persistence, validate every parent task, root task, parent team, team
  assignment, manager run, worker child, and reviewer link. A child from one
  team cannot be attached to another team's manager. A failed hierarchy check
  rolls back the transaction; no partial team, run, writer, or claim survives.
- Runs have a durable type (`manager`, `worker`, or `reviewer`), parent run,
  team, and identity. A reviewer run is linked to the worker run it evaluates.
- `dispatch batch` creates manager and worker reservations atomically. A
  worker is not created until all hierarchy, writer, resource, and live
  admission checks pass.

### Claims, owners, fences, and recovery

- Resource claims use a canonical `(kind, key)` pair. File keys are absolute
  real paths; non-file keys are normalized identifiers. Each claim records its
  owning run, unique fence token, lease, and status.
- A live reconciliation records evidence for the owner and its current
  activity. Lease expiry changes `active` to `reconcile_required`; that state
  is still held and cannot be reused.
- Release requires explicit live evidence that the owner no longer writes,
  the owning run ID, and the exact fence token. A mismatched owner or fence is
  rejected. Reconciliation never auto-steals a claim. Normal successful run
  finalization releases its still-active claims; an expired claim still needs
  explicit reconciliation and release.

### Live Git writer admission

Before `run worker-start` or `dispatch batch` creates a worker run, the
supervisor must inspect the live repository and reject the writer unless all of
these are true:

- the branch is dedicated and is not protected (`main`, `master`, `develop`,
  or any repository-configured protected branch);
- the path is a separate linked worktree, not the primary checkout, and the
  worktree is clean, including untracked files;
- no active writer owns the branch, canonical real worktree path, or worktree
  ID; and
- the base SHA names an existing commit in the same repository and is the
  approved base for the task.

The reservation records the branch, base SHA, worktree path, and worktree ID.
Invalid base SHAs, primary or dirty worktrees, protected branches, and
duplicate writer identities are admission failures, not reasons to create a
worker and repair state later. The registry's `--writer-json` carries these
four fields; the live Git evidence is a prerequisite supplied by the
supervisor, not a substitute for the reservation.

### Review and deterministic finalization

- Every team worker requires a separate reviewer run linked with
  `--worker-run-id`. The reviewer must have a different live identity, finish
  successfully, and be the evaluator run supplied to `evaluate`. A reviewer
  string alone is not valid for team workers; it remains bounded compatibility
  only for a legacy singleton `run start` evaluation.
- A worker's accepted result names its exact Git head SHA. The linked reviewer
  checks that exact head and records evidence tied to that SHA; reviewing a
  moving branch name or a different head is not acceptance.
- Finalize a team in a deterministic order: settle every worker turn, finish
  every worker, release or reconcile every claim, finish every linked reviewer,
  record each exact-head evaluation, then finish the manager. The manager
  cannot finish while child workers are active. The root task is accepted only
  after all required worker evaluations pass; no transcript or timing race may
  decide completion.
- Acceptance evidence must demonstrate at least three overlapping workers under
  two managers and exact-head review for each accepted worker. This is the
  minimum concurrency and soundness test for the parallel contract.

### Herdr topology and executive reporting

- Each team owns one unique expected Herdr tab label and one reconciled live
  workspace/tab location. Create and reconcile that tab before the first
  agent. Manager, worker, and reviewer bindings are admitted only when their
  observed Herdr session, workspace, and tab match that location. Singleton
  runs retain the legacy binding contract.
- The manager/control pane stays at the top; every worker and reviewer is stacked
  below it with horizontal dividers. Every invocation splits an
  explicit anchor already in that team tab with a down split using
  `herdr pane split TEAM_ANCHOR_PANE_ID --direction down` before starting the
  agent. The `--current` focused-pane option and rightward splits are
  mechanically forbidden. Missing, foreign, or ambiguous tabs and anchors block admission;
  existing panes are never moved or closed to repair them.
- Executive status is a mechanically derived view of task outcomes, signals,
  approvals, blockers, and team progress. It excludes prompts, transcripts,
  turn artifacts, and low-level worker activity. Sensitive signals can be
  stored as `[redacted]`.

## Data and migration

Schema version 7 adds `team_herdr_tabs`. The v6-to-v7 migration gives each
existing team a unique expected tab label and leaves its live tab unbound until
the supervisor observes and reconciles one. Schema version 6 added `teams`,
`writer_identities`, `resource_claims`, and `task_signals`, plus nullable
hierarchy and run identity fields. Both migrations preserve every existing
task, run, turn, evaluation, feedback, and promotion. Existing runs default to
`worker`; existing `start_run` callers do not need to create a team or writer
reservation.

## Rejected alternatives

- A process-local lock cannot recover after a crash and cannot represent
  non-file resources, so it is not the canonical claim authority.
- Automatically stealing expired leases would allow a stale writer to commit
  after a new writer starts. Fail-closed reconciliation is safer.
- Interactive manager chat trees would make status and recovery depend on
  transcripts. Durable rows and bounded scopes provide coordination without
  making conversation history a control surface.

## Consequences

Parallel execution is available only when task hierarchy, live Git admission,
writer identities, resource claims, exact-head review, and (for team runs) the
named-tab admission contract are valid. Managers still require live runtime
reconciliation when an external worker is used. The database remains the source
of durable coordination facts; live native and Herdr runtime identity remains
authoritative for actual worker ownership.
