CREATE TABLE schema_meta (version INTEGER NOT NULL);
INSERT INTO schema_meta(version) VALUES (5);
CREATE UNIQUE INDEX IF NOT EXISTS idx_schema_meta_singleton
    ON schema_meta((1));


CREATE TABLE IF NOT EXISTS tasks (
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

CREATE TABLE IF NOT EXISTS task_events (
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

CREATE TABLE IF NOT EXISTS runs (
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

CREATE TABLE IF NOT EXISTS herdr_bindings (
    run_id TEXT PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
    herdr_session TEXT NOT NULL,
    worker_name TEXT NOT NULL,
    agent_kind TEXT NOT NULL,
    session_source TEXT,
    session_agent TEXT,
    session_ref_kind TEXT CHECK (session_ref_kind IS NULL OR session_ref_kind IN ('id', 'path')),
    session_value TEXT,
    pane_id TEXT,
    tab_id TEXT,
    workspace_id TEXT,
    status TEXT NOT NULL CHECK (status IN ('pending', 'live', 'blocked', 'stale', 'unknown')),
    bound_at TEXT NOT NULL,
    reconciled_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS run_turns (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    purpose TEXT NOT NULL CHECK (purpose IN (
        'task', 'correction', 'clarification', 'review_follow_up'
    )),
    prompt TEXT NOT NULL,
    prompt_digest TEXT NOT NULL,
    artifact_path TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running', 'blocked', 'succeeded', 'failed', 'unknown')),
    lifecycle_evidence TEXT,
    summary TEXT,
    result_json TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    UNIQUE (run_id, ordinal),
    UNIQUE (run_id, artifact_path)
);

CREATE TABLE IF NOT EXISTS evaluations (
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

CREATE TABLE IF NOT EXISTS feedback (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    run_id TEXT REFERENCES runs(id),
    kind TEXT NOT NULL CHECK (kind IN ('preference', 'correction', 'failure', 'observation')),
    recurrence_key TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS promotions (
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

CREATE INDEX IF NOT EXISTS idx_tasks_state_priority
    ON tasks(state, priority DESC, created_at);
CREATE INDEX IF NOT EXISTS idx_feedback_recurrence_key
    ON feedback(recurrence_key, created_at);
CREATE INDEX IF NOT EXISTS idx_run_turns_run_ordinal
    ON run_turns(run_id, ordinal);
CREATE UNIQUE INDEX IF NOT EXISTS idx_active_herdr_worker
    ON herdr_bindings(herdr_session, worker_name)
    WHERE status <> 'stale';
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_open_turn_per_run
    ON run_turns(run_id)
    WHERE status = 'running';

CREATE TABLE IF NOT EXISTS maintenance_runs (
    id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('succeeded', 'failed')),
    summary_json TEXT NOT NULL,
    error_message TEXT
);

INSERT INTO tasks(id, title, goal, success_criteria, state, created_at, updated_at)
VALUES ('task_v5', 'v5 task', 'goal', 'criteria', 'running', '2026-01-01T00:00:00Z',
        '2026-01-01T00:00:00Z');
INSERT INTO runs(id, task_id, agent_role, status, started_at)
VALUES ('run_v5', 'task_v5', 'worker', 'running', '2026-01-01T00:00:00Z');
INSERT INTO run_turns(id, run_id, ordinal, purpose, prompt, prompt_digest, artifact_path,
                      status, summary, started_at, finished_at)
VALUES ('turn_done', 'run_v5', 1, 'task', 'p1', 'd1', '.bossmode/turns/turn_done.json',
        'succeeded', 'finished turn', '2026-01-01T00:00:00Z', '2026-01-01T00:01:00Z');
INSERT INTO run_turns(id, run_id, ordinal, purpose, prompt, prompt_digest, artifact_path,
                      status, started_at)
VALUES ('turn_open', 'run_v5', 2, 'task', 'p2', 'd2', '.bossmode/turns/turn_open.json',
        'running', '2026-01-01T00:02:00Z');
INSERT INTO feedback(id, task_id, kind, recurrence_key, content, created_at)
VALUES ('fb_v5', 'task_v5', 'correction', 'api.retry', 'bound the retries',
        '2026-01-01T00:03:00Z');
