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

Bossmode adds a durable team layer while retaining the legacy singleton API.

- A root task may have child tasks. Teams belong to a root task, carry a
  manager identity and scope, and own child task slices.
- Runs have a durable type (`manager`, `worker`, or `reviewer`), parent run,
  team, and identity. Reviewer runs are linked to the worker run they evaluate.
- Writers must reserve a dedicated branch, base SHA, worktree path, and
  worktree identity. Conflicts are rejected in the dispatch transaction before
  a worker run is created.
- Resource claims use a canonical `(kind, key)` pair. File keys are absolute
  real paths; non-file keys are normalized identifiers. Claims carry a unique
  fence token and lease. Expiry changes `active` to `reconcile_required`.
  Reconciliation never auto-steals an expired claim; explicit recovery must
  release it before reuse.
- `dispatch_batch` creates manager and worker reservations atomically, so at
  least three disjoint workers may overlap under two or more managers. The
  existing `start_run` path remains single-flight compatible.
- Evaluations may reference an independent finished reviewer run. A reviewer
  identity cannot equal the worker identity or silently become a string-only
  self-evaluation.
- Executive status is a mechanically derived view of task outcomes, signals,
  approvals, blockers, and team progress. It does not include prompts,
  transcripts, turn results, or low-level worker activity. Sensitive signals
  can be stored as `[redacted]`.

## Data and migration

Schema version 6 adds `teams`, `writer_identities`, `resource_claims`, and
`task_signals`, plus nullable hierarchy and run identity fields. The v5-to-v6
migration preserves every existing task, run, turn, evaluation, feedback, and
promotion. Existing runs default to `worker`; existing `start_run` callers do
not need to create a team or writer reservation.

## Rejected alternatives

- A process-local lock cannot recover after a crash and cannot represent
  non-file resources, so it is not the canonical claim authority.
- Automatically stealing expired leases would allow a stale writer to commit
  after a new writer starts. Fail-closed reconciliation is safer.
- Interactive manager chat trees would make status and recovery depend on
  transcripts. Durable rows and bounded scopes provide coordination without
  making conversation history a control surface.

## Consequences

Parallel execution is available only when task scopes, writer identities, and
resource claims are valid. Managers still require live runtime reconciliation
when an external worker is used. The database remains the source of durable
coordination facts; live native and Herdr runtime identity remains authoritative
for actual worker ownership.
