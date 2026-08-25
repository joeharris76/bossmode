# Bossmode MVP instructions

## Authority

- Live host runtime state (e.g. Antigravity/AGY, Codex) is authoritative for native subagent
  identity and ownership. Live Herdr state is authoritative for external workers (`pi`, `codex`,
  `claude`, `agy`, `grok`, `muse`); the registry stores only a reconciled binding.
- `.bossmode/control.db` is the durable task index and event log. A stored thread ID is not a
  capability and must be reconciled against live state before use.
- Required behavior belongs in this file or deterministic code. Memories are helpful recall only.

## Supervisor behavior

- Use the `bossmode` skill for registry reconciliation and dispatch.
- Record a task before delegating it and preserve the returned task ID.
- Reserve an external run before creating its worker, then bind the live Herdr identity to that run.
- Give each subagent a bounded goal, success criteria, permission scope, and output contract.
- Do not let multiple agents write the same files concurrently.
- Never adopt, prompt, replace, or close a Herdr worker from a stored name or pane ID alone.
- Never approve an agent trust or permission dialog without explicit user authorization.
- Require external evidence or an independent reviewer before recording a passing evaluation.
- Report `waiting_user` work, protected approvals, and genuine blockers separately.

## Naming policy

Bossmode's recurring naming defect has been additive aliasing: a new name is
introduced, the old one is kept, and the old one keeps canonical position. That
produced six spellings for one reconciliation behaviour, a repurposed `init`,
and single tokens naming several unrelated concepts. These rules exist to stop
it recurring.

- One canonical spelling per operation. A second spelling requires a stated
  removal release; there are none today and none should be added casually.
- A token means one thing across the CLI, the Python API, JSON keys, schema
  columns, and the docs. When two concepts want the same English word, qualify
  one of them: `agent_kind`, not `kind`.
- `status` is where an object currently sits; `outcome` is how a finished object
  ended. Do not use one for the other.
- JSON payload keys reuse the state or enum names they select. Do not invent a
  parallel vocabulary for the same values.
- Retiring the old name is part of introducing the new one, in the same change,
  including its tests and docs.
- Never repurpose a token to a disjoint meaning. Retire it and introduce a new
  one, so old invocations fail loudly instead of doing something else.
- Every user-facing spelling has a test that invokes it. An untested spelling
  cannot be removed safely later.
- Every `add_parser` and every `add_argument` carries `help=`.
- The rules cover the worker turn-result contract too: it reports `outcome`, not
  `status`. A `kind` field nested inside an object that names its own domain,
  such as an artifact's `kind` or a session reference's `kind`, is already
  qualified by that object and stays as it is.

## Learning boundary

- Treat feedback as data only after recording its source and recurrence key.
- Promotion proposals are not permission to edit memories, skills, `AGENTS.md`, or controls.
- Apply a promotion only after the user accepts it and the proposed artifact is reviewable and
  tested in proportion to risk.
- Prefer memory for context, a skill for a reusable procedure, and code or hooks for deterministic
  enforcement.

## Development

- Use `uv run` for Python commands.
- Run `uv run ruff check .`, `uv run ruff format --check .`, and `uv run pytest` before completion.
- Keep the spike standard-library-only at runtime.
