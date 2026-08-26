# ADR 0004: Durable Team Hierarchy Without Runtime Side Effects

Status: Accepted
Date: 2026-08-27

## Context

Bossmode needs durable organization for parallel teams before it needs Herdr provisioning, Git worktree creation, resource claims, reviewer gatekeeping, automatic cleanup, reporting aggregation, or executor-selection policy. ADR 0006 and the owned-resources ledger already cover registry authority and strict ownership. The remaining gap is pure hierarchy.

## Decision

One migration adds two tables and two columns:

- \`teams(id, root_task_id REFERENCES tasks, parent_team_id REFERENCES teams, name, team_status CHECK(planned/active/archived), team_outcome CHECK(succeeded/failed/cancelled nullable), agent_kind, scope_json, created_at, updated_at) UNIQUE(root_task_id, name)\` — status and outcome are qualified and distinct; \`agent_kind\` is qualified (no unqualified \`kind\` column).
- \`team_members(team_id REFERENCES teams, task_id REFERENCES tasks, member_role CHECK(manager/member), added_at) PRIMARY KEY(team_id, task_id)\` — at most one \`member_role='manager'\` per team.
- \`tasks.team_id REFERENCES teams\` and \`tasks.parent_task_id REFERENCES tasks\` nullable FKs — membership and parent-task lineage.

Registry exposes exactly five pure operations (\`create_team\`, \`get_team\`, \`list_teams\`, \`attach_task_to_team\`, \`transition_team\`) with one canonical spelling. CLI exposes \`bossmode team {create,show,list,attach-task,transition}\`. No alias \`team_kind\`, no unqualified \`kind\`.

Invariants: team root must have \`parent_task_id\` NULL and \`team_id\` NULL; parent team must share same \`root_task_id\` and not be \`archived\`; attach rejects cross-team reuse, archived teams, duplicate manager; transition enforces \`planned -> active -> archived\` with \`team_outcome\` only on \`archived\`.

No Herdr subprocess, Git worktree remove, resource claim, evaluator_run_id, or reporting behavior is added.

## Consequences

Legacy flows unchanged. Later work adds Git writer, claims, reviewer gate, and closeout atop this durable model.

## References

- \`src/bossmode/registry.py:MIGRATION_V9_TO_V10_TEAM_SQL\`
- \`src/bossmode/registry.py:TEAM_STATUSES / TEAM_OUTCOMES\`
- \`src/bossmode/cli.py:team\`
