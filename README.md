# Bossmode

Stop babysitting coding agents. Bossmode makes you the CEO of `Get-It-Done`.

A `bossmode` supervisor agent manages teams of native subagents and external agents such as Pi,
Claude, Codex, Grok, AGY, and Muse—so far—while they carry out bounded project tasks. You describe
the outcome in a prompt. The supervisor handles task records, delegation, recovery, evidence, and
independent evaluation.

Bossmode improves agent effectiveness over time: it captures durable session history, turns
repeated and verified workflows into gated proposals for reusable skills, measures agent and model
efficiency, and enforces critical rules in deterministic code.

> `herdr` is the recommended external-agent runner for `bossmode` because it lets you monitor your
> agent team's activity and resume workers safely.

> Bossmode is a control plane, not a background agent or provider router. Your supervisor agent
> performs the work and uses the Bossmode CLI for durable bookkeeping.

## Install and start with a prompt

Install the v0.1.0 wheel as an isolated command-line tool, then install its version-matched
Bossmode skill in your project:

```bash
uv tool install ./bossmode-0.1.0-py3-none-any.whl
cd /path/to/your-project
bossmode install-skill
```

`bossmode install-skill` creates `.agents/skills/bossmode/SKILL.md`. Running it again is safe when
the file is identical. It refuses to overwrite a different file or install through a symlinked skill
directory. It does not create the registry; the first command that needs `.bossmode/control.db`
creates it.

Then ask an agent that discovers project skills:

```text
Use the Bossmode skill for this project. Turn my request into one bounded task with explicit success criteria and permission limits, record it before delegation, and require independent evidence before marking it complete: [describe the outcome you want]
```

For example:

```text
Use Bossmode to implement rate limiting for the auth endpoints.

Success means the implementation uses a token bucket, sustains 100 requests per minute,
allows bursts of 20 requests, and passes focused tests. Limit edits to the auth middleware
and its tests. Require an independent reviewer before calling the task complete. Ask before
expanding permissions or approving any trust dialog.
```

The supervisor should return the material result, artifact paths, verification evidence, and exact
task, run, and evaluation IDs. You should not need to translate the request into registry commands.

## Common prompts

### Resume safely

```text
Use Bossmode to resume this project. Reconcile the registry with live native-runtime and
Herdr state before continuing. Report waiting-user work, protected approvals, genuine blockers,
active work, and the next safe action separately. Do not prompt, replace, or close a worker from
a stored ID alone.
```

### Run a read-only investigation

```text
Use Bossmode to investigate why the API tests became slower. Keep the task read-only and do not
edit files or external state. Have a separate reviewer check the evidence. Return the findings and
the task, run, and evaluation IDs.
```

### Choose an external agent through Herdr

```text
Use Bossmode and Herdr to delegate this compatibility review to Claude. Reserve the run before
creating the worker, bind only a uniquely verified live identity, and correlate every prompt with
its exact turn result. Do not approve trust or permission dialogs.
```

### Record a correction without applying it

```text
Record this correction for task TASK_ID using recurrence key api.rate-limit.algorithm:
use token bucket rather than fixed-window rate limiting. Show any resulting promotion proposal
and its evidence. Do not accept the proposal or edit memories, skills, AGENTS.md, or controls.
```

More recipes, including retry, promotion, maintenance, and scheduling prompts, are in the
[prompt guide](docs/prompt-guide.md).

## What happens after your prompt

```text
Your prompt
  -> supervisor records goal, success criteria, and permissions
  -> Bossmode selects one ready task
  -> supervisor delegates to a native subagent or a Herdr worker
  -> worker returns declared artifacts and evidence
  -> independent reviewer checks the result
  -> supervisor reports completion, a blocker, or a decision for you
```

A worker's successful run moves the task to evaluation. Only independent evidence can move it to
final success. Bossmode also keeps these decisions separate:

- A request to expand permissions or approve a trust dialog always returns to you.
- Missing, foreign, or ambiguous live worker identity is a blocker, not a reason to guess.
- `waiting_user`, protected approvals, and genuine blockers are reported separately.
- Feedback may create a promotion proposal, but no proposal edits memory, skills, instructions, or
  controls without your approval.
- Accepting a promotion authorizes implementation of the proposed artifact. The supervisor must
  make that artifact reviewable and test it; `promotion apply` only records that verified work was
  applied.
- Installing or removing a scheduler changes host state and requires an explicit request.

## Agent integration status

Bossmode has no provider-specific transport adapters. Its registry and CLI record generic agent
roles and Herdr bindings. The deterministic test suite exercises those control records for AGY,
Codex, Claude, Pi, Grok, and Muse, but it does not start those agents. The documented Herdr path
uses the official Herdr CLI for all six named kinds; the current automated UAT has not certified any
of them with a live Herdr canary.

Use a native subagent only when the host runtime supports that agent. Ask for Herdr when you need an
external interactive agent, a visible pane, or durable detach and reattach behavior, and verify the
chosen kind against live Herdr state before relying on it. Bossmode does not choose a provider from
historical scores in this MVP. Choose explicitly by the task's required capabilities, permissions,
interaction mode, and current availability. Stored model and reasoning metadata is telemetry, not
proof of the provider, model family, or model that ran; different labels do not replace independent
evaluation.

## Documentation

- [Prompt guide](docs/prompt-guide.md): the primary user interface and copyable recipes.
- [Example walkthrough](docs/example-walkthrough.md): an end-to-end conversation with failure,
  retry, evaluation, and learning.
- [Supervisor protocol](docs/agent-workflow.md): canonical agent procedure and recovery rules.
- [CLI reference](docs/cli-reference.md): exact commands, options, output, and exit codes.
- [Herdr runtime boundary](docs/adr/0001-herdr-runtime-boundary.md): why live Herdr state remains
  authoritative.
- [Agent organization design](docs/adr/0002-agent-organization-design.md): deferred scaling choices.
- [Task intake and external sources](docs/adr/0003-task-intake-and-external-sources.md): how work
  reaches the registry.
- [Deferred executor selection](docs/adr/0005-defer-executor-selection.md): why executor choice is
  explicit and capability-based rather than automatic.
- [Registry worktree ownership](docs/adr/0006-registry-worktree-ownership.md): why each checkout
  owns its own registry.

## Development and verification

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest
uv run python scripts/run_uat.py
```

`uv run pytest` enforces at least 90% branch coverage and a 30-second test timeout. CI repeats the
suite on supported Python versions and platforms, then builds and executes the wheel in isolation.
The in-process UAT checks registry and CLI behavior; it does not create live Herdr workers, approve
trust dialogs, or install host scheduler entries.
