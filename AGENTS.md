# Bossmode skill repository instructions

## Authority

- `.agents/skills/bossmode/SKILL.md` in this repository is the canonical
  source for the Bossmode skill.
- The `joeharris76/skill-sync-skills` catalog contains a downstream copy for
  distribution. It is not an editing authority.
- Do not create a second editable authority for the Bossmode skill.

## Repository scope

- This repository contains the canonical Bossmode skill and minimal supporting documentation.
- Runtime, package, database, CLI, scheduler, and Python code do not belong here.
- Keep the README and repository instructions minimal.
- Preserve local `.bossmode/`, `.todo-db/`, and `_project/` state. Do not delete or commit it.

## Change safety

- Update the canonical skill here first, then synchronize the downstream skill-sync catalog copy.
- Stage changed paths explicitly. Never use `git add -A`.
- Preserve unrelated work and use the repository's normal worktree and review safeguards.
