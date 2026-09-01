# Manager Recovery

Read this reference only when a paired Manager is lost, unresponsive, or requires replacement. Do not use this during ordinary execution. Restrict every step to the affected Manager's specific topic.

## Authority and Containment

Live runtime state is authoritative. Stored handles are hints. Always reconcile a session against its live identity and state before acting on it.

The Executive may interrupt the failed Manager's verified live descendants solely to contain that Manager's work. The Executive must not redirect these agents or adopt, integrate, evaluate, or accept their work. Never act on a stale name, pane, or stored session handle alone.

## Resume or Replace

Resume the original Manager only if its live identity, continuation channel, and ownership are verifiable. Directly inspect the native raw JSONL session log (not a summary) to identify the Manager agent session, the last completed action, and the pending next action. If any of these three elements cannot be established, do not resume or pair a replacement.

Before pairing a replacement:

1. Reconcile the failed Manager's live descendants and contain its active writers. Leave other Managers' sessions, worktrees, and branches completely untouched.
2. Inspect the failed topic's worktrees, branches, path claims, current instructions, correction deltas, integrated revisions, and durable evidence.
3. Preserve any ambiguous or unverified work. Do not reset, clean, merge, or delete it automatically.
4. Provide the replacement Manager with a bounded handoff of verified state and route it to [manager.md](manager.md).
5. Report the replacement following the Executive reporting contract.

No replacement Manager may begin dispatch, implementation, integration, or review until it is verified live and owns the reconciled charter and work boundaries.

Recovery is entirely event-driven. Do not invent generations, clocks, background health polling, a scheduler, or a registry to simulate runtime authority. Rely on live sessions, Git, and durable authorized artifacts as your sole evidence.
