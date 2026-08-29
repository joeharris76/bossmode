# Agent Execution

Read this reference when pairing the Manager or selecting a Worker or
Independent Reviewer. Bossmode owns decomposition, workspace isolation, and
acceptance criteria, and enforces the authority the user granted.

## Model Tiers

Tiers describe operating roles, not absolute quality rankings. Match models to
task complexity and risk.

*Selection Rule*: Pick the tier here. For native dispatch, choose the listed
tier model directly. When an external harness is selected, take its exact
harness-specific identifier from the External Harness Configurations section
below. Default reasoning effort to `medium`. Use maximum effort only for Tier 1
adversarial review; use `low` for mechanical bulk work.

- **Tier 1: Strategic**
  - Models: `gpt-5.6-sol`, `claude-fable-5`, `grok-4.6`, `gemini-3.7-flash-high`
  - Usage: Strategic planning, architecture, high-risk tradeoffs, and final adversarial review.
- **Tier 2: Generalist**
  - Models: `gpt-5.6-terra`, `claude-opus-5`, `grok-4.5`, `gemini-3.7-flash-medium`, `muse-spark-1.2`
  - Usage: Management, decomposition, integration, investigation, and routine review.
- **Tier 3: Contributor**
  - Models: `gpt-5.6-luna`, `claude-sonnet-5`, `gemini-3.7-flash-low`, `gemini-3.7-flash-tiered`, `muse-spark-1.2-contributor`
  - Usage: Focused implementation, bounded research, bulk work, and parallel coverage.

## Reasoning Effort Reference

| Harness | CLI Flag / Option | Supported Values (Lowest to Highest) |
| :--- | :--- | :--- |
| **pi** | `--thinking <level>` | `off`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max` |
| **claude** | `--effort <level>` | `low`, `medium`, `high`, `xhigh`, `max` |
| **muse** | `--reasoning-effort <EFFORT>` | `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `ultra` |
| **agy** | `--effort <level>` | `low`, `medium`, `high` |
| **grok** | `--reasoning-effort <EFFORT>` | `low`, `medium`, `high`, `xhigh` |
| **codex** | `-c model_reasoning_effort="<level>"` | `low`, `medium`, `high`, `xhigh`, `max`, `ultra` |
| **prime-agent** | `--thinking <level>` | `off`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max` |

For `jcode`, `opencode`, `hermes`, `goose`, and `aider`, effort is selected via
model variants such as `gemini-3.7-flash-tiered`, a `:thinking` suffix, or
provider settings.

## Dispatch Rules

Treat native subagents and external harnesses as peer dispatch choices. Select
between them using the factors relevant to the assignment: task and model fit,
provider usage or capacity, cost, isolation or read-only strength, and useful
parallelism. Assign an explicit role, bounded goal, path constraints,
permission scope, success criteria, and output contract.

Choose the dispatch mode from the delegated role:

- **Manager:** select a channel only when current runtime capabilities or
  behavioral evidence verify a stable live session identity, resume or
  follow-up, live status and interrupt, and a working path to dispatch or
  coordinate Workers and Reviewers. Do not infer Manager capability from a
  one-shot Worker or Reviewer command. Invocations such as `--print`, `exec`,
  `--single`, or equivalents are not Manager-capable unless a separate verified
  continuation channel supplies every required capability; they remain valid
  Worker or Reviewer choices.
- **Worker:** use a write-capable mode only within the authorized workspace or
  sandbox. Require the repository's narrowest proving check and explicit-path
  staging; never permit `git add -A`.
- **Reviewer:** use a separate dispatch that did not author the work. Prefer a
  hard read-only sandbox or tool allowlist. A plan mode is soft read-only and
  requires explicit findings-only instructions that forbid edits, commits,
  pushes, and other mutations.

Only when selecting an external or headless harness, read the External Harness
Configurations section below, choose the documented command for the role, and
use it directly.

Headless Worker dispatch may automate routine tool confirmations only when
write scope is already bounded by a sandbox, workspace flag, or dedicated
worktree, and only for already-authorized actions. A confirmation flag never
grants authority and never substitutes for a credential, trust, permission,
destructive, merge, release, deployment, or activation approval. Never add
flags that remove workspace, sandbox, or tool boundaries.

## External Harness Configurations

Use these known-good direct configurations whenever an external harness is
selected. Choose the documented command for the delegated role and use it
directly. Only after an actual command failure may reactive diagnosis use
`command -v` and the installed `--help` output to distinguish a missing binary
from flag drift. Do not run those checks proactively.

- Worker commands require already-authorized write scope bounded by a sandbox,
  workspace, or dedicated worktree. Confirmation automation in a documented
  Worker command is allowed only within that bounded scope.
- Reviewer commands use the declared **Hard Read-Only** or **Soft Read-Only**
  classification. Reinforce Soft Read-Only with findings-only instructions that
  forbid edits, commits, pushes, and other mutations.

### Frontier Lab Harnesses

- **codex**
  - Worker (Write): `codex exec -C "$WORKSPACE" --model "$MODEL" --sandbox workspace-write "$PROMPT"`
  - Reviewer (Hard Read-Only): `codex exec -C "$WORKSPACE" --model "$MODEL" --sandbox read-only "$PROMPT"`
  - Known-good models: `gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna`
  - Effort: Optional `-c model_reasoning_effort="<level>"`
- **claude**
  - Worker (Write): `(cd "$WORKSPACE" && claude --print --model "$MODEL" --effort "$EFFORT" "$PROMPT")`
  - Reviewer (Hard Read-Only): `(cd "$WORKSPACE" && claude --print --tools Read,Grep,Glob --model "$MODEL" --effort "$EFFORT" "$PROMPT")`
  - Known-good models: `claude-fable-5`, `claude-opus-5`, `claude-sonnet-5`
- **agy**
  - Worker (Write): `(cd "$WORKSPACE" && agy --model "$MODEL" --effort "$EFFORT" --print="$PROMPT")`
  - Reviewer (Soft Read-Only): `(cd "$WORKSPACE" && agy --model "$MODEL" --effort "$EFFORT" --mode plan --print="$PROMPT")`
  - Known-good models: `gemini-3.7-flash-high`, `gemini-3.7-flash-medium`, `gemini-3.7-flash-low`
- **grok**
  - Worker (Write): `grok --cwd "$WORKSPACE" --single "$PROMPT" --model "$MODEL" --reasoning-effort "$EFFORT"`
  - Reviewer (Soft Read-Only): `grok --cwd "$WORKSPACE" --single "$PROMPT" --model "$MODEL" --reasoning-effort "$EFFORT" --permission-mode plan`
  - Known-good models: `grok-4.6`, `grok-4.5`
- **muse**
  - Worker (Write): `muse exec --workspace "$WORKSPACE" --disable-approval --model "$MODEL" --reasoning-effort "$EFFORT" "$PROMPT"`
  - Reviewer (Hard Read-Only): `muse exec --workspace "$WORKSPACE" --disable-approval --disable-write --disable-shell --model "$MODEL" --reasoning-effort "$EFFORT" "$PROMPT"`
  - Known-good models: `muse-spark-1.2-contributor`, `muse-spark-1.2`
  - Note: Unset invalid credentials with `env -u META_API_KEY` before execution.

### Extensible and Community Harnesses

- **pi**
  - Worker (Write): `(cd "$WORKSPACE" && pi --print --model "$MODEL" --thinking "$EFFORT" "$PROMPT")`
  - Reviewer (Hard Read-Only): `(cd "$WORKSPACE" && pi --print --tools read,grep,find,ls --model "$MODEL" --thinking "$EFFORT" "$PROMPT")`
  - Known-good models: `openai-codex/gpt-5.6-sol`, `openai-codex/gpt-5.6-terra`, `openai-codex/gpt-5.6-luna`, `anthropic/claude-fable-5`, `anthropic/claude-opus-5`, `anthropic/claude-sonnet-5`, `xai/grok-4.6`, `xai/grok-4.5`, `muse-spark/muse-spark-1.2-contributor`
- **jcode**
  - Worker (Write): `jcode run -C "$WORKSPACE" --model "$MODEL" "$PROMPT"`
  - Reviewer (Hard Read-Only): `jcode run -C "$WORKSPACE" --disable-base-tools --tools read --model "$MODEL" "$PROMPT"`
  - Known-good models: `gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna`, `claude-fable-5`, `claude-opus-5`, `claude-sonnet-5`, `gemini-3.7-flash-tiered`, `muse-spark-1.2-contributor`
- **goose**
  - Worker (Write): `(cd "$WORKSPACE" && goose run --text "$PROMPT" --no-session --provider "$PROVIDER" --model "$MODEL")`
  - Reviewer (Hard Read-Only): `(cd "$WORKSPACE" && goose review --prompt "$CRITERIA_FILE" --model "$MODEL")`
- **prime-agent**
  - Worker (Write): `prime-agent -p --cwd "$WORKSPACE" --provider "$PROVIDER" --model "$MODEL" --thinking "$EFFORT" "$PROMPT"`
  - Reviewer (Hard Read-Only): `prime-agent -p --tools read,grep,find,ls --cwd "$WORKSPACE" --provider "$PROVIDER" --model "$MODEL" --thinking "$EFFORT" "$PROMPT"`
- **opencode**
  - Worker (Write): `(cd "$WORKSPACE" && opencode run -m "$MODEL" "$PROMPT")`
  - Reviewer (Soft Read-Only): `(cd "$WORKSPACE" && opencode run --agent plan -m "$MODEL" "$PROMPT")`
  - Note: Model format is `<provider>/<model>`.
- **hermes**
  - Worker (Write): `(cd "$WORKSPACE" && hermes chat -q "$PROMPT")`
  - Reviewer (Hard Read-Only): `(cd "$WORKSPACE" && hermes chat -q --tools read,search "$PROMPT")`
- **aider**
  - Worker (Write): `(cd "$WORKSPACE" && aider --model "$MODEL" --message "$PROMPT" --yes-always --no-auto-commits)`
  - Reviewer (Hard Read-Only): `(cd "$WORKSPACE" && aider --model "$MODEL" --message "$PROMPT" --chat-mode ask)`
