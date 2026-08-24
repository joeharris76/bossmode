# Prompt guide

Bossmode users normally describe work to a supervisor agent in plain language. The supervisor uses
the Bossmode registry and the available runtime tools behind the scenes. The CLI is an agent and
operator interface, not a prerequisite vocabulary for users.

## A useful Bossmode prompt

Include the parts that affect the result or its safety:

1. **Goal:** the outcome you want.
2. **Success criteria:** evidence that would prove completion.
3. **Permission scope:** files, systems, and actions the task may change.
4. **Worker preference:** optional; name a native or Herdr agent only when it matters.
5. **Approval boundary:** decisions the supervisor must return to you.

You can omit task IDs, run IDs, state transitions, result paths, and CLI commands. The supervisor
creates and preserves those records.

## Start and verify work

```text
Use Bossmode to implement <goal>.

Record the task before delegation. Success means <criteria>. Limit permissions to <scope>.
Require an independent reviewer before calling it complete. Report the task, run, and evaluation
IDs with the artifacts and evidence. Ask before expanding permissions or approving a trust dialog.
```

Expected result: the supervisor either reports a reviewed result or separates the precise blocker
or user decision from other status.

## Investigate without changing anything

```text
Use Bossmode to investigate <question>. Create a read-only researcher task with success criteria
<criteria>. Do not edit files or external state. Have a separate reviewer check the evidence, then
return the findings and exact task, run, and evaluation IDs.
```

Use this form for audits, architecture research, incident analysis, and any request where findings
must come before remediation. A later prompt can authorize a specific change.

## Resume after an interruption

```text
Use Bossmode to resume this project. Reconcile .bossmode/control.db with live native-runtime and
Herdr state before continuing. Report waiting-user work, protected approvals, genuine blockers,
active work, and the next safe action separately. Do not prompt, replace, or close a worker from a
stored ID alone.
```

The registry is an index, not proof that a worker is still live or still owned by this task. A safe
resume checks the current runtime identity before any message, interruption, continuation, or close.

## Use a particular external agent

```text
Use Bossmode and Herdr to delegate <task> to <agent kind>. Reserve the Bossmode run before creating
the worker, bind only a uniquely verified live identity, and correlate every prompt with the exact
turn result artifact. Do not approve trust or permission dialogs. Stop and report blocked if the
identity is missing, foreign, or ambiguous.
```

Ask for Herdr when you want an external interactive worker, visible terminal activity, or later
reattachment. Native subagents are simpler when the host already supports the chosen worker.

## Retry after a failed evaluation

```text
Retry task <task_id> using the failed evaluation evidence. Preserve the original run and feedback
history. Narrow the correction to <required change>, re-run the relevant checks, and require a new
independent evaluation. Do not broaden the file or permission scope without asking.
```

A failed evaluation is evidence, not permission for unrelated remediation. The supervisor records
the correction, starts a new run, and retains the earlier result in the history.

## Record learning without changing policy

```text
Record this <preference or correction> for task <task_id> using recurrence key <stable.key>:
<content>. Show any resulting promotion proposal with its evidence and target layer. Do not accept
it or edit memory, skills, AGENTS.md, or controls.
```

Feedback becomes a proposal only when the recurrence and evidence rules support one. A proposal is
never permission to modify a durable artifact.

## Approve a promotion

```text
Accept promotion <promotion_id>. Implement only the proposed artifact, make it reviewable, test it
in proportion to risk, and obtain independent evidence. Mark the promotion applied only after those
checks pass. Stop if the implementation scope differs from the accepted proposal.
```

Acceptance, implementation, verification, and recording `applied` are separate steps. The CLI does
not create a memory, skill, instruction, or control by changing the proposal status.

## Run maintenance without changing the host

```text
Run Bossmode maintenance and report database health, orphaned turns, stale bindings, telemetry, and
promotion proposals. Do not install or remove a scheduler job.
```

## Authorize background scheduling

```text
Install hourly Bossmode maintenance for this repository. First show the exact scheduler target,
repository path, and log path. After installation, verify its registration and report how to remove
it. Do not change any other scheduler entry.
```

This prompt explicitly authorizes a host-state change. A general request to maintain a project does
not authorize scheduler installation or removal.

## What the supervisor should report

For completed work, expect:

- the task, run, and evaluation IDs;
- material artifact paths;
- the checks or external evidence used by the independent reviewer;
- any untested or externally gated risk.

For incomplete work, expect separate sections or statements for:

- `waiting_user` decisions;
- protected approvals, including trust and permission dialogs;
- genuine technical or ownership blockers;
- active work and the next safe action.

Use the [example walkthrough](example-walkthrough.md) to see this conversation pattern end to end.
Agents and operators can find the exact mechanics in the [supervisor protocol](agent-workflow.md)
and [CLI reference](cli-reference.md).
