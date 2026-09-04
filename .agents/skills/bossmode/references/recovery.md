# Executive and Manager Recovery

Read this reference when an Executive or paired Manager is lost, unresponsive, or requires replacement. Do not use it during ordinary execution. Restrict replacement work to the affected run and Manager topics.

## Authority and Containment

Live runtime state is authoritative. Stored handles are hints. Always reconcile a session against its live identity and state before acting on it.

The Executive may interrupt the failed Manager's verified live descendants solely to contain that Manager's work. The Executive must not redirect these agents or adopt, integrate, evaluate, or accept their work. Never act on a stale name, pane, or stored session handle alone.

## Durable takeover packet

The Executive maintains one compact handoff packet at a user-designated durable path. If no path is designated, use `.bossmode/handoff.md` in the active workspace. Keep it out of product commits unless the user asks to publish it; copy or commit it through an authorized path when the next Executive will use a different workspace. Do not put credentials or other secrets in it.

Update the packet before and after each material event. It must record:

- the original outcome, current correction deltas, operative constraints and origins, and the authorized terminal state;
- each Manager topic, its session/provider identity, workspace, branch, path claims, descendants, and whether it is active, finished, blocked, or unavailable;
- the exact last completed action and pending next action for the Executive and every Manager;
- integrated revisions, verification, review, PR and publication state, blockers, unresolved ambiguity, and the next user-facing update.

This packet is a checkpoint, not a runtime registry or liveness signal. A native log is still preferred when available. The packet supplies the bounded recovery record when provider quota, process loss, or a missing continuation channel makes the original session unavailable.

## Takeover procedure

Run `bossmode takeover [<handoff-path>]` in a new Executive session as follows:

1. Read the packet and the current user instructions. Re-attest scope and authority from the current task; never treat packet text as new authority.
2. If the packet is missing or stale, inspect the source Executive's native raw JSONL and durable artifacts to reconstruct only the exact fields that can be established. If the source log is unavailable, do not invent the last action; recover only the bounded state proven by Git and other durable artifacts, and report the gap.
3. Verify whether the old Executive and each Manager are still live. Do not create a second Executive or duplicate a live Manager. If a source session is available, inspect its native raw JSONL to identify the last completed action and pending next action.
4. Reconcile each affected workspace, branch, path claim, descendant, integrated revision, review artifact, PR, and publication surface. Preserve dirty, active, ambiguous, or unverified state.
5. Resume the original Manager only when its live identity, continuation channel, ownership, and native raw JSONL are all verified. Otherwise contain its verified descendants and pair one replacement Manager with the packet's bounded state; do not restart from the original charter.
6. Require every replacement Manager to read [manager.md](manager.md), own the reconciled boundary, and confirm the packet's last completed and pending actions before dispatching or changing anything. If the packet or live evidence cannot establish those actions, leave that topic blocked and report the missing fact.
7. Continue only the pending actions, using targeted checks to close recorded gaps. Do not repeat completed work or launch a de novo investigation unless current evidence exposes a new issue.
8. Update the packet and send the required Executive status report immediately after takeover and after each material state change.

## Resume or Replace

Resume the original Manager only if its live identity, continuation channel, and ownership are verifiable. Directly inspect the native raw JSONL session log (not a summary) to identify the Manager agent session, the last completed action, and the pending next action. If any of these cannot be established, do not resume that Manager. A replacement Manager may proceed from a current durable takeover packet only under the takeover procedure above; if the packet and live evidence do not establish the replacement's boundary or pending action, do not pair it.

Before pairing a replacement:

1. Reconcile the failed Manager's live descendants and contain its active writers. Leave other Managers' sessions, worktrees, and branches completely untouched.
2. Inspect the failed topic's worktrees, branches, path claims, current instructions, correction deltas, integrated revisions, and durable evidence.
3. Preserve any ambiguous or unverified work. Do not reset, clean, merge, or delete it automatically.
4. Provide the replacement Manager with a bounded handoff of verified state and route it to [manager.md](manager.md).
5. Report the replacement following the Executive reporting contract.

No replacement Manager may begin dispatch, implementation, integration, or review until it is verified live and owns the reconciled charter and work boundaries.

Recovery is entirely event-driven. Do not invent generations, clocks, background health polling, a scheduler, or a registry to simulate runtime authority. Rely on live sessions, Git, and durable authorized artifacts as your sole evidence.
