# Bossmode

Bossmode is a skill for organizing complex agent work through an executive,
accountable managers, focused workers, and independent review.

It is prompt-driven. There is no Bossmode runtime, package, command-line tool,
database, or scheduler to install from this repository.

## Install

Install Bossmode from the skill-sync global catalog when skill-sync is available.
The catalog distributes a downstream copy of the canonical skill to supported agents.

For a manual installation, copy this repository's canonical
`.agents/skills/bossmode` directory into the skill directory used by your agent
environment.

Canonical source: [`.agents/skills/bossmode/SKILL.md`](.agents/skills/bossmode/SKILL.md)

The [skill-sync-skills catalog](https://github.com/joeharris76/skill-sync-skills/tree/main/skills/bossmode)
is a downstream distribution copy.

## Use

Give an agent the outcome, boundaries, and evidence you need. For example:

```text
Use the Bossmode skill to implement rate limiting for the auth endpoints.
Success means the implementation uses a token bucket, sustains 100 requests
per minute, allows bursts of 20 requests, and passes focused tests. Limit edits
to the auth middleware and its tests. Require an independent reviewer before
calling the task complete. Ask before expanding permissions.
```

The executive session should report outcomes, decisions, blockers, and protected
approvals. Worker and run mechanics stay in the background unless you ask for them.

## Repository purpose

This repository is the canonical source of the Bossmode skill. Its workflow
rejects tracked Python, legacy package paths, and files outside the intended
minimal repository tree.

Make skill changes here first. Then synchronize the downstream skill-sync
catalog copy.

## Python prototype retirement

The experimental Python control plane was retired on 2026-08-27. It was never
published to PyPI. The historical `v0.1.0` tag remains available for source
history, but the prototype is unsupported.

There is no runtime, CLI, or database migration. Existing local
`.bossmode/control.db` files are legacy state. This repository transition does
not delete them.

## License

Bossmode is available under the MIT License. See [LICENSE](LICENSE).
