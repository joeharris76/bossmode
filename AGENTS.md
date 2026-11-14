# Bossmode skill repository instructions

## Authority

- `.agents/skills/bossmode/SKILL.md` in this repository is the canonical
  source for the Bossmode skill.
- This repository is the sole editable authority for the Bossmode skill.
- Do not create a second editable authority or distribution source.
- The `shared-agent-execution` skill is owned by the `skill-sync-skills`
  repository (`skills/shared-agent-execution`). The copy under
  `.agents/skills/shared-agent-execution/` is a snapshot that Bossmode reads
  at run time; do not edit it here.

## Repository scope

- This repository contains the canonical Bossmode skill with minimal supporting documentation.
- Runtime, package, database, CLI, scheduler, and Python code do not belong here.
- Keep the README and repository instructions minimal.
- Preserve local `.bossmode/` and `.tractable/` state. Do not delete or commit it.

## Change safety

- Update the Bossmode skill only in this repository.
- Make changes on a task branch per `shared-change-framework` Section 4; leave unrelated changes untouched.
