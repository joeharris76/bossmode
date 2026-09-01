# Bossmode skill repository instructions

## Authority

- `.agents/skills/bossmode/SKILL.md` and `.agents/skills/shared-agent-execution/SKILL.md`
  in this repository are the canonical sources for the Bossmode and `shared-agent-execution` skills.
- This repository is the sole editable authority for these skills.
- Do not create a second editable authority or distribution source.

## Repository scope

- This repository contains the canonical Bossmode skill and sibling `shared-agent-execution` skill with minimal supporting documentation.
- Runtime, package, database, CLI, scheduler, and Python code do not belong here.
- Keep the README and repository instructions minimal.
- Preserve local `.bossmode/`, `.todo-db/`, and `_project/` state. Do not delete or commit it.

## Change safety

- Update the canonical skills only in this repository.
- Stage changed paths explicitly. Never use `git add -A`.
- Preserve unrelated work and use the repository's normal worktree and review safeguards.
