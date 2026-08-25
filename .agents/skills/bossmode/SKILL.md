---
name: bossmode
description: Manage, dispatch, and continue tasks using the Bossmode durable control plane. Reconcile runtime state, orchestrate native subagents (AGY, Codex) or external Herdr workers (pi, codex, claude, agy, grok, muse), enforce independent evaluation gates, and propose gated promotions.
---

# Bossmode

Operate from the repository root. Each checkout or linked worktree owns its own
`.bossmode/control.db`. The registry is not a shared worktree file.

## Registry Ownership and Schema Compatibility

1. Before reconciling, verify `pwd`, `git rev-parse --show-toplevel`, and the resolved database
   path. The default database is the current checkout's `.bossmode/control.db`.
2. Never pass `--db` or set `BOSSMODE_DB` to another worktree's `.bossmode/control.db`. The CLI
   rejects that path before opening SQLite, inspecting the schema, or running migrations.
3. `Registry.initialize()` may migrate only the registry owned by the current checkout. If the
   database reports a newer schema than this checkout supports, stop and return the mismatch as a
   blocker; do not downgrade, copy over, or override it with a sibling worktree path.
4. A genuinely shared control plane requires a dedicated supervisor-owned registry location and an
   explicit adoption protocol. No implicit shared-registry workflow is available in this MVP.
5. Do not assume commands or schema features from an unmerged worktree are available in this
   checkout. Run the skill and CLI from the checkout whose source and registry you are reconciling.

## Command Surface

```text
bossmode                        # Default: converge the registry and report state
├── install-skill               # Install this version's skill into a project
├── reconcile                   # The default command, named explicitly
├── task
│   ├── create                  # Create a new task with success criteria
│   ├── list                    # List tasks by state
│   ├── show <id>               # Show task details and event history
│   └── transition <id> <state> # Move a task to ready, blocked, or archived
├── team
│   ├── create/bind-tab/list/show # Manage teams and verify one named tab
├── run
│   ├── start <task_id>         # Start a singleton execution run
│   ├── start-manager <team_id> # Start a durable manager run
│   ├── start-worker <task_id>  # Start a fenced writer run
│   ├── start-reviewer <task_id> # Start an independent reviewer run
│   ├── record-head <run_id>    # Record a missing accepted head for a worker
│   ├── finish <run_id>         # Complete a run (task -> evaluating, waiting_user,
│   │                           #   blocked, or failed)
│   └── show <run_id>           # Show run details and turn records
├── herdr
│   ├── bind <run_id>           # Link a live Herdr worker pane to a run
│   └── show <run_id>           # Show Herdr binding and native session info
├── turn
│   ├── start <run_id>          # Open a prompt turn and allocate result path
│   ├── finish <turn_id>        # Validate turn JSON artifact and record its outcome
│   └── show <turn_id>          # Show prompt text, digest, and validated result
├── evaluate <task_id>          # Record independent evaluation (reviewer != worker)
├── dispatch <task_id>          # Atomically dispatch multiple bounded workers
├── resource
│   ├── reclaim                 # Mark expired leases and report reclaimable claims
│   └── release <claim_id>      # Release an expired claim with owner/fence evidence
├── report <task_id>            # Redacted executive aggregation
├── signal <task_id> <category> # Record a decision, blocker, or approval
├── feedback <task_id>          # Record user or system feedback with recurrence key
├── promotion
│   ├── propose                 # Scan feedback and generate candidate proposals
│   ├── list                    # List promotion proposals by status
│   ├── accept <id>             # Accept a proposal for implementation
│   ├── reject <id>             # Reject a proposal
│   └── apply <id>              # Mark an accepted proposal as verified and applied
├── maintenance                 # Run telemetry analytics, health checks, & promotion scan
└── schedule
    ├── install                 # Register OS scheduler job (launchd on macOS, crontab on Linux)
    ├── status                  # Inspect registration and available log activity
    └── uninstall               # Cleanly remove OS scheduler job
```

`bossmode` and `bossmode reconcile` are the only two spellings of the default
command, and there are no aliases anywhere in this surface.

## 1. Intake a Prompt

When the user asks Bossmode to handle work, translate the request into one bounded task before
delegating it. Preserve the user's outcome and limits; do not add adjacent work.

1. Derive a short title, one outcome-oriented goal, observable success criteria, and the permission
   scope needed for that outcome.
2. Ask only when a missing choice would materially change the result or permissions. Otherwise,
   make reasonable bounded assumptions and state them.
3. Record the task with `bossmode task create` and preserve the returned task ID. For team Git work,
   provide the supervisor-approved base explicitly with `--approved-base-sha`; do not rely only on
   scope JSON.
4. Run `bossmode` to confirm that the recorded task is the next eligible dispatch. Do not delegate
   unrecorded work.

## 2. Reconcile

1. Run `uv run bossmode` (naked execution) or `uv run bossmode reconcile` and read the JSON
   output. This command writes: it creates or migrates the registry and materialises promotion
   proposals before reporting.
2. Use the nested run, binding, and turn records in `running` and `evaluating` to recover after
   interruption. Use `bossmode run show RUN_ID` or `bossmode turn show TURN_ID` for exact records.
3. For every active registry task, reconcile its executor against live state before sending,
   interrupting, completing, or closing it. Use live runtime state for native subagents (AGY, Codex)
   and `herdr agent get/list` for Herdr workers (`pi`, `codex`, `claude`, `agy`, `grok`, `muse`).
   Treat missing, foreign, or ambiguous identity as a blocker; stored IDs are indexes, not capabilities.
4. Surface `waiting_user` and `blocked` items before starting new work when they affect priority
   or safety. Ask only for the decision the system cannot safely make.

## 3. Dispatch

1. For singleton work, select only the task returned in `next_task`; for team
   work, use the atomic `dispatch` path for disjoint child tasks.
2. Choose the narrowest role: `researcher` for read-heavy evidence, `worker` for authorized edits,
   or `reviewer` for independent evaluation.
3. Give the agent the task ID, goal, success criteria, permission limits, relevant evidence, and
   required structured return format.
4. For a native subagent (e.g. AGY, Codex), spawn it and record the returned thread or task ID with
   `bossmode run start TASK_ID --role ROLE --thread-id NATIVE_ID`.
5. For a Herdr worker, reserve the run with `bossmode run start` before creating any layout.
   Derive a unique worker name from the run ID, create it through the official Herdr CLI, then call
   `bossmode herdr bind --agent-kind KIND`. If binding fails after creation, reconcile the deterministic name;
   do not create another worker or close by a stored pane ID.
6. Use Herdr only when the task explicitly requests an external interactive agent. Do not add an
   agent router or choose providers from historical scores in this spike.
7. Do not run parallel writers against the same paths.

### Parallel manager teams

Use `dispatch` only for child tasks with disjoint declared scopes. A
manager run must be created and identified before a worker is created. A
worker writer reservation includes a dedicated branch, base SHA, worktree path,
and worktree ID. Resource claims cover both files and named external resources,
carry fence tokens, and are atomic with worker creation. Expired leases become
`expired` and are never automatically stolen.

Each team has exactly one unique durable `expected_tab_label` and one reconciled live
Herdr `(session, workspace, tab)` binding, with no fallback or second team tab.
Validate the task/team hierarchy, including the complete ancestor chain, before
persistence. Create the one named tab first, reconcile it with `bossmode team
bind-tab`, and reject every manager, worker, or reviewer binding whose
observed workspace or tab differs. Singleton runs with no `team_id` remain
bounded-compatible with the legacy binding path.

Before creating a worker through `dispatch` or `run start-worker`, admit
the live Git writer against the registry's repository. Require a dedicated non-protected branch, a clean
linked
worktree that is not the primary checkout, unique branch/path/worktree identity,
and a base SHA that resolves to an existing commit in that repository. Reject
protected branches (`main`, `master`, `develop`, and repository-configured
protected branches), dirty or primary worktrees, duplicate worktrees,
unrelated repositories, and invalid base SHAs before persisting the worker. The
supervisor supplies the explicit `--approved-repository-path` and
`--approved-base-sha` inputs; a scope field or writer declaration alone is not
the approval. A caller-supplied repository path cannot redirect admission. The writer
reservation records
branch, base SHA, worktree path, and worktree ID; it is not a substitute for
live evidence.

For every team agent invocation, split a pane from an explicit anchor already
inside the reconciled team tab before starting the agent. Keep the manager or
control pane at the top, and stack every worker/reviewer pane vertically below
it with horizontal dividers:

```bash
herdr pane split TEAM_ANCHOR_PANE_ID --direction down --cwd "$PWD" --no-focus
herdr agent start WORKER_NAME --kind claude --pane NEW_PANE_ID
```

The `--current` focused-pane option and rightward splits are mechanically forbidden. Never
use the focused or an unrelated tab as the parent. If the tab is missing or
ambiguous, stop and reconcile it; do not move or close existing panes.

Reviewer runs have their own durable identity and link to the worker run they
evaluate. Team workers require a finished successful reviewer run and its
reviewer run ID passed to `evaluate`; a failed or unfinished reviewer is not
evidence. A reviewer string alone is supported only for bounded legacy
singleton compatibility. The reviewer must check the worker's exact Git head
SHA, and team evaluation must pass that same SHA through
`evaluate --reviewed-head-sha`; the registry rejects a missing or mismatched
head.
Admission atomically allows only one active reviewer for a worker, so a
reviewer started after an earlier reviewer settles is a legitimate sequential
retry. Any reviewer admitted while the task is `evaluating` may settle after
the task succeeds only when the same parent worker has a passing evaluation
whose reviewed head equals that worker's accepted head; settlement preserves
the succeeded task state and existing evaluation. Reviewer admission after
success remains impossible. Failed, mismatched, or missing evaluation
evidence rejects post-success settlement.

Finalize a team deterministically: settle turns, finish current worker
attempts, release all claims, finish linked reviewers, and record
a passing exact-head evaluation for every child before finishing the manager.
For each non-archived child task in `succeeded`, select its latest worker
attempt as the accepted attempt; historical failed or rejected attempts from a
retry do not count. A manager cannot finish while child workers or reviewers
are active, while claims remain held, or while a succeeded child lacks a
passing exact-head evaluation for its selected attempt. Acceptance requires at
least three selected accepted workers overlapping under two managers.

For a legacy finished successful team worker whose schema-upgraded writer row has NULL
`repository_path` and `accepted_head_sha`, use
`bossmode run record-head RUN_ID --repository-path PATH --accepted-head-sha SHA --evidence TEXT`.
The supervisor must supply the live Git root/common repository. The registry revalidates that
repository, the recorded linked worktree, branch, clean current head, worker identity, and exact
commit existence before an atomic conditional one-time assignment of both fields. A pre-existing
non-NULL repository path must match the supplied path and is never overwritten; an accepted head
cannot be overwritten. Evidence is required and recorded in task history; active, wrong-identity,
unrelated or non-root repositories, dirty/moved/ambiguous worktrees, invalid commits, and races
fail closed.

Claims are owned by a run and fenced by a unique token. Lease expiry changes
`active` to `expired`; live owner evidence is required before the
owner and matching fence token can explicitly release the claim. Bossmode
never auto-steals a claim. The executive report contains only outcomes, decisions,
blockers, approvals, and team progress; it excludes prompts, transcripts, turn
artifacts, and low-level worker activity.

Use `report` for leadership reporting. It mechanically includes task
outcomes, decisions, blockers, approvals, and per-team progress while excluding
prompts, transcripts, turn artifacts, and low-level worker activity.

## 4. Correlate Herdr Turns

Before the first external run, follow this command sequence and result contract.

1. Use the official, release-matched Herdr commands; never invoke legacy wrappers.
2. Require one unambiguous live worker matching the binding and wait until it is `idle` or `done`.
   `agent prompt --wait` is lifecycle-based and cannot identify a turn that began while the worker
   was already busy.
3. Call `bossmode turn start RUN_ID --purpose PURPOSE --prompt PROMPT`, put its `turn_id` and
   `artifact_path` in the worker prompt envelope, then prompt through Herdr. Only one turn may
   remain open per run.
4. Run `herdr agent prompt NAME ENVELOPE --wait` and inspect `blocked`, stalled, unknown, and error
   states separately. Never approve a trust or permission dialog without explicit user authority.
5. Call `bossmode turn finish TURN_ID --outcome succeeded` only after the worker settles. It reads
   and validates the exact result path; on rejection, preserve `failed` or `unknown` with explicit
   evidence. Never infer success or a path from terminal text.
6. Re-run `herdr agent get` after the first session-bearing event and bind the structured native
   session reference `{source, agent, kind, value}`. Refuse a different native reference for the
   same run.

## 5. Complete and Evaluate

1. When the agent finishes, record the run outcome, summary, artifact manifest, model/runtime
   information, retries, and timing with `bossmode run finish`.
2. A successful run is not a passing evaluation; it moves the task to `evaluating`.
3. Require an independent evaluation (`reviewer` role or user); self-evaluation is rejected.
4. Record the evaluation with `bossmode evaluate TASK_ID --run-id RUN_ID --evaluator REVIEWER --passed/--failed --evidence EVIDENCE`;
   only a passing evaluation moves the task to `succeeded`.
5. Record explicit user corrections or preferences with
   `bossmode feedback TASK_ID --category CATEGORY --key KEY --content CONTENT`.
   Choose a stable, narrowly scoped recurrence key.

## 6. Gated Promotion

1. Run `uv run bossmode` after feedback or evaluation to compute promotion proposals.
2. Present promotion proposals with their evidence and intended layer:
   - `control`: deterministic code/rules/hooks for repeated failures (2+);
   - `skill`: reusable workflow for repeated corrections with passing evals (2+);
   - `memory`: contextual preference or observation for future retrieval.
3. Do not accept, apply, or implement a promotion without explicit user authorization.
4. Advance promotions through explicit approval gates:
   - `bossmode promotion accept PROMOTION_ID`
   - `bossmode promotion apply PROMOTION_ID`
   - `bossmode promotion reject PROMOTION_ID`

## 7. Maintenance and Scheduling

1. Run `uv run bossmode maintenance` to audit database health, orphaned turns, and token telemetry across models and reasoning effort tiers.
2. Configure native OS background scheduling when requested:
   - `bossmode schedule install --interval 3600` (registers LaunchAgent on macOS, crontab on Linux).
   - `bossmode schedule status` to inspect registration and available log activity.
   - `bossmode schedule uninstall` to cleanly remove the OS background job.

## 8. Report

Return only material state changes: dispatched work, completed and evaluated results, user decisions,
new blockers, and promotion proposals. Include exact task, run, turn, evaluation, and promotion IDs.
