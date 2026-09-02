---
name: agent-execution
description: Select model tiers, map reasoning effort, and dispatch delegated work through native or external agent harnesses. Use when a workflow must choose an agent model or effort level, or launch a manager, worker, or independent reviewer. Do not use for direct, undelegated tool calls.
---

# Agent Execution

Use this skill to select models, set reasoning effort, or configure agent harnesses when delegating work. The calling skill still owns task decomposition, authorization, workspace isolation, and acceptance criteria.

## Model Tiers

Tiers describe roles, not just quality. Match the model to the task's complexity and risk.

**Selection Rule**: Choose the tier below. For native dispatch, use the model name directly. For external harnesses, use the specific identifier from [references/external-harnesses.md](references/external-harnesses.md).
Default reasoning effort to `medium`. Use `max` effort only for Tier 1 adversarial review. Use `low` for repetitive bulk work.

- **Tier 1: Strategic**
  - Models: `gpt-5.6-sol`, `claude-fable-5`
  - Usage: Strategic planning, architecture, high-risk tradeoffs, and final adversarial review.
- **Tier 2: Generalist**
  - Models: `gpt-5.6-terra`, `claude-opus-5`, `grok-4.6`, `gemini-3.7-flash-high`, `muse-spark-1.2`, `muse-spark-1.2-contributor`
  - Usage: Management, decomposition, integration, investigation, and routine review.
- **Tier 3: Contributor**
  - Models: `gpt-5.6-luna`, `claude-sonnet-5`, `grok-4.5`, `gemini-3.7-flash-medium`, `gemini-3.7-flash-low` (low-cost bulk)
  - Usage: Focused implementation, bounded research, bulk work, and parallel coverage.

> Note: pi/jcode prefixed forms (openai-codex/, muse-spark/) map to same tier as unprefixed base.

## Reasoning Effort

| Harness | Flag | Supported Values (Lowest to Highest) |
| :--- | :--- | :--- |
| **pi** | `--thinking <level>` | `off`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max` |
| **claude** | `--effort <level>` | `low`, `medium`, `high`, `xhigh`, `max` |
| **muse** | `--reasoning-effort <EFFORT>` | `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `ultra` |
| **agy** | `--effort <level>` | `low`, `medium`, `high` |
| **grok** | `--reasoning-effort <EFFORT>` | `low`, `medium`, `high`, `xhigh` |
| **codex** | `-c model_reasoning_effort="<level>"` | `low`, `medium`, `high`, `xhigh`, `max`, `ultra` |
| **prime-agent** | `--thinking <level>` | `off`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max` |

For `jcode`, `opencode`, `hermes`, `goose`, and `aider`, select effort through model variants (e.g., `gemini-3.7-flash-tiered`, `:thinking` suffix) or provider settings.

## Dispatch Rules

Treat native subagents and external harnesses as equal options. Choose based on task fit, provider capacity, cost, isolation, and parallelism. Always provide an explicit role, goal, constraints, permissions, and success criteria.

Choose the dispatch mode based on the role:

- **Manager:** Only use channels that support stable live sessions, resume capability, live status, interrupts, and worker coordination. Do not assume Manager capabilities from simple commands. Flags like `--print`, `exec`, or `--single` are only for Workers or Reviewers unless paired with a verified continuation channel.
- **Worker:** Only allow write access within an authorized workspace or sandbox. Require narrow repository checks and explicit staging paths. Never permit `git add -A`.
- **Reviewer:** Use a separate agent that did not author the work. Prefer hard read-only sandboxes or strict tool allowlists. If using a soft read-only plan mode, explicitly forbid edits, commits, pushes, and other mutations.

When using an external harness, find the correct command in [references/external-harnesses.md](references/external-harnesses.md) and use it exactly as written.

For headless Worker dispatch, you may automate confirmations only if the write scope is strictly bounded (by a sandbox, workspace flag, or dedicated worktree). Never add flags that bypass workspace or sandbox boundaries.
