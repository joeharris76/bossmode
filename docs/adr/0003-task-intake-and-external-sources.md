# ADR 0003: Bossmode owns task intake and treats trackers as optional sources

Status: accepted; implementation deferred beyond the current MVP spike

Date: 2026-08-24

## Context

Bossmode currently accepts tasks through `bossmode task create` and dispatches one ready task at a
time. This is sufficient when a user already has bounded work orders, but it does not turn a larger
goal into enough reviewed tasks to sustain an agent workflow.

Task supply and task execution are separate concerns. Bossmode must work for users who do not use a
project tracker, while also allowing tools such as todo-db to provide work without becoming a
required dependency or a second copy of Bossmode's execution state.

todo-db and Bossmode have intentional overlap but different authority:

- todo-db owns planning items, priority, dependencies, work units, scope rules, claims, and
  verification definitions;
- Bossmode owns worker runs, turns, runtime bindings, artifacts, independent evaluations, feedback,
  and promotion proposals.

Copying a live todo-db backlog into `.bossmode/control.db` would give both systems mutable task
lifecycles and completion gates. Reconciliation would then have to resolve conflicting priority,
claim, blocked, and completion states.

## Decision

Bossmode will own a source-independent task-intake workflow. External trackers will remain optional
sources that attach selected work to Bossmode execution records; they will not replace or share the
Bossmode registry.

### Built-in intake

Bossmode will support two built-in paths:

1. **Direct tasks:** retain explicit task creation for users who already have a bounded goal,
   success criteria, permissions, priority, and expected evidence.
2. **Reviewed mission planning:** allow a user to provide one larger objective. A planning run will
   propose bounded tasks, dependencies, permissions, artifacts, and evaluation requirements. An
   independent review will check the proposal before approved tasks are materialized in
   `.bossmode/control.db`.

Planning output is a proposal, not permission to execute. Architectural decisions, expanded
permissions, protected approvals, and other user decisions remain explicit gates.

### External task sources

An external source adapter will activate one selected item at a time instead of importing or
synchronizing a backlog.

For todo-db, the adapter will use its supported agent CLI lifecycle (`next`, `take`, `context`,
`progress`, and `finish`) through the adopting project's wrapper. todo-db remains authoritative for
the item and its claim. Bossmode records the execution attempts and their evidence.

Each attached task must have an idempotent source binding containing at least:

- source kind;
- source project identity and repository;
- source item identifier;
- a digest of the planning context used to create the execution record.

The binding must not store a live claim token or treat a stored actor, session, or claim identifier
as authority. Reconciliation must check the source before dispatch and stop when the item is
terminal, blocked, missing, ambiguous, or held by another principal.

A passing Bossmode evaluation is evidence for the source item. It does not bypass todo-db work-unit,
scope, audit, claim-generation, or verification gates. The source system owns its final completion
transition.

### Scheduling boundary

Task intake may keep the ready queue supplied, but it does not change the current single-flight
dispatcher. Dependency-aware multi-slot scheduling is a separate post-MVP decision. It requires
path-permission conflict checks, worker-capacity accounting, and independent reviewer capacity
before Bossmode may run multiple writers concurrently.

## Safety and compatibility

- Bossmode must continue to work with no tracker configuration or external task source.
- External adapters should invoke a versioned CLI without a shell, bound output size, require
  structured JSON, serialize mutations, and fail closed on authentication or protocol errors.
- Bossmode must not automatically execute verification commands obtained from a shared tracker.
- The runtime remains standard-library-only. An adapter should use a subprocess boundary rather
  than add todo-db as a Python dependency.
- Crashes between source claim and Bossmode attachment must recover the existing claim
  idempotently instead of selecting another item.
- Hosted todo-db mutations remain opt-in until todo-db certifies their commit-outcome behavior
  against the real hosted service.

todo-db's bounded Pi adapter is the existing pattern to extend: it discovers the project wrapper,
uses a sanitized environment, invokes without a shell, caps output, validates JSON, and serializes
mutations. A generic plugin framework is not justified until a second external source demonstrates
a stable common contract.

## Alternatives considered

### Require todo-db

Rejected. Bossmode must accept direct tasks and larger user objectives without requiring a separate
tracker, hosted service, or release lifecycle.

### Bulk import or bidirectional synchronization

Rejected. Two mutable task stores would create ambiguous authority for priority, claims,
dependencies, blocked state, verification, and completion.

### Manual task creation only

Rejected as the long-term product model. It preserves a small MVP but makes users decompose and
refill the queue by hand.

### Build a generic task-source framework now

Deferred. Implement the built-in planning path first, then prove a narrow todo-db adapter. Extract a
general source interface only after another real adapter needs the same contract.

## Consequences

- Users without todo-db can give Bossmode a mission and approve a reviewed queue of work.
- todo-db users keep one canonical planning and claim lifecycle while gaining Bossmode execution,
  evaluation, and learning records.
- Bossmode needs a mission/proposal model and an idempotent source-binding record before external
  activation is safe.
- Queue supply can be developed and tested independently from parallel dispatch.
- The current CLI, single-flight scheduler, registry schema, and todo-db integration remain
  unchanged until follow-up implementation is separately approved.

## Follow-up sequence

1. Pilot mission decomposition through the existing direct task workflow and record the concrete
   gaps.
2. Define and test a structured mission proposal and independent review gate.
3. Materialize approved proposals atomically and idempotently into the Bossmode registry.
4. Pilot one local todo-db item through claim, attachment, execution, evaluation, and source
   completion.
5. Add deterministic source reconciliation and recovery tests.
6. Evaluate multi-slot dispatch separately under ADR 0002's path-boundary and independent-quality
   guidance.
