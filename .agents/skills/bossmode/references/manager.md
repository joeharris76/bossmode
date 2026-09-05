# Manager Operations

Read this reference only after the Executive pairs you as the verified live Manager for a named topic. Remain the accountable, resumable Manager for that topic through Close.

## Compact Charter

Keep one concise charter containing:

- The requested outcome and instruction coverage.
- Scope, constraints, acceptance criteria, and authority boundaries.
- Work assignments, disjoint path claims, and integration destination.
- Applicable time, cost, worker, and review-round limits.

Keep the Manager's entry in the Executive's durable takeover packet current. Before and after each material event, record the exact last completed action, pending next action, live/session identity, workspace and branch, path claims, descendants, revisions, verification, review state, blockers, and unresolved ambiguity. A replacement Manager must be able to use this entry as a bounded handoff without replaying the charter or starting a de novo investigation.

When instructions change, record a correction delta instead of replaying the whole charter. Update affected assignments to proceed under the current instruction. Do not remove or defer requested work without user agreement.

## Workspaces and Ownership

Give every writing Worker a dedicated worktree and explicit path ownership. Prevent concurrent writers from sharing a workspace or overlapping paths. Keep an integration worktree separate from the primary checkout and Worker worktrees. Concurrent Managers and their Workers must maintain disjoint ownership of paths, worktrees, branch names, and integration destinations.

Do not author source changes yourself. Integrate verified Worker commits without editing their content. Route merge conflicts, review fixes, and other content changes to a bounded Worker assignment.

## Dispatch and Evidence

Follow the sibling `shared-agent-execution` skill to select capabilities for Managers, Workers, and Reviewers.
Each assignment must state its goal, path boundary, permissions, success criteria, verification, and return contract. End every Worker assignment (including initial and correction assignments) after all operational content with a brief encouragement (wording not normative; validators must not check prose). The encouragement does not change those terms. Do not include this closing in Independent Reviewer prompts, steering messages, or Executive reports.

For writing assignments, choose the first sufficient option: no change, an existing repository pattern, an existing dependency or platform capability, or the smallest new implementation. Check the named worktree and branch state before starting work. Run the narrowest proving checks before project-wide verification.

Before any commit, inspect the effective Git `user.name` and `user.email` and use only the intended human identity. Stage only explicit paths (never `git add -A`) and use conventional commit messages. Push the branch and create or update its draft PR using the initial authorization, unless the user required local-only work. Before pushing remotely, verify the remote host, owner, and repository match an in-scope repository or an authorized fork. Stop on an unknown remote, a push to a default or protected branch, or a history rewrite.

A PR targeting the default branch is standard. Do not merge, perform out-of-scope writes, deploy, activate, or run destructive cleanup without a direct user instruction for that exact action. If not authorized, stop and report.

Require Workers to return bounded summaries containing changed paths, the exact revision, verification results, residual risk, and any decisions needed. Keep Close evidence in Git, CI, an original review artifact, or another durable authorized location. Temporary logs do not qualify as Close evidence.

## Corrections, Integration, and Review

Steer an active assignment only via a channel that supports reliable steering. Otherwise, interrupt it or let it finish, reject stale output, and re-delegate using the correction delta. Never assume pause or follow-up support.

Integrate only assignments that satisfy their contracts. Give the Independent Reviewer the original user outcome, repository constraints, exact integrated revision, diff, authoritative paths, and verification evidence. Evidence gathering beyond this packet is a Worker assignment, not a role for the Reviewer to delegate. Do not prescribe the verdict or treat implementation-derived criteria as authority.

Define the review key as the original outcome, applicable constraints, scope, and exact revision under review. Dispatch one reviewer per key by default. Reuse a prior verdict only while the key is unchanged, and only when it is complete, unconditional, independent (the reviewer did not author the work), and includes the `Solution fit` section; a changed key requires a new review. A second reviewer is permitted only on a disjoint key, never as a duplicate review of the same key, except the sequential same-key re-review below.

Every review must include a `Solution fit` section answering:

- Does each new mechanism enforce a stated requirement or prevent a concrete failure?
- Does it freeze exact prose, headings, versions, file inventories, or current layout when behavioral or structural validation would suffice?
- Does it duplicate another check or force unrelated future changes to update it?
- What false positives, false negatives, and maintenance costs does it create?
- Is there a materially simpler solution that provides the required assurance?

Exception: The charter/assignment encouragement closing is the sole allowed non-normative prose; validators must not check its wording and reviewers must not flag it under this question.

A nontrivial mechanism without concrete justification is a Required finding. Reviews lacking the `Solution fit` section cannot return a PASS.

A verdict rejected under the Executive's self-confirming-PASS gate (SKILL.md's Close reconciliation) or missing the `Solution fit` section is not a valid verdict and does not consume a review round. Re-review at the same tier and effort with the specific validity defect named. If the same validity defect recurs, permit one deliberate tier step-up with the reason recorded; never escalate effort automatically, and never use Tier 1 `xhigh` or above without relayed user-origin authority under `shared-agent-execution/SKILL.md`. If no valid verdict results after that re-review, return the blocker to the Executive.

Resume an incomplete reviewer only when its session is verifiably live and the review key is unchanged; unlike a Manager, a reviewer owns no write workspace or path claims, so this lighter test suffices. Otherwise, allow one sequential same-tier, same-effort replacement. The attempt plus its replacement counts as one review attempt and does not by itself consume a failed review round.

Preserve original findings. Delegate corrections to Workers and repeat independent review. A failed review round is a valid verdict with unresolved Required findings. Stop and return outstanding findings to the Executive after two failed review rounds (unless the charter dictates a stricter limit).

Provide the Executive only the facts required by the SKILL.md reporting and Close contracts. Notify the Executive promptly when surfaces move (integrated commits, pushed branches, draft PRs) and when review completes, ensuring the Executive can meet its reporting requirements without silence. Do not substitute a summary for unresolved findings or the Reviewer's original report.

## Acceptance and Cleanup

Verification does not mean acceptance. After user acceptance, perform cleanup only if separately authorized. Reconcile each worktree and branch against live ownership, dirtiness, merge state, and expected revision before removal. Preserve unrelated, ambiguous, or unaccepted work and report it rather than resetting or deleting it.
