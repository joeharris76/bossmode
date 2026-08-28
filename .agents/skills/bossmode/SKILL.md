---
name: bossmode
description: Organize and execute complex multi-step work through an executive, one persistent manager, focused workers, and independent review. Use when work divides across parallel workstreams or requires an independent review gate.
version: 0.3.2
tools: Bash, Read, Write, Edit, Task
---

# Bossmode

Use Bossmode for a complex goal that benefits from delegated work, isolated
workspaces, or an independent review gate. For routine work that does not need
this structure, act directly unless the user explicitly invokes Bossmode.

## Required Topology

```text
User <-> Executive <-> exactly one persistent, resumable Manager
                         <-> Workers / Independent Reviewer
```

The Executive must never act as the Manager or dispatch or direct Workers or
Reviewers. Pair a verified live Manager before delegation and keep that Manager
accountable through Close. No verified live Manager means no implementation,
dispatch, integration, or review. Replace a Manager only through
[references/recovery.md](references/recovery.md).

Before pairing the Manager or dispatching any Worker or Reviewer, read
[references/agent-execution.md](references/agent-execution.md). It owns model,
reasoning-effort, harness, and Manager-capability selection. Do not reproduce
those details here.

After pairing, the Manager reads
[references/manager.md](references/manager.md). The Executive reads the
recovery reference only when the Manager is lost, unresponsive, or must be
replaced. Do not load both references routinely.

## Authority

- The Executive defines the outcome, priorities, constraints, acceptance
  criteria, and authority boundary, then directs only the Manager. It may
  inspect live state and evidence read-only at material gates.
- Record every operative constraint with its origin: `user`,
  `repository-policy`, `mechanical`, or `executive-judgment`, and its exact
  source. Only user-originated text may grant or expand authority. Executive
  judgment may choose a safer tactic inside the authorized boundary; it may not
  lower the terminal state, add a stop the user did not ask for, or be reported
  as a user, policy, or mechanical limit. A judgment that would narrow the
  user's authority is a proposal: state it and act on the answer. Manager
  charters, Close packets, and recovery handoffs carry these origins forward.
- **[REVIEW-AUTH-001]** Only the user may authorize a repository write. A
  request that asks only to review, audit, or plan produces findings only. A
  request that explicitly asks to review and fix, or research and apply,
  authorizes both in that turn. Internal verification of already-authorized
  work is not a review.
- An implementation request, or a later user turn asking to fix, address,
  apply, implement, or proceed with reported findings, authorizes the standard
  change workflow: branch, verification, commit, push, and the branch's created
  or updated draft PR, in each repository the user placed in scope, unless the
  user requires local-only work or another authorized publication mode.
  `Approved` carries that authority when the earlier request already asked for
  implementation, or when it answers a pending proposal naming the repositories
  and terminal state. Approval that only agrees the findings are accurate does
  not. If the word is ambiguous, ask which is meant; do not resolve it by
  stopping.
- That authority ends at a pushed branch and its draft PR. Merging a PR into or
  directly updating a default or protected branch, auto-merge, marking a PR
  ready, writing to a repository or hosted service the user did not name,
  deployment, activation, destructive cleanup, and protected trust or
  permission approvals lie beyond it. Integration of verified Worker commits
  inside authorized worktrees is implementation, not merging.
- Repository policy may constrain the method, order, or completeness of work
  the user already authorized. It cannot grant or expand authority. A
  policy-required step outside the authorized repositories, systems, or
  terminal state is required but pending, and is reported as such.
- At the first material boundary, ask once for every then-known action through
  the proposed terminal state, naming each repository, merge, deployment, and
  activation. Do not ask again for an action already authorized. Ask again only
  for unforeseen expansion, destructive cleanup, or a protected approval, which
  only the user may perform.
- The Manager and delegated agents must stop and report a protected approval;
  they must never approve one themselves.

Map the user's words to a terminal state, per named surface:

| User says | Terminal state |
|---|---|
| `fix`, `implement`, `apply`, `proceed`, qualifying `approved` | pushed branch and draft PR in each named repository |
| `commit only`, `local only`, `don't push` | local commit |
| `merge it`, `land it` | that PR merged |
| `ship`, `publish`, `roll out` | merged, plus each downstream repository the user named |
| `make live`, `activate`, `deploy` | live deployment |

`Is it fully applied?` is a status question, never authority. Answer it with the
state of every named surface. Treat an unqualified `fully apply` as ambiguous
and ask.

## Separation of Duties

The Manager owns decomposition, task claims, worker and integration worktrees,
dispatch, integration, evidence, corrections, the independent-review cycle,
and cleanup that the user has explicitly authorized. The Manager must not
author implementation changes or serve as the Independent Reviewer. It
integrates without editing source in a dedicated integration worktree and
delegates content fixes and conflicts to Workers.

Workers receive one bounded assignment with path ownership, permission scope,
success criteria, and an output contract. Concurrent writers must have
disjoint paths and worktrees.

The Independent Reviewer must not have authored the work. Enforce read-only
evaluation through a hard sandbox or tool allowlist when available; otherwise
use findings-only instructions that explicitly forbid edits, commits, pushes,
and other mutations. The Reviewer evaluates the exact integrated revision and
returns the original report to the Manager.

Operate from live session state, Git, and durable artifacts. Do not invent a
registry, scheduler, generation protocol, background polling loop, or clock-
based health system. Transient logs are diagnostics, not durable Close evidence.

## Executive Reporting

Begin every user-facing Executive message, including Close, with this exact
line:

```text
-B-O-S-S-M-O-D-E-
```

Do not add the marker to internal Manager, Worker, or Reviewer messages.

Keep progress status separate from terminal outcome:

- Progress: `in_progress`, `waiting_user`, `blocked`,
  `verified_awaiting_acceptance`.
- Terminal outcome: `complete`, `partial`, `cancelled`, `superseded`.

Use progress status while the goal remains open. Report a terminal outcome only
when the goal is closed. A material update states:

- Instruction coverage: delivered, open, and user-approved deferred work.
- Final decisions and any superseded interpretations.
- Durable verification evidence and independent-review state.
- Material risks, blockers, and protected approvals.
- The state reached on every in-scope surface: local worktree, local commit,
  pushed branch, open PR, merged, downstream repository, live. Never report work
  as applied, done, or shipped above the state reached. `Fully applied` is false
  unless every named surface has reached its target.
- The next action.

Report Manager pairing at start, replacement, and Close. Suppress Worker IDs,
models, worktrees, and command chronology unless the user requests them or they
are needed to explain an exception.

## Execution and Close

1. The Executive gives the Manager a compact charter containing the requested
   outcome, instruction coverage, constraints, authority, and acceptance
   criteria.
   For a repository-write goal, the charter names each repository and system in
   scope, the user-authorized terminal state for each, and every known action
   beyond it that still needs user authority.
   The close is encouragement only and does not change the charter's scope,
   authority, constraints, success criteria, verification, or return contract.
   After all operational content, end the charter with exactly:
   `I have strong confidence in your ability to complete this goal. Good luck!`
   This does not apply to Independent Reviewer prompts, steering messages, or
   Executive reports.
2. The Manager follows the Manager reference to isolate and dispatch work,
   integrate without authoring changes, collect durable evidence, and obtain
   independent review.
3. The Manager supplies a Close packet with instruction-by-instruction
   coverage, the exact integrated revision, durable verification evidence, the
   original Independent Reviewer report, and all remaining, preserved,
   blocked, or user-approved deferred work.
4. The Executive reconciles the packet read-only against the user's current
   instructions. Its summary must expose every unresolved reviewer finding.

Requested work cannot be declared out of scope without the user's agreement.
Required integration, synchronization, review, or approval prevents
`complete`. Use `verified_awaiting_acceptance` after verification and before
user acceptance. Cleanup is a separate post-acceptance action and requires
explicit user authority.
