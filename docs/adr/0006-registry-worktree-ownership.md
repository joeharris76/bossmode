# ADR 0006: Bind the operational registry to the primary checkout

Status: accepted; supersedes this ADR's 2026-08-25 per-worktree decision

Date: 2026-08-25

## Context

Bossmode task, run, evaluation, and resource history must survive worker worktree removal. Git does
not merge `.bossmode/control.db`, so a registry in each linked worktree fragments that history and
has no safe automatic merge-back protocol. The primary checkout is the one durable operational
owner already available for a repository.

Automatic SQLite initialization creates another risk. A linked worktree can run different code,
and opening an unowned database can create files, enable WAL, or start a migration before a late
ownership check rejects it. Path names and the caller's current directory are not durable identity:
they do not detect copied databases, alternate paths, symlinks, same-origin clones, or a caller that
changes directory.

## Prior art reconciliation

- Supersede the original ADR 0006 decision that every worktree owns an operational registry. Keep
  only its fail-before-I/O safety requirement.
- Supersede the basename- and current-directory-based ownership helper in `registry.py`. Extend its
  rejection intent with immutable repository identity and no-follow path checks.
- Extend ADR 0006's canonical CLI naming rule: `registry create` is the only registry-creation
  spelling. The retired `init` command remains retired.
- Extend the checksum-bound migration ledger and verified per-transition backups introduced in
  schema 7. Registry identity is a new schema 8 transition; it does not alter the schema 6 to 7
  checksum or reuse the closed PR 6 schema versions.
- Extend the existing scheduler adapters only at the CLI boundary. Scheduler operations use the
  validated primary checkout and report the owning registry ID; they cannot select a different
  `--repo-dir`.
- Do not adopt PR 6's team tables or migrations. PR 6 contains no registry-identity implementation
  and remains read-only prior art for later feature PRs.

## Decision

Each Git repository has at most one operational registry. Its canonical path is
`<primary-checkout>/.bossmode/control.db`. The registry stores one immutable identity row with:

- a generated `registry_id`;
- `registry_role=operational`;
- the exact `remote.origin.url`;
- the absolute Git common directory;
- the absolute primary checkout;
- creation time and versioned creation metadata.

`bossmode registry create` is the only command that may create or upgrade this operational
registry. It succeeds only from an unambiguous primary checkout and only at the canonical path.
Every other operational command is open-only: an absent database, missing identity, mismatched
identity, linked-worktree caller, copied database, wrong repository, noncanonical path, or symlink
fails during read-only preflight before directory creation, SQLite write-open, initialization, or
migration. Each operational SQLite open must match the preflighted database device and inode, and
that binding plus live Git authority is checked again before and after commit. The same validation
runs before direct Python transaction helpers open SQLite for write.

Explicit non-repository database paths remain available for tests and certification. They receive
`registry_role=ephemeral`, claim no repository URL or Git paths, and may never be selected, copied,
or upgraded into operational authority. There is no importer, merge-back, or implicit adoption
path.

Scheduler install, status, and uninstall require a validated operational identity. The recorded
primary checkout owns the native scheduler entry. An explicit `--repo-dir` must contain no symlink
component and must match that owner lexically.

## Consequences

- The supervisor writes durable bookkeeping through one primary-checkout database while workers
  use linked worktrees only for repository artifacts.
- Removing a worker worktree cannot remove the operational history.
- Repository relocation, origin changes, or primary-checkout replacement fail closed. A future
  explicit adoption design must preserve history and prove identity; silent rebinding is forbidden.
- Repository URL comparison deliberately uses the exact nonempty `remote.origin.url` reported by
  Git. Equivalent URL spellings fail closed as an availability limitation; normalizing or rebinding
  them would weaken the current identity contract and requires a future explicit adoption design.
- Schema 7 registries require the explicit `registry create` upgrade. The schema 7 to 8 transition
  retains its own verified backup and checksum-bound ledger row.
- Temporary certification remains possible without creating a second operational authority.

## Alternatives considered

### Keep one operational registry per worktree and merge later

Rejected. SQLite histories have generated identifiers, transitions, and evaluations with no safe
Git merge or collision protocol, and `.bossmode` is ignored.

### Accept any database selected by `--db`

Rejected. A path is not authority. This would allow copied, symlinked, linked-worktree, and
ephemeral databases to masquerade as the repository control plane.

### Infer ownership only from the current directory

Rejected. The caller may be outside the repository or may change directories. The stored identity
and live Git common directory establish authority; current-directory Git state is only a caller
safety check.

### Add compatibility aliases such as `init` or `registry init`

Rejected. Bossmode's naming policy requires one canonical spelling and forbids repurposing the
retired `init` token.
