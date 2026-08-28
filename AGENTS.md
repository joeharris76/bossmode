# Bossmode skill repository instructions

## Authority

- `.agents/skills/bossmode/SKILL.md` in this repository is the canonical
  source for the Bossmode skill.
- This repository is the sole editable authority for the Bossmode skill.
- Do not create a second editable authority or distribution source.

## Repository scope

- This repository contains the canonical Bossmode skill and minimal supporting documentation.
- Runtime, package, database, CLI, scheduler, and Python code do not belong here.
- Keep the README and repository instructions minimal.
- Preserve local `.bossmode/`, `.todo-db/`, and `_project/` state. Do not delete or commit it.

## Change safety

- Update the canonical skill only in this repository.
- Stage changed paths explicitly. Never use `git add -A`.
- Preserve unrelated work and use the repository's normal worktree and review safeguards.
