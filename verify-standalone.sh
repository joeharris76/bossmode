#!/usr/bin/env bash

set -euo pipefail

# Pin byte collation so `sort` output matches the hard-coded byte-ordered
# allowlists in the archive and package checks below.
export LC_ALL=C

repository_root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
cd "$repository_root"

expected_paths=$(printf '%s\n' \
  .agents/skills/bossmode/SKILL.md \
  .agents/skills/bossmode/references/agent-execution.md \
  .agents/skills/bossmode/references/external-harnesses.md \
  .agents/skills/bossmode/references/manager.md \
  .agents/skills/bossmode/references/recovery.md \
  .github/workflows/skill-parity.yml \
  .gitignore \
  AGENTS.md \
  LICENSE \
  README.md \
  verify-standalone.sh)

if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  actual_paths=$(git ls-files)
else
  actual_paths=$(find . -type f -not -path './.git/*' | sed 's|^./||' | sort)
fi
if test "$actual_paths" != "$expected_paths"; then
  echo "Repository tree must contain exactly the eleven approved paths." >&2
  printf 'Expected:\n%s\nActual:\n%s\n' "$expected_paths" "$actual_paths" >&2
  exit 1
fi

if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  python_paths=$(git ls-files '*.py')
else
  python_paths=$(find . -type f -name '*.py' -not -path './.git/*' | sort)
fi
if test -n "$python_paths"; then
  echo "Tracked Python files are not allowed." >&2
  printf '%s\n' "$python_paths" >&2
  exit 1
fi

for legacy_path in pyproject.toml uv.lock src scripts tests; do
  if test -e "$legacy_path"; then
    echo "Legacy path remains: $legacy_path" >&2
    exit 1
  fi
done

retired_sync_literal=skill
retired_sync_literal="${retired_sync_literal}-sync"
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  if git grep -n -I -- "$retired_sync_literal" >&2; then
    echo "Retired distribution literal remains in tracked content." >&2
    exit 1
  fi
elif grep -R -n -I --exclude-dir=.git -e "$retired_sync_literal" . >&2; then
  echo "Retired distribution literal remains in archive content." >&2
  exit 1
fi

skill=.agents/skills/bossmode/SKILL.md
execution=.agents/skills/bossmode/references/agent-execution.md
harnesses=.agents/skills/bossmode/references/external-harnesses.md
manager=.agents/skills/bossmode/references/manager.md
recovery=.agents/skills/bossmode/references/recovery.md

require_text() {
  file=$1
  text=$2
  if ! grep -Fq -- "$text" "$file"; then
    echo "Required skill contract missing from $file: $text" >&2
    exit 1
  fi
}

require_text "$skill" 'version: 0.3.2'
require_text "$skill" '-B-O-S-S-M-O-D-E-'
require_text "$skill" '[references/agent-execution.md](references/agent-execution.md)'
require_text "$skill" 'The Executive must never act as the Manager'
require_text "$skill" 'No verified live Manager means no implementation'
require_text "$skill" '[references/manager.md](references/manager.md)'
require_text "$skill" '[references/recovery.md](references/recovery.md)'
require_text "$skill" '**[REVIEW-AUTH-001]** Only the user may authorize a repository write.'
require_text "$manager" '[agent-execution.md](agent-execution.md)'
require_text "$manager" 'never `git add -A`'
require_text "$execution" '## Model Tiers'
require_text "$execution" '## Reasoning Effort Reference'
require_text "$execution" '[external-harnesses.md](external-harnesses.md)'
require_text "$execution" 'stable live session identity'
require_text "$harnesses" 'Only after an actual command failure may reactive diagnosis use'
require_text "$harnesses" 'Do not run those checks proactively.'
require_text "$harnesses" 'codex exec -C "$WORKSPACE" --model "$MODEL" --sandbox read-only "$PROMPT"'
require_text "$harnesses" 'Reviewer (Soft Read-Only)'
require_text "$recovery" 'Live runtime state is authoritative.'
require_text "$recovery" 'Stored handles are hints only'

if grep -Eq '^(## Model Tiers|## Reasoning Effort Reference)|command -v|Worker \(Write\):|/tmp/bossmode' \
  "$skill" "$manager" "$recovery"; then
  echo "Model and harness details must remain in their conditional references." >&2
  exit 1
fi

if grep -Eq 'command -v|Worker \(Write\):' "$execution"; then
  echo "Harness commands must remain in external-harnesses.md." >&2
  exit 1
fi

if grep -Eq '^(## Model Tiers|## Reasoning Effort Reference)' "$harnesses"; then
  echo "Model selection belongs in agent-execution.md." >&2
  exit 1
fi

external_routes=$(grep -RlF -- 'external-harnesses.md' .agents/skills/bossmode | sort)
if test "$external_routes" != "$execution"; then
  echo "Only agent-execution.md may route to external harness details." >&2
  printf 'Actual routes:\n%s\n' "$external_routes" >&2
  exit 1
fi

if grep -REq 'shared-[[:alnum:]-]+/SKILL\.md' .agents/skills/bossmode; then
  echo "Bossmode must not depend on catalog-external shared skills." >&2
  grep -REn 'shared-[[:alnum:]-]+/SKILL\.md' .agents/skills/bossmode >&2
  exit 1
fi

standalone_root=$(mktemp -d)
cleanup_standalone() {
  find "$standalone_root" -depth -delete
}
trap cleanup_standalone EXIT
cp -R .agents/skills/bossmode "$standalone_root/bossmode"
standalone_skill=$(cd "$standalone_root/bossmode" && pwd -P)

expected_skill_files=$(printf '%s\n' \
  SKILL.md \
  references/agent-execution.md \
  references/external-harnesses.md \
  references/manager.md \
  references/recovery.md)
actual_skill_files=$(find "$standalone_skill" -type f \
  | sed "s|^$standalone_skill/||" \
  | sort)
if test "$actual_skill_files" != "$expected_skill_files"; then
  echo "Standalone skill must contain exactly five files." >&2
  printf 'Expected:\n%s\nActual:\n%s\n' \
    "$expected_skill_files" "$actual_skill_files" >&2
  exit 1
fi

if grep -REq 'control\.db|\.bossmode/|bossmode (init|reconcile)|SCHEMA_VERSION' \
  "$standalone_skill"; then
  echo "Retired Bossmode control-plane assumptions are not allowed." >&2
  exit 1
fi

document_path_pattern='(^|[[:space:]`(])((/|~/|[[:alpha:]]:[/\\])|(\.\.?[/\\])|[[:alnum:]_.-]+[/\\])*[[:alnum:]_.-]+\.md(#[[:alnum:]_.-]+)?([[:space:]`).,;:]|$)'
has_unlinked_document_path() {
  sed -E 's/\[[^]]+\]\([^)]+\)//g' | grep -Eq "$document_path_pattern"
}

for rejected_path in \
  '/tmp/outside.md' '`/tmp/outside.md`' \
  '~/outside.md' '`~/outside.md`' \
  'C:\temp\outside.md' '`C:\temp\outside.md`' \
  'C:/temp/outside.md' '`C:/temp/outside.md`'; do
  if ! printf '%s\n' "$rejected_path" | has_unlinked_document_path; then
    echo "Document-path validator missed: $rejected_path" >&2
    exit 1
  fi
done
for accepted_text in \
  '[Manager](references/manager.md)' \
  'run `grep -F needle input.txt`' \
  'use model_reasoning_effort'; do
  if printf '%s\n' "$accepted_text" | has_unlinked_document_path; then
    echo "Document-path validator rejected valid text: $accepted_text" >&2
    exit 1
  fi
done

while IFS= read -r file; do
  if has_unlinked_document_path < "$file"; then
    echo "Markdown document paths must use checked Markdown links: $file" >&2
    exit 1
  fi

  while IFS= read -r target; do
    case "$target" in
      http://*|https://*|mailto:*|'#'*) continue ;;
    esac
    target=${target%%#*}
    target_dir=$(dirname "$target")
    target_name=$(basename "$target")
    if ! resolved_dir=$(cd "$(dirname "$file")/$target_dir" && pwd -P); then
      echo "Broken local link in $file: $target" >&2
      exit 1
    fi
    resolved=$resolved_dir/$target_name
    case "$resolved" in
      "$standalone_skill"/*) ;;
      *)
        echo "Local link escapes standalone skill in $file: $target" >&2
        exit 1
        ;;
    esac
    if ! test -f "$resolved"; then
      echo "Broken local link in $file: $target" >&2
      exit 1
    fi
  done < <(
    grep -oE '\[[^]]+\]\([^)]+\)' "$file" \
      | sed -E 's/^.*\(([^)]+)\)$/\1/' \
      || true
  )
done < <(find "$standalone_skill" -type f -name '*.md' | sort)

if grep -REq 'shared-[[:alnum:]-]+/SKILL\.md' "$standalone_skill"; then
  echo "Standalone copy retains a catalog-external dependency." >&2
  exit 1
fi

if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  empty_tree=$(git hash-object -t tree /dev/null)
  git diff --check "$empty_tree" HEAD
fi
