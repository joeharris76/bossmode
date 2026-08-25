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
  assignment, manager run, worker child, and reviewer link. Walk the complete
  ancestor chain: a child, its parent task, its team, and its manager must stay
  in one root/team hierarchy; a child from one team cannot be attached to
  another team's manager. A failed hierarchy check rolls back the transaction;
  no partial team, run, writer, or claim survives.
- Runs have a durable run kind (`manager`, `worker`, or `reviewer`), parent run,
  team, and identity. A reviewer run is linked to the worker run it evaluates.
- `dispatch` creates manager and worker reservations atomically. A
  worker is not created until all hierarchy, writer, resource, and live
  admission checks pass.

### Claims, owners, fences, and recovery

- Resource claims use a canonical `(kind, key)` pair. File keys are absolute
  real paths; non-file keys are normalized identifiers. Each claim records its
  owning run, unique fence token, lease, and status.
- Live verification records evidence for the owner and its current activity.
  Lease expiry changes `active` to `expired`; that state
  is still held and cannot be reused.
- Release requires explicit live evidence that the owner no longer writes,
  the owning run ID, and the exact fence token, through
  `bossmode resource release`. A mismatched owner or fence is rejected.
  Verification and release never auto-steal a claim. Normal successful run
  finalization releases its still-active claims; an expired claim still needs
  explicit live verification and release.

### Live Git writer admission

Before `run start-worker` or `dispatch` creates a worker run, the
supervisor must inspect the live repository and reject the writer unless all of
these are true:

- the branch is dedicated and is not protected (`main`, `master`, `develop`,
  or any repository-configured protected branch);
- the path is a separate linked worktree, not the primary checkout, and the
  worktree is clean, including untracked files;
- no active writer owns the branch, canonical real worktree path, or worktree
  ID; and
- the worktree and base SHA belong to the registry's repository, the base SHA
  names an existing commit there, and it is the approved base for the task.

The supervisor passes the approved repository root and base explicitly through
`--approved-repository-path PATH` and `--approved-base-sha SHA`. The reservation
records the branch, base SHA, worktree path, and worktree ID.
Invalid base SHAs, unrelated repositories, primary or dirty worktrees,
protected branches, and duplicate writer identities are admission failures,
not reasons to create a worker and repair state later. A caller-supplied
repository path cannot redirect admission away from the registry repository.
The registry's `--writer-json` carries these four fields; the live Git evidence
is a prerequisite supplied by the supervisor, not a substitute for the
reservation.

### Review and deterministic finalization

- Every team worker requires a separate reviewer run linked with
  `--worker-run-id`. The reviewer must have a different live identity, finish
  successfully, and be the evaluator run supplied to `evaluate`. A reviewer
  string alone is not valid for team workers; it remains bounded compatibility
  only for a legacy singleton `run start` evaluation.
- The public `evaluate` command requires `--reviewed-head-sha SHA` for a team
  worker. The registry resolves that commit in the approved repository and
  accepts it only when it equals the worker's durable `accepted_head_sha`.
- A worker's accepted result names its exact Git head SHA. The linked reviewer
  must finish successfully, check that exact head, and record evidence tied to
  that SHA; a failed or unfinished reviewer, a moving branch name, or a
  different head is not acceptance.
- Finalize a team in a deterministic order: settle every worker turn, finish
  every worker, release every claim, finish every linked reviewer,
  record each exact-head evaluation for every child, then finish the manager.
  The manager cannot finish while child workers or reviewers are active, while
  claims remain held, or while a child lacks a passing evaluation. The root
  task is accepted only after all required worker evaluations pass; no
  transcript or timing race may decide completion.
- Acceptance evidence must demonstrate at least three overlapping workers under
  two managers and exact-head review for each accepted worker. This is the
  minimum concurrency and soundness test for the parallel contract.

### Herdr topology and executive reporting

- Each team owns exactly one unique expected Herdr tab label and one reconciled live
  workspace/tab location. Create and reconcile that tab before the first
  agent. Manager, worker, and reviewer bindings are admitted only when their
  observed Herdr session, workspace, and tab match that location. Singleton
  runs retain the legacy binding contract.
- The manager/control pane stays at the top; every worker and reviewer is stacked
  vertically below it with horizontal dividers. Every invocation splits an
  explicit anchor already in that one team tab with a down split using
  `herdr pane split TEAM_ANCHOR_PANE_ID --direction down` before starting the
  agent. The `--current` focused-pane option and rightward splits are
  mechanically forbidden. Missing, foreign, or ambiguous tabs and anchors block admission;
  existing panes are never moved or closed to repair them.
- The executive report is a mechanically derived view of task outcomes, signals,
  approvals, blockers, and team progress. It excludes prompts, transcripts,
  turn artifacts, and low-level worker activity. Sensitive signals can be
  stored as `[redacted]`.

## Data and migration

The current registry schema is version 9. `Registry.initialize()` applies each
pending migration in order and updates the single schema-version row in the
same transaction; a newer unsupported version or a missing migration fails
closed. The relevant parallel-team migrations are:

- v5 -> v6 splits turn status from outcome and qualifies feedback category.
- v6 -> v7 adds task hierarchy, team and run identity fields, reviewer-run
  links, and the parallel-team tables while preserving legacy lifecycle rows.
- v7 -> v8 adds `team_herdr_tabs`, assigning every existing team one unique
  expected label while leaving its live tab unbound until reconciliation.
- v8 -> v9 adds resource-claim expiry and release evidence.
- v9 -> v10 adds task `approved_base_sha`, evaluation `reviewed_head_sha`, and
  writer `repository_path` and `accepted_head_sha` fields used by explicit
  repository admission and exact-head acceptance.

Earlier migrations add Herdr bindings and correlated turns, normalize binding
constraints, add turn prompts, and create maintenance records. All migrations
preserve existing tasks, runs, turns, evaluations, feedback, and promotions;
existing runs default to `worker`, and legacy singleton `start_run` callers do
not need team or writer metadata.

## Rejected alternatives

- A process-local lock cannot recover after a crash and cannot represent
  non-file resources, so it is not the canonical claim authority.
- Automatically stealing expired leases would allow a stale writer to commit
  after a new writer starts. Fail-closed verification and release are safer.
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
