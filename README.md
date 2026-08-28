# Bossmode

Bossmode is a skill for organizing complex agent work through an executive,
accountable managers, focused workers, and independent review.

It is prompt-driven. There is no Bossmode runtime, package, command-line tool,
database, or scheduler to install from this repository.

## Install

Install Bossmode directly from this repository. For a local installation, copy
the canonical `.agents/skills/bossmode` directory into the skill directory used
by your agent environment. The copied directory is self-contained and requires
no other skills.

Canonical source: [`.agents/skills/bossmode/SKILL.md`](.agents/skills/bossmode/SKILL.md)

Validate a clone or extracted archive with `./verify-standalone.sh`.

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

This repository is the canonical source of the Bossmode skill. Its validator
rejects tracked Python, legacy package paths, and files outside the intended
minimal repository tree.

## Python prototype retirement

The experimental Python control plane was retired on 2026-08-27 and was never
published to PyPI. The historical `v0.1.0` tag remains only for source history;
the prototype is unsupported and non-operative.

This repository provides no runtime, CLI, database migration, or action on
existing local `.bossmode/control.db` files. They remain legacy state.

## License

Bossmode is available under the MIT License. See [LICENSE](LICENSE).
