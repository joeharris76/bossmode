```text
██████╗   ██████╗  ███████╗ ███████╗ ███╗   ███╗  ██████╗  ██████╗  ███████╗
██╔══██╗ ██╔═══██╗ ██╔════╝ ██╔════╝ ████╗ ████║ ██╔═══██╗ ██╔══██╗ ██╔════╝
██████╔╝ ██║   ██║ ███████╗ ███████╗ ██╔████╔██║ ██║   ██║ ██║  ██║ █████╗
██╔══██╗ ██║   ██║ ╚════██║ ╚════██║ ██║╚██╔╝██║ ██║   ██║ ██║  ██║ ██╔══╝
██████╔╝ ╚██████╔╝ ███████╗ ███████╗ ██║ ╚═╝ ██║ ╚██████╔╝ ██████╔╝ ███████╗
══════╝   ╚═════╝  ╚══════╝ ╚══════╝ ╚═╝     ╚═╝  ╚═════╝  ╚═════╝  ╚══════╝
```

# Bossmode

Stop babysitting coding agents. Bossmode makes you the CEO of `Get-It-Done`.

Bossmode is a skill for coordinating complex agent work through a single executive
session. One persistent, topic-named manager is assigned to each high level task,
tightly focused workers implement the component features and an independent
reviewer assesses the work.

You describe the outcome to the executive and the `bossmode` team handles the rest.

It is prompt-driven. There is no Bossmode runtime, package, command-line tool,
database, or scheduler to install from this repository.

## Install

Copy the `bossmode` skill and sibling `shared-agent-execution` skill from this
repository to your agent's skills. Most agents use the canonical
`.agents/skills/` directory but Claude Code uses `.claude/skills/`

The primary skills are:

- [`.agents/skills/bossmode/SKILL.md`](.agents/skills/bossmode/SKILL.md)
- [`.agents/skills/shared-agent-execution/SKILL.md`](.agents/skills/shared-agent-execution/SKILL.md)

```text
.agents/skills/
├── bossmode/
│   ├── SKILL.md                   #  Main skill file
│   ├── skill.yaml                 #  Skill metadata
│   └── references/
│       ├── manager.md             #  Manager workflow
│       └── recovery.md            #  Resume a session
└── shared-agent-execution/
    ├── SKILL.md                   #  Model/effort selection & dispatch
    ├── skill.yaml                 #  Skill metadata
    └── references/
        └── external-harnesses.md  #  External harness commands & config
```

## Use

Give an agent the outcome, boundaries, and evidence you need. For example:

```text
Use the Bossmode skill to implement rate limiting for the auth endpoints.
Success means the implementation sustains 100 requests per minute and allows
bursts of 20 requests, and passes focused tests. Limit changes to auth middleware
```

The executive session should report outcomes, decisions, blockers, and protected
approvals. Worker and run mechanics stay in the background unless you ask for them.

## Repository purpose

This repository is the canonical source of the Bossmode skill and sibling `shared-agent-execution` skill.

## Python prototype retirement

The experimental Python control plane was retired on 2026-08-27 and was never
published to PyPI. `v0.1.0` is the current clean-slate release; the prototype
is unsupported and non-operative.

This repository provides no runtime, CLI, database migration, or action on
existing local `.bossmode/control.db` files. They remain legacy state.

## License

Bossmode is available under the MIT License. See [LICENSE](LICENSE).
