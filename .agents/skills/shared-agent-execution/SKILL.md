---
name: agent-execution
description: Select model tiers, map reasoning effort, and dispatch delegated work through native or external agent harnesses. Use when a workflow must choose an agent model or effort level, or launch a manager, worker, or independent reviewer. Do not use for direct, undelegated tool calls.
---

# Agent Execution

Use this skill to select models, set reasoning effort, or configure agent harnesses when delegating work. The calling skill still owns task decomposition, authorization, workspace isolation, and acceptance criteria.

## Model Tiers

Tiers describe roles, not just quality. Match the model to the task's complexity and risk.

**Selection Rule**: Choose the tier below. For native dispatch, use the model name directly. For external harnesses, use the specific identifier from [references/external-harnesses.md](references/external-harnesses.md). Select the tier before the effort level: additional effort can improve bounded lower-tier work but does not substitute for a higher capability tier.

- **Tier 1: Strategic**
  - Models: `gpt-5.6-sol`, `claude-fable-5`
  - Usage: Strategic planning, architecture, high-risk tradeoffs, and final high-risk cross-cutting review.
- **Tier 2: Generalist**
  - Models: `gpt-5.6-terra`, `claude-opus-5`, `grok-4.6`, `gemini-3.7-flash-high`, `muse-spark-1.2`, `muse-spark-1.2-contributor`
  - Usage: Management, decomposition, integration, investigation, bounded adversarial review, and routine review.
- **Tier 3: Contributor**
  - Models: `gpt-5.6-luna`, `claude-sonnet-5`, `grok-4.5`, `gemini-3.7-flash-medium`, `gemini-3.7-flash-low` (low-cost bulk)
  - Usage: Focused implementation, bounded research, bulk work, and parallel coverage.

> Note: pi/jcode prefixed forms (openai-codex/, muse-spark/) map to same tier as unprefixed base.

## Reasoning Effort

Managers are persistent and span task types: launch Manager dispatches at Tier 2 `high` by default. For a Manager whose scope is genuinely strategic or high-risk, deliberately select Tier 1 `high` instead; all tier-specific effort ceilings still apply.

Single-shot Workers and Reviewers: recommend effort by tier and work type.

- **Tier 1:** `medium` for strategic planning. `high` for architecture, high-risk tradeoffs, and final high-risk cross-cutting review. `xhigh`, `max`, and `ultra` require an explicit current-task user request as the sole authorization; a charter, agent prompt, retry, policy, or history alone does not qualify. The calling skill's authority and origin rules govern how that request is relayed to and verified by a delegated agent before it acts.
- **Tier 2:** `high` for bounded cognitive work, including bounded adversarial review. `medium` for routine mechanical work. `xhigh` for difficult bounded investigation, integration, decomposition, or adversarial review, where the harness supports it.
- **Tier 3:** `high` for bounded implementation or research. `medium` for repetitive work. `xhigh` for difficult bounded work where the harness supports it. `low` only for deterministic, no-judgment work.

For single-shot work not covered by a tier's recommendation above, default to `medium`; this fallback never overrides an explicit Tier 2 or Tier 3 `high` recommendation.

Retries and replacements never auto-escalate tier or effort. If a task genuinely needs higher capability, select the higher tier deliberately; tier-specific effort ceilings still apply.

| Harness | Flag | Supported Values (Lowest to Highest) |
| :--- | :--- | :--- |
| **pi** | `--thinking <level>` | `off`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max` |
| **claude** | `--effort <level>` | `low`, `medium`, `high`, `xhigh`, `max` |
| **muse** | `--reasoning-effort <EFFORT>` | `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `ultra` |
| **agy** | `--effort <level>` | `low`, `medium`, `high` |
| **grok** | `--reasoning-effort <EFFORT>` | `low`, `medium`, `high`, `xhigh` |
| **codex** | `-c model_reasoning_effort="<level>"` | `low`, `medium`, `high`, `xhigh`, `max`, `ultra` |
| **prime-agent** | `--thinking <level>` | `off`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max` |

Across all tiers, use the highest harness-supported effort no greater than the selected or recommended level. For harnesses without an effort flag (`jcode`, `opencode`, `hermes`, `goose`, `aider`), constrain model-variant or provider selection instead (e.g., `gemini-3.7-flash-tiered`, `:thinking` suffix).

## Dispatch Rules

Treat native subagents and external harnesses as equal options. Choose based on task fit, provider capacity, cost, isolation, and parallelism. Always provide an explicit role, goal, constraints, permissions, and success criteria.

Choose the dispatch mode based on the role:

- **Manager:** Only use channels that support stable live sessions, resume capability, live status, interrupts, and worker coordination. Do not assume Manager capabilities from simple commands. Flags like `--print`, `exec`, or `--single` are only for Workers or Reviewers unless paired with a verified continuation channel.
- **Worker:** Only allow write access within an authorized workspace or sandbox. Require narrow repository checks and explicit staging paths. Never permit `git add -A`.
- **Reviewer:** Use a separate agent that did not author the work. Prefer hard read-only sandboxes or strict tool allowlists. If using a soft read-only plan mode, explicitly forbid edits, commits, pushes, and other mutations.

When using an external harness, find the correct command in [references/external-harnesses.md](references/external-harnesses.md) and use it exactly as written.

For headless Worker dispatch, you may automate confirmations only if the write scope is strictly bounded (by a sandbox, workspace flag, or dedicated worktree). Never add flags that bypass workspace or sandbox boundaries.
