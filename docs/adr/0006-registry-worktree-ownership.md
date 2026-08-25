# ADR 0006: Bind standard registries to their owning worktree

Status: accepted

Date: 2026-08-25

## Context

Bossmode uses automatic SQLite schema initialization and migrations. Linked worktrees can contain
different source revisions, so a command started in one worktree can otherwise open the standard
`.bossmode/control.db` of another worktree and migrate it with the wrong code. This can mutate a
primary registry for work that is still deferred or unmerged.

## Decision

Every checkout or linked worktree owns its standard `.bossmode/control.db`. `Registry` rejects a
standard registry path whose worktree contains a `.git` entry and differs from the current
checkout. The rejection occurs during construction, before parent-directory creation, SQLite open,
schema inspection, or migration.

The CLI may still accept non-standard paths for temporary or dedicated registries. A shared control
plane is a separate design: it must have a dedicated supervisor owner and an explicit adoption
protocol rather than reusing a sibling worktree's primary registry.

## Consequences

- A worker or feature worktree cannot silently migrate the primary registry of another worktree.
- Existing temporary database tests and explicit dedicated paths remain usable.
- A schema mismatch must be resolved in the owning checkout instead of bypassed with `--db`.
- Future shared-registry support must define ownership, compatibility, and adoption explicitly.

## Alternatives considered

### Continue relying on operator discipline

Rejected. The incident showed that a valid task command and a valid migration can still target the
wrong database when worktree ownership is implicit.

### Add a schema version check after opening the database

Rejected. Checking after SQLite open is too late: opening and initialization can already create
files or begin migration work. The ownership check must precede all registry I/O.

### Make every registry path globally shared

Rejected. This would couple independent worktrees to one migration and task-history authority
without defining a supervisor owner or compatibility protocol.
