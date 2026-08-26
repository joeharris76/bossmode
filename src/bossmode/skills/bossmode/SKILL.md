---
name: bossmode
description: Manage, dispatch, and continue tasks using the Bossmode durable control plane. Reconcile runtime state, orchestrate native subagents (AGY, Codex) or external Herdr workers (pi, codex, claude, agy, grok, muse), enforce independent evaluation gates, and propose gated promotions.
---

# Bossmode

Operate from the repository's primary checkout. Its `.bossmode/control.db` is the one operational
authority for every linked worktree.

## Registry Ownership and Schema Compatibility

1. Before first use, run `bossmode registry create` from the primary checkout. This is the only
   operational creation and upgrade spelling.
2. Before reconciling, verify `pwd`, `git rev-parse --show-toplevel`, the Git common directory, and
   that the checkout is primary. Every other operational command is open-only.
3. Never pass `--db` or set `BOSSMODE_DB` to a linked-worktree, copied, symlinked, nonstandard,
   wrong-repository, or ephemeral database. Identity validation rejects it before filesystem or
   SQLite mutation.
4. The registry's immutable ID, operational role, repository URL, Git common directory, and
   primary checkout are authority. Repository relocation or origin changes are blockers; do not
   copy, rebind, import, or replace the database.
5. Explicit non-repository databases are ephemeral certification state. They cannot be adopted or
   merged into operational history.
6. Do not assume commands or schema features from an unmerged worktree are available in the
   primary checkout.

## Command Surface

```text
bossmode                        # Default: converge the registry and report state
├── install-skill               # Install this version's skill into a project
├── registry
│   └── create                  # Create or upgrade the primary operational authority
├── reconcile                   # The default command, named explicitly
├── task
│   ├── create                  # Create a new task with success criteria
│   ├── list                    # List tasks by state
│   ├── show <id>               # Show task details and event history
│   └── transition <id> <state> # Move a task to ready, blocked, or archived
├── run
│   ├── start <task_id>         # Start an execution run for a worker
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
3. Record the task with `bossmode task create` and preserve the returned task ID.
4. Run `bossmode` to confirm that the recorded task is the next eligible dispatch. Do not delegate
   unrecorded work.

## 2. Reconcile

1. From the primary checkout, run `uv run bossmode` (naked execution) or
   `uv run bossmode reconcile` and read the JSON output. This command validates the existing
   authority, then may write promotion proposals; it never creates or upgrades the registry.
2. Use the nested run, binding, and turn records in `running` and `evaluating` to recover after
   interruption. Use `bossmode run show RUN_ID` or `bossmode turn show TURN_ID` for exact records.
3. For every active registry task, reconcile its executor against live state before sending,
   interrupting, completing, or closing it. Use live runtime state for native subagents (AGY, Codex)
   and `herdr agent get/list` for Herdr workers (`pi`, `codex`, `claude`, `agy`, `grok`, `muse`).
   Treat missing, foreign, or ambiguous identity as a blocker; stored IDs are indexes, not capabilities.
4. Surface `waiting_user` and `blocked` items before starting new work when they affect priority
   or safety. Ask only for the decision the system cannot safely make.

## 3. Dispatch

1. Select only the single task returned in `next_task`.
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
   The validated registry's primary checkout owns the job. Omit `--repo-dir` or require it to match
   that owner exactly.

## 8. Report

Return only material state changes: dispatched work, completed and evaluated results, user decisions,
new blockers, and promotion proposals. Include exact task, run, turn, evaluation, and promotion IDs.
