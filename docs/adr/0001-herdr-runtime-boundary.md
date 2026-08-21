# ADR 0001: Herdr owns external-agent runtime state

Status: accepted for the MVP spike

## Decision

Use Herdr 0.8.2 directly for external-agent processes, panes, lifecycle state, native session
references, detach/reattach, and server-restart restoration. Keep task, run, turn, review,
evaluation, feedback, and promotion state in the continual-agent SQLite registry.

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
