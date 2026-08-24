CREATE TABLE schema_meta (
    version INTEGER NOT NULL
);
INSERT INTO schema_meta(version) VALUES (2);

CREATE TABLE tasks (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    goal TEXT NOT NULL,
    success_criteria TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN (
        'backlog', 'ready', 'running', 'evaluating', 'waiting_user', 'blocked',
        'succeeded', 'failed', 'archived'
    )),
    priority INTEGER NOT NULL DEFAULT 0,
    owner_thread_id TEXT,
    permissions_json TEXT NOT NULL DEFAULT '{}',
    next_action TEXT,
    blocked_on TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE task_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    event_type TEXT NOT NULL,
    from_state TEXT,
    to_state TEXT,
    actor TEXT NOT NULL,
    reason TEXT,
    evidence TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE runs (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    thread_id TEXT,
    agent_role TEXT NOT NULL,
    model TEXT,
    reasoning_effort TEXT,
    status TEXT NOT NULL CHECK (status IN ('running', 'finished')),
    outcome TEXT,
    summary TEXT,
    artifacts_json TEXT NOT NULL DEFAULT '[]',
    tokens INTEGER,
    duration_seconds REAL,
    retries INTEGER NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE TABLE herdr_bindings (
    run_id TEXT PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
    herdr_session TEXT NOT NULL,
    worker_name TEXT NOT NULL,
    agent_kind TEXT NOT NULL,
    session_source TEXT,
    session_agent TEXT,
    session_ref_kind TEXT CHECK (
        session_ref_kind IS NULL OR session_ref_kind IN ('id', 'path')
    ),
    session_value TEXT,
    pane_id TEXT,
    tab_id TEXT,
    workspace_id TEXT,
    status TEXT NOT NULL CHECK (
        status IN ('pending', 'live', 'blocked', 'stale', 'unknown')
    ),
    bound_at TEXT NOT NULL,
    reconciled_at TEXT NOT NULL,
    UNIQUE (herdr_session, worker_name)
);

CREATE TABLE run_turns (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    purpose TEXT NOT NULL CHECK (purpose IN (
        'task', 'correction', 'clarification', 'review_follow_up'
    )),
    prompt_digest TEXT NOT NULL,
    artifact_path TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('running', 'blocked', 'succeeded', 'failed', 'unknown')
    ),
    lifecycle_evidence TEXT,
    summary TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    UNIQUE (run_id, ordinal),
    UNIQUE (run_id, artifact_path)
);

CREATE TABLE evaluations (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    run_id TEXT REFERENCES runs(id),
    evaluator TEXT NOT NULL,
    passed INTEGER NOT NULL CHECK (passed IN (0, 1)),
    score REAL CHECK (score IS NULL OR (score >= 0 AND score <= 1)),
    evidence TEXT NOT NULL,
    notes TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE feedback (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    run_id TEXT REFERENCES runs(id),
    kind TEXT NOT NULL CHECK (kind IN ('preference', 'correction', 'failure', 'observation')),
    recurrence_key TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE promotions (
    id TEXT PRIMARY KEY,
    recurrence_key TEXT NOT NULL,
    target_layer TEXT NOT NULL CHECK (target_layer IN ('memory', 'skill', 'control')),
    status TEXT NOT NULL CHECK (status IN ('proposed', 'accepted', 'rejected', 'applied')),
    rationale TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (recurrence_key, target_layer)
);

CREATE INDEX idx_tasks_state_priority
    ON tasks(state, priority DESC, created_at);
CREATE INDEX idx_feedback_recurrence_key
    ON feedback(recurrence_key, created_at);
CREATE INDEX idx_run_turns_run_ordinal
    ON run_turns(run_id, ordinal);

INSERT INTO tasks(
    id, title, goal, success_criteria, state, permissions_json, created_at, updated_at
) VALUES (
    'task_v2', 'Historical task', 'Migrate', 'Preserve every record',
    'failed', '{}', 'then', 'then'
);
INSERT INTO runs(
    id, task_id, agent_role, status, outcome, summary, artifacts_json,
    retries, started_at, finished_at
) VALUES (
    'run_v2', 'task_v2', 'worker', 'finished', 'failed', 'historical run',
    '[]', 0, 'then', 'then'
);
INSERT INTO herdr_bindings(
    run_id, herdr_session, worker_name, agent_kind, status, bound_at, reconciled_at
) VALUES (
    'run_v2', 'bossmode', 'worker_v2', 'codex', 'live', 'then', 'then'
);
INSERT INTO run_turns(
    id, run_id, ordinal, purpose, prompt_digest, artifact_path,
    status, summary, started_at, finished_at
) VALUES (
    'turn_v2', 'run_v2', 1, 'task', 'digest', '.bossmode/turns/turn_v2.json',
    'failed', 'historical turn', 'then', 'then'
);
INSERT INTO evaluations(
    id, task_id, run_id, evaluator, passed, evidence, created_at
) VALUES ('eval_v2', 'task_v2', 'run_v2', 'reviewer', 0, 'historical evidence', 'then');
INSERT INTO feedback(
    id, task_id, run_id, kind, recurrence_key, content, created_at
) VALUES (
    'feedback_v2', 'task_v2', 'run_v2', 'failure',
    'historical.failure', 'historical feedback', 'then'
);
INSERT INTO promotions(
    id, recurrence_key, target_layer, status, rationale, evidence_json,
    created_at, updated_at
) VALUES (
    'promotion_v2', 'historical.failure', 'control', 'proposed',
    'historical rationale', '{}', 'then', 'then'
);
