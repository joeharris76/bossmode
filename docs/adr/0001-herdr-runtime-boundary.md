# ADR 0001: Herdr owns external-agent runtime state

Status: accepted for the MVP spike

## Decision

Use Herdr 0.8.2 directly for external-agent processes, panes, lifecycle state, native session
references, detach/reattach, and server-restart restoration. Keep task, run, turn, review,
evaluation, feedback, and promotion state in the bossmode SQLite registry.

Archive `herdr-orch` as prior art. Do not copy its state store, recipe runner, launched-kind support,
screen artifact extraction, or pane-destruction commands into this project.

## Why

`herdr-orch` implemented a stable coordinator interface and valuable fail-closed ownership checks,
but it duplicated an evolving runtime. Current Herdr ships its own agent skill and structured API,
supports the target agent kinds, exposes native session references, and restores supported sessions.
The wrapper still models a session as a string and infers results from terminal lifecycle and screen
contents.

Herdr does not correlate `agent prompt --wait` with an individual turn. The MVP closes that gap by
allowing one open turn, requiring the worker to write bounded JSON to an exact turn-specific path,
and validating its ID and status before accepting success. It does not infer success or an artifact
path from terminal text.

## Safety boundaries

- Live Herdr state is authoritative. SQLite bindings are indexes, not capabilities.
- Never adopt, prompt, replace, or close an ambiguous or foreign worker.
- Never approve trust or permission dialogs without explicit user authorization.
- Preserve `blocked`, stalled, unknown, failed, and succeeded as distinct outcomes.
- A different native session reference for an existing run is an identity mismatch, not a rebind.

## Replacement trigger

Add provider-specific resume code only if an isolated canary proves that Herdr cannot recover a
required native session after the individual agent process exits. Do not add a generic executor or
MCP layer until a second concrete implementation demonstrates behavior that the official Herdr CLI
or socket API cannot provide.

## Worker lifecycle (2a)

Owned worker = `owned_resources` `herdr_worker` with `canonical_key = herdr_worker:<session>/<name>` and receipt `{herdr_session, worker_name, agent_kind, pane_id, tab_id, workspace_id, session_source, session_agent, session_ref_kind, session_value?}`. Attached = observed without our reservation.

Contracts:
- Reserve `herdr_worker` before `herdr agent start`; on crash, `herdr agent get <name>` reconciles missing/foreign and we orphan.
- Identity = `pane_id` + `workspace_id`/`tab_id` + `agent_session {source,kind,value}` — must match receipt; pane movement changes `pane_id`, server restart may clear `agent_session` but name+receipt still required.
- Missing-worker retire is success only if no active with same canonical; name reuse after `retired/orphaned` requires generation bump via partial-unique index.
- Never close on stored name/pane alone.

Capability failures: missing `herdr` (127), timeout (124), malformed JSON — all `blocked` not `failed` for close-gate.
