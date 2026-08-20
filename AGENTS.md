# Continual Agent MVP instructions

## Authority

- Live Codex task state is authoritative for agent identity and ownership.
- `.continual/control.db` is the durable task index and event log. A stored thread ID is not a
  capability and must be reconciled against live state before use.
- Required behavior belongs in this file or deterministic code. Memories are helpful recall only.

## Supervisor behavior

- Use the `supervisor` skill for registry reconciliation and dispatch.
- Record a task before delegating it and preserve the returned task ID.
- Give each subagent a bounded goal, success criteria, permission scope, and output contract.
- Do not let multiple agents write the same files concurrently.
- Require external evidence or an independent reviewer before recording a passing evaluation.
- Report `waiting_user` work, protected approvals, and genuine blockers separately.

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

