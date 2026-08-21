from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from collections.abc import Iterable
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

TASK_STATES = {
    "backlog",
    "ready",
    "running",
    "evaluating",
    "waiting_user",
    "blocked",
    "succeeded",
    "failed",
    "archived",
}
CREATE_TASK_STATES = {"backlog", "ready"}

TERMINAL_RUN_OUTCOMES = {"waiting_user", "blocked", "succeeded", "failed"}
HERDR_BINDING_STATUSES = {"pending", "live", "blocked", "stale", "unknown"}
TURN_PURPOSES = {"task", "correction", "clarification", "review_follow_up"}
TERMINAL_TURN_STATUSES = {"blocked", "succeeded", "failed", "unknown"}
HERDR_NAME_PATTERN = r"^[a-z][a-z0-9_-]{0,31}$"
MAX_TURN_RESULT_BYTES = 1_048_576
SCHEMA_VERSION = 3

ALLOWED_TRANSITIONS = {
    "backlog": {"ready", "archived"},
    "ready": {"blocked", "archived"},
    "running": set(),
    "evaluating": {"ready", "blocked", "archived"},
    "waiting_user": {"ready", "blocked", "archived"},
    "blocked": {"ready", "archived"},
    "succeeded": {"archived"},
    "failed": {"ready", "archived"},
    "archived": set(),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    version INTEGER NOT NULL
);

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
"""


class RegistryError(RuntimeError):
    """Raised when a registry invariant is violated."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


class Registry:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            schema_exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_meta'"
            ).fetchone()
            if schema_exists is None:
                connection.executescript(SCHEMA)
                connection.execute("INSERT INTO schema_meta(version) VALUES (?)", (SCHEMA_VERSION,))
                return
            version = connection.execute("SELECT version FROM schema_meta LIMIT 1").fetchone()
            if version is None:
                raise RegistryError("registry schema version is missing")
            current = version["version"]
            if current > SCHEMA_VERSION:
                raise RegistryError(
                    f"registry schema {current} is newer than supported {SCHEMA_VERSION}"
                )
            migrations = {
                1: self._migrate_v1_to_v2,
                2: self._migrate_v2_to_v3,
            }
            while current < SCHEMA_VERSION:
                migration = migrations.get(current)
                if migration is None:
                    raise RegistryError(f"no registry migration from schema {current}")
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    migration(connection)
                    current += 1
                    connection.execute("UPDATE schema_meta SET version = ?", (current,))
                    connection.commit()
                except sqlite3.Error as error:
                    connection.rollback()
                    raise RegistryError(
                        f"registry migration {current} -> {current + 1} failed: {error}"
                    ) from error

    @staticmethod
    def _migrate_v1_to_v2(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
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
            )
            """
        )
        connection.execute(
            """
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
            )
            """
        )
        connection.execute("CREATE INDEX idx_run_turns_run_ordinal ON run_turns(run_id, ordinal)")

    @staticmethod
    def _migrate_v2_to_v3(connection: sqlite3.Connection) -> None:
        connection.execute("ALTER TABLE herdr_bindings RENAME TO herdr_bindings_v2")
        connection.execute(
            """
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
                reconciled_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO herdr_bindings(
                run_id, herdr_session, worker_name, agent_kind,
                session_source, session_agent, session_ref_kind, session_value,
                pane_id, tab_id, workspace_id, status, bound_at, reconciled_at
            )
            SELECT
                run_id, herdr_session, worker_name, agent_kind,
                session_source, session_agent, session_ref_kind, session_value,
                pane_id, tab_id, workspace_id, status, bound_at, reconciled_at
            FROM herdr_bindings_v2
            """
        )
        connection.execute("DROP TABLE herdr_bindings_v2")
        connection.execute("ALTER TABLE run_turns ADD COLUMN result_json TEXT")
        connection.execute(
            """
            UPDATE herdr_bindings
            SET status = 'stale'
            WHERE run_id IN (SELECT id FROM runs WHERE status = 'finished')
            """
        )
        connection.execute(
            """
            CREATE UNIQUE INDEX idx_active_herdr_worker
            ON herdr_bindings(herdr_session, worker_name)
            WHERE status <> 'stale'
            """
        )
        connection.execute(
            """
            CREATE UNIQUE INDEX idx_one_open_turn_per_run
            ON run_turns(run_id)
            WHERE status = 'running'
            """
        )

    @contextmanager
    def _transaction(self) -> Iterable[sqlite3.Connection]:
        self.initialize()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def create_task(
        self,
        *,
        title: str,
        goal: str,
        success_criteria: str,
        state: str = "ready",
        priority: int = 0,
        permissions: dict[str, Any] | None = None,
        next_action: str | None = None,
    ) -> dict[str, Any]:
        if state not in CREATE_TASK_STATES:
            raise RegistryError(f"invalid initial task state: {state}")
        task_id = _id("task")
        timestamp = _now()
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO tasks(
                    id, title, goal, success_criteria, state, priority,
                    permissions_json, next_action, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    title,
                    goal,
                    success_criteria,
                    state,
                    priority,
                    _json(permissions or {}),
                    next_action,
                    timestamp,
                    timestamp,
                ),
            )
            self._record_event(
                connection,
                task_id=task_id,
                event_type="created",
                actor="user",
                to_state=state,
                reason="task created",
            )
        return self.get_task(task_id)

    def get_task(self, task_id: str) -> dict[str, Any]:
        self.initialize()
        with closing(self._connect()) as connection:
            task = _row(
                connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            )
            if task is None:
                raise RegistryError(f"task not found: {task_id}")
            task["permissions"] = json.loads(task.pop("permissions_json"))
            task["events"] = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM task_events WHERE task_id = ? ORDER BY id", (task_id,)
                )
            ]
            run_ids = [
                row["id"]
                for row in connection.execute(
                    "SELECT id FROM runs WHERE task_id = ? ORDER BY started_at", (task_id,)
                )
            ]
            task["evaluations"] = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM evaluations WHERE task_id = ? ORDER BY created_at", (task_id,)
                )
            ]
            task["feedback"] = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM feedback WHERE task_id = ? ORDER BY created_at", (task_id,)
                )
            ]
        task["runs"] = [self.get_run(run_id) for run_id in run_ids]
        return task

    def list_tasks(self, states: Iterable[str] | None = None) -> list[dict[str, Any]]:
        self.initialize()
        selected = list(states or [])
        for state in selected:
            if state not in TASK_STATES:
                raise RegistryError(f"unknown task state: {state}")
        query = "SELECT * FROM tasks"
        parameters: list[Any] = []
        if selected:
            placeholders = ",".join("?" for _ in selected)
            query += f" WHERE state IN ({placeholders})"
            parameters.extend(selected)
        query += " ORDER BY priority DESC, created_at, id"
        with closing(self._connect()) as connection:
            rows = [dict(row) for row in connection.execute(query, parameters)]
        for task in rows:
            task["permissions"] = json.loads(task.pop("permissions_json"))
        return rows

    def transition_task(
        self,
        task_id: str,
        to_state: str,
        *,
        actor: str,
        reason: str,
        evidence: str | None = None,
        next_action: str | None = None,
        blocked_on: str | None = None,
    ) -> dict[str, Any]:
        if to_state not in TASK_STATES:
            raise RegistryError(f"unknown task state: {to_state}")
        with self._transaction() as connection:
            task = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if task is None:
                raise RegistryError(f"task not found: {task_id}")
            from_state = task["state"]
            if to_state not in ALLOWED_TRANSITIONS[from_state]:
                raise RegistryError(f"invalid task transition: {from_state} -> {to_state}")
            changed = connection.execute(
                """
                UPDATE tasks
                SET state = ?, next_action = ?, blocked_on = ?, updated_at = ?
                WHERE id = ? AND state = ?
                """,
                (to_state, next_action, blocked_on, _now(), task_id, from_state),
            ).rowcount
            if changed != 1:
                raise RegistryError(f"concurrent task transition detected: {task_id}")
            self._record_event(
                connection,
                task_id=task_id,
                event_type="transition",
                actor=actor,
                from_state=from_state,
                to_state=to_state,
                reason=reason,
                evidence=evidence,
            )
        return self.get_task(task_id)

    def start_run(
        self,
        task_id: str,
        *,
        agent_role: str,
        thread_id: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        run_id = _id("run")
        with self._transaction() as connection:
            task = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if task is None:
                raise RegistryError(f"task not found: {task_id}")
            if task["state"] != "ready":
                raise RegistryError(f"task must be ready to start a run; found {task['state']}")
            open_run = connection.execute(
                "SELECT id FROM runs WHERE task_id = ? AND status = 'running' LIMIT 1",
                (task_id,),
            ).fetchone()
            if open_run is not None:
                raise RegistryError(f"task already has a running run: {open_run['id']}")
            timestamp = _now()
            changed = connection.execute(
                """
                UPDATE tasks
                SET state = 'running', owner_thread_id = ?, updated_at = ?
                WHERE id = ? AND state = 'ready'
                """,
                (thread_id, timestamp, task_id),
            ).rowcount
            if changed != 1:
                raise RegistryError(f"concurrent task dispatch detected: {task_id}")
            connection.execute(
                """
                INSERT INTO runs(
                    id, task_id, thread_id, agent_role, model, reasoning_effort,
                    status, started_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'running', ?)
                """,
                (run_id, task_id, thread_id, agent_role, model, reasoning_effort, timestamp),
            )
            self._record_event(
                connection,
                task_id=task_id,
                event_type="run_started",
                actor="supervisor",
                from_state="ready",
                to_state="running",
                reason=f"dispatched to {agent_role}",
                evidence=thread_id,
            )
        return self.get_run(run_id)

    def get_run(self, run_id: str) -> dict[str, Any]:
        self.initialize()
        with closing(self._connect()) as connection:
            run = _row(connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone())
            binding = _row(
                connection.execute(
                    "SELECT * FROM herdr_bindings WHERE run_id = ?", (run_id,)
                ).fetchone()
            )
            turns = [
                self._hydrate_turn(dict(row))
                for row in connection.execute(
                    "SELECT * FROM run_turns WHERE run_id = ? ORDER BY ordinal", (run_id,)
                )
            ]
        if run is None:
            raise RegistryError(f"run not found: {run_id}")
        run["artifacts"] = json.loads(run.pop("artifacts_json"))
        if binding is not None:
            binding["native_session"] = self._session_reference(binding)
            for key in (
                "session_source",
                "session_agent",
                "session_ref_kind",
                "session_value",
            ):
                binding.pop(key)
        run["herdr_binding"] = binding
        run["turns"] = turns
        return run

    def bind_herdr_run(
        self,
        run_id: str,
        *,
        herdr_session: str,
        worker_name: str,
        agent_kind: str,
        status: str = "live",
        session_source: str | None = None,
        session_agent: str | None = None,
        session_ref_kind: str | None = None,
        session_value: str | None = None,
        pane_id: str | None = None,
        tab_id: str | None = None,
        workspace_id: str | None = None,
    ) -> dict[str, Any]:
        if not herdr_session.strip():
            raise RegistryError("Herdr session name is required")
        if not re.fullmatch(HERDR_NAME_PATTERN, worker_name):
            raise RegistryError(f"invalid Herdr worker name: {worker_name}")
        if not agent_kind.strip():
            raise RegistryError("Herdr agent kind is required")
        if status not in HERDR_BINDING_STATUSES:
            raise RegistryError(f"invalid Herdr binding status: {status}")
        reference = (session_source, session_agent, session_ref_kind, session_value)
        if any(reference) and not all(reference):
            raise RegistryError(
                "native session source, agent, kind, and value must be supplied together"
            )
        if session_ref_kind is not None and session_ref_kind not in {"id", "path"}:
            raise RegistryError(f"invalid native session reference kind: {session_ref_kind}")

        timestamp = _now()
        with self._transaction() as connection:
            run = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if run is None:
                raise RegistryError(f"run not found: {run_id}")
            if run["status"] != "running":
                raise RegistryError(f"Herdr binding requires a running run; found {run['status']}")
            existing = connection.execute(
                "SELECT * FROM herdr_bindings WHERE run_id = ?", (run_id,)
            ).fetchone()
            claimed = connection.execute(
                """
                SELECT run_id FROM herdr_bindings
                WHERE herdr_session = ? AND worker_name = ? AND status <> 'stale'
                """,
                (herdr_session, worker_name),
            ).fetchone()
            if claimed is not None and claimed["run_id"] != run_id:
                raise RegistryError(
                    f"Herdr worker already bound to another run: {claimed['run_id']}"
                )
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO herdr_bindings(
                        run_id, herdr_session, worker_name, agent_kind,
                        session_source, session_agent, session_ref_kind, session_value,
                        pane_id, tab_id, workspace_id, status, bound_at, reconciled_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        herdr_session,
                        worker_name,
                        agent_kind,
                        session_source,
                        session_agent,
                        session_ref_kind,
                        session_value,
                        pane_id,
                        tab_id,
                        workspace_id,
                        status,
                        timestamp,
                        timestamp,
                    ),
                )
            else:
                stable = ("herdr_session", "worker_name", "agent_kind")
                supplied = {
                    "herdr_session": herdr_session,
                    "worker_name": worker_name,
                    "agent_kind": agent_kind,
                }
                for key in stable:
                    if existing[key] != supplied[key]:
                        raise RegistryError(f"refuse to replace Herdr binding {key}")
                existing_reference = tuple(
                    existing[key]
                    for key in (
                        "session_source",
                        "session_agent",
                        "session_ref_kind",
                        "session_value",
                    )
                )
                if any(reference) and any(existing_reference) and reference != existing_reference:
                    raise RegistryError("refuse to replace native Herdr session reference")
                connection.execute(
                    """
                    UPDATE herdr_bindings
                    SET session_source = COALESCE(?, session_source),
                        session_agent = COALESCE(?, session_agent),
                        session_ref_kind = COALESCE(?, session_ref_kind),
                        session_value = COALESCE(?, session_value),
                        pane_id = COALESCE(?, pane_id),
                        tab_id = COALESCE(?, tab_id),
                        workspace_id = COALESCE(?, workspace_id),
                        status = ?, reconciled_at = ?
                    WHERE run_id = ?
                    """,
                    (
                        session_source,
                        session_agent,
                        session_ref_kind,
                        session_value,
                        pane_id,
                        tab_id,
                        workspace_id,
                        status,
                        timestamp,
                        run_id,
                    ),
                )
        binding = self.get_run(run_id)["herdr_binding"]
        assert binding is not None
        return binding

    def start_turn(self, run_id: str, *, purpose: str, prompt: str) -> dict[str, Any]:
        if purpose not in TURN_PURPOSES:
            raise RegistryError(f"invalid turn purpose: {purpose}")
        if not prompt.strip():
            raise RegistryError("turn prompt is required")
        turn_id = _id("turn")
        artifact_path = f".continual/turns/{turn_id}.json"
        timestamp = _now()
        with self._transaction() as connection:
            run = connection.execute("SELECT status FROM runs WHERE id = ?", (run_id,)).fetchone()
            if run is None:
                raise RegistryError(f"run not found: {run_id}")
            if run["status"] != "running":
                raise RegistryError(f"turn requires a running run; found {run['status']}")
            binding = connection.execute(
                "SELECT status FROM herdr_bindings WHERE run_id = ?", (run_id,)
            ).fetchone()
            if binding is None:
                raise RegistryError("turn requires a Herdr binding")
            if binding["status"] != "live":
                raise RegistryError(
                    f"turn requires a live Herdr binding; found {binding['status']}"
                )
            open_turn = connection.execute(
                "SELECT id FROM run_turns WHERE run_id = ? AND status = 'running' LIMIT 1",
                (run_id,),
            ).fetchone()
            if open_turn is not None:
                raise RegistryError(f"run already has an open turn: {open_turn['id']}")
            ordinal = connection.execute(
                "SELECT COALESCE(MAX(ordinal), 0) + 1 FROM run_turns WHERE run_id = ?",
                (run_id,),
            ).fetchone()[0]
            connection.execute(
                """
                INSERT INTO run_turns(
                    id, run_id, ordinal, purpose, prompt_digest, artifact_path,
                    status, started_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'running', ?)
                """,
                (
                    turn_id,
                    run_id,
                    ordinal,
                    purpose,
                    hashlib.sha256(prompt.encode()).hexdigest(),
                    artifact_path,
                    timestamp,
                ),
            )
        return self.get_turn(turn_id)

    def finish_turn(
        self,
        turn_id: str,
        *,
        status: str,
        summary: str | None = None,
        lifecycle_evidence: str | None = None,
    ) -> dict[str, Any]:
        if status not in TERMINAL_TURN_STATUSES:
            raise RegistryError(f"invalid terminal turn status: {status}")
        if status != "succeeded" and (summary is None or not summary.strip()):
            raise RegistryError("turn summary is required")
        with self._transaction() as connection:
            turn = connection.execute("SELECT * FROM run_turns WHERE id = ?", (turn_id,)).fetchone()
            if turn is None:
                raise RegistryError(f"turn not found: {turn_id}")
            if turn["status"] != "running":
                raise RegistryError(f"turn already finished: {turn_id}")
            result = None
            if status == "succeeded":
                result = self._validated_turn_result(dict(turn), expected_summary=summary)
                summary = result["summary"]
            connection.execute(
                """
                UPDATE run_turns
                SET status = ?, summary = ?, lifecycle_evidence = ?,
                    result_json = ?, finished_at = ?
                WHERE id = ? AND status = 'running'
                """,
                (
                    status,
                    summary,
                    lifecycle_evidence,
                    _json(result) if result is not None else None,
                    _now(),
                    turn_id,
                ),
            )
        return self.get_turn(turn_id)

    def get_turn(self, turn_id: str) -> dict[str, Any]:
        return self._hydrate_turn(self._get_record("run_turns", turn_id))

    @staticmethod
    def _hydrate_turn(turn: dict[str, Any]) -> dict[str, Any]:
        result_json = turn.pop("result_json", None)
        turn["result"] = json.loads(result_json) if result_json is not None else None
        return turn

    @staticmethod
    def _validated_turn_result(
        turn: dict[str, Any], *, expected_summary: str | None
    ) -> dict[str, Any]:
        path = Path(turn["artifact_path"])
        try:
            with path.open("rb") as result_file:
                raw = result_file.read(MAX_TURN_RESULT_BYTES + 1)
        except OSError as error:
            raise RegistryError(
                f"turn result is unavailable at {path}: {error.strerror}"
            ) from error
        if len(raw) > MAX_TURN_RESULT_BYTES:
            raise RegistryError(f"turn result exceeds {MAX_TURN_RESULT_BYTES} bytes: {path}")
        try:
            result = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RegistryError(f"turn result is not valid JSON: {path}") from error
        if not isinstance(result, dict):
            raise RegistryError("turn result must be a JSON object")
        required = {"turn_id", "status", "summary", "artifacts"}
        missing = sorted(required - result.keys())
        if missing:
            raise RegistryError(f"turn result is missing fields: {', '.join(missing)}")
        if result["turn_id"] != turn["id"]:
            raise RegistryError("turn result ID does not match the open turn")
        if result["status"] != "succeeded":
            raise RegistryError("successful turn result must have status succeeded")
        if not isinstance(result["summary"], str) or not result["summary"].strip():
            raise RegistryError("turn result summary must be a non-empty string")
        if expected_summary is not None and expected_summary != result["summary"]:
            raise RegistryError("turn result summary does not match --summary")
        artifacts = result["artifacts"]
        if not isinstance(artifacts, list) or any(
            not isinstance(artifact, dict)
            or not isinstance(artifact.get("path"), str)
            or not artifact["path"].strip()
            or not isinstance(artifact.get("kind"), str)
            or not artifact["kind"].strip()
            for artifact in artifacts
        ):
            raise RegistryError(
                "turn result artifacts must contain non-empty path and kind strings"
            )
        return result

    @staticmethod
    def _session_reference(binding: dict[str, Any]) -> dict[str, str] | None:
        if not binding.get("session_value"):
            return None
        return {
            "source": binding["session_source"],
            "agent": binding["session_agent"],
            "kind": binding["session_ref_kind"],
            "value": binding["session_value"],
        }

    def finish_run(
        self,
        run_id: str,
        *,
        outcome: str,
        summary: str,
        artifacts: list[dict[str, Any]] | None = None,
        tokens: int | None = None,
        duration_seconds: float | None = None,
        retries: int = 0,
        blocked_on: str | None = None,
    ) -> dict[str, Any]:
        if outcome not in TERMINAL_RUN_OUTCOMES:
            raise RegistryError(f"invalid run outcome: {outcome}")
        with self._transaction() as connection:
            run = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if run is None:
                raise RegistryError(f"run not found: {run_id}")
            if run["status"] != "running":
                raise RegistryError(f"run already finished: {run_id}")
            open_turn = connection.execute(
                "SELECT id FROM run_turns WHERE run_id = ? AND status = 'running' LIMIT 1",
                (run_id,),
            ).fetchone()
            if open_turn is not None:
                raise RegistryError(f"run has an unfinished turn: {open_turn['id']}")
            task = connection.execute(
                "SELECT * FROM tasks WHERE id = ?", (run["task_id"],)
            ).fetchone()
            if task is None or task["state"] != "running":
                state = None if task is None else task["state"]
                raise RegistryError(f"run task must be running; found {state}")
            timestamp = _now()
            connection.execute(
                """
                UPDATE runs
                SET status = 'finished', outcome = ?, summary = ?, artifacts_json = ?,
                    tokens = ?, duration_seconds = ?, retries = ?, finished_at = ?
                WHERE id = ? AND status = 'running'
                """,
                (
                    outcome,
                    summary,
                    _json(artifacts or []),
                    tokens,
                    duration_seconds,
                    retries,
                    timestamp,
                    run_id,
                ),
            )
            connection.execute(
                """
                UPDATE herdr_bindings
                SET status = 'stale', reconciled_at = ?
                WHERE run_id = ?
                """,
                (timestamp, run_id),
            )
            task_outcome = "evaluating" if outcome == "succeeded" else outcome
            changed = connection.execute(
                """
                UPDATE tasks
                SET state = ?, blocked_on = ?, updated_at = ?
                WHERE id = ? AND state = 'running'
                """,
                (task_outcome, blocked_on, timestamp, run["task_id"]),
            ).rowcount
            if changed != 1:
                raise RegistryError(f"concurrent run completion detected: {run_id}")
            self._record_event(
                connection,
                task_id=run["task_id"],
                event_type="run_finished",
                actor=run["agent_role"],
                from_state="running",
                to_state=task_outcome,
                reason=summary,
                evidence=_json(artifacts or []),
            )
        return self.get_run(run_id)

    def add_evaluation(
        self,
        task_id: str,
        *,
        evaluator: str,
        passed: bool,
        evidence: str,
        run_id: str | None = None,
        score: float | None = None,
        notes: str | None = None,
    ) -> dict[str, Any]:
        if score is not None and not 0 <= score <= 1:
            raise RegistryError("evaluation score must be between 0 and 1")
        evaluation_id = _id("eval")
        with self._transaction() as connection:
            if (
                connection.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone()
                is None
            ):
                raise RegistryError(f"task not found: {task_id}")
            if run_id is not None:
                run = connection.execute(
                    "SELECT task_id, agent_role FROM runs WHERE id = ?", (run_id,)
                ).fetchone()
                if run is None:
                    raise RegistryError(f"run not found: {run_id}")
                if run["task_id"] != task_id:
                    raise RegistryError("evaluation run does not belong to task")
                if run["agent_role"] == evaluator:
                    raise RegistryError("evaluation must be independent from the run agent role")
            timestamp = _now()
            connection.execute(
                """
                INSERT INTO evaluations(
                    id, task_id, run_id, evaluator, passed, score, evidence, notes, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evaluation_id,
                    task_id,
                    run_id,
                    evaluator,
                    int(passed),
                    score,
                    evidence,
                    notes,
                    timestamp,
                ),
            )
            task = connection.execute("SELECT state FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if task is not None and task["state"] == "evaluating":
                evaluated_state = "succeeded" if passed else "failed"
                changed = connection.execute(
                    """
                    UPDATE tasks
                    SET state = ?, updated_at = ?
                    WHERE id = ? AND state = 'evaluating'
                    """,
                    (evaluated_state, timestamp, task_id),
                ).rowcount
                if changed != 1:
                    raise RegistryError(f"concurrent task evaluation detected: {task_id}")
                self._record_event(
                    connection,
                    task_id=task_id,
                    event_type="evaluated",
                    actor=evaluator,
                    from_state="evaluating",
                    to_state=evaluated_state,
                    reason="evaluation passed" if passed else "evaluation failed",
                    evidence=evidence,
                )
        return self._get_record("evaluations", evaluation_id)

    def add_feedback(
        self,
        task_id: str,
        *,
        kind: str,
        recurrence_key: str,
        content: str,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        if kind not in {"preference", "correction", "failure", "observation"}:
            raise RegistryError(f"invalid feedback kind: {kind}")
        feedback_id = _id("feedback")
        with self._transaction() as connection:
            if (
                connection.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone()
                is None
            ):
                raise RegistryError(f"task not found: {task_id}")
            if run_id is not None:
                run = connection.execute(
                    "SELECT task_id FROM runs WHERE id = ?", (run_id,)
                ).fetchone()
                if run is None:
                    raise RegistryError(f"run not found: {run_id}")
                if run["task_id"] != task_id:
                    raise RegistryError("feedback run does not belong to task")
            connection.execute(
                """
                INSERT INTO feedback(
                    id, task_id, run_id, kind, recurrence_key, content, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (feedback_id, task_id, run_id, kind, recurrence_key, content, _now()),
            )
        return self._get_record("feedback", feedback_id)

    def propose_promotions(self) -> list[dict[str, Any]]:
        self.initialize()
        created: list[dict[str, Any]] = []
        with self._transaction() as connection:
            keys = [
                row["recurrence_key"]
                for row in connection.execute(
                    "SELECT DISTINCT recurrence_key FROM feedback ORDER BY recurrence_key"
                )
            ]
            for key in keys:
                feedback = [
                    dict(row)
                    for row in connection.execute(
                        "SELECT * FROM feedback WHERE recurrence_key = ? ORDER BY created_at",
                        (key,),
                    )
                ]
                target = self._promotion_target(feedback)
                if target is None:
                    continue
                existing = connection.execute(
                    "SELECT 1 FROM promotions WHERE recurrence_key = ? AND target_layer = ?",
                    (key, target),
                ).fetchone()
                if existing is not None:
                    continue
                task_ids = sorted({item["task_id"] for item in feedback})
                placeholders = ",".join("?" for _ in task_ids)
                evaluations = [
                    dict(row)
                    for row in connection.execute(
                        f"SELECT * FROM evaluations WHERE task_id IN ({placeholders})",
                        task_ids,
                    )
                ]
                if target == "skill" and not any(item["passed"] for item in evaluations):
                    continue
                promotion_id = _id("promotion")
                timestamp = _now()
                evidence = {
                    "feedback_ids": [item["id"] for item in feedback],
                    "evaluation_ids": [item["id"] for item in evaluations],
                    "task_ids": task_ids,
                }
                rationale = self._promotion_rationale(target, feedback, evaluations)
                connection.execute(
                    """
                    INSERT INTO promotions(
                        id, recurrence_key, target_layer, status, rationale,
                        evidence_json, created_at, updated_at
                    ) VALUES (?, ?, ?, 'proposed', ?, ?, ?, ?)
                    """,
                    (promotion_id, key, target, rationale, _json(evidence), timestamp, timestamp),
                )
                created.append(
                    {
                        "id": promotion_id,
                        "recurrence_key": key,
                        "target_layer": target,
                        "status": "proposed",
                        "rationale": rationale,
                        "evidence": evidence,
                        "created_at": timestamp,
                        "updated_at": timestamp,
                    }
                )
        return created

    def list_promotions(self, status: str | None = None) -> list[dict[str, Any]]:
        self.initialize()
        query = "SELECT * FROM promotions"
        parameters: list[Any] = []
        if status is not None:
            query += " WHERE status = ?"
            parameters.append(status)
        query += " ORDER BY created_at, id"
        with closing(self._connect()) as connection:
            rows = [dict(row) for row in connection.execute(query, parameters)]
        for promotion in rows:
            promotion["evidence"] = json.loads(promotion.pop("evidence_json"))
        return rows

    def set_promotion_status(self, promotion_id: str, status: str) -> dict[str, Any]:
        if status not in {"accepted", "rejected", "applied"}:
            raise RegistryError(f"invalid promotion status: {status}")
        with self._transaction() as connection:
            current = connection.execute(
                "SELECT status FROM promotions WHERE id = ?", (promotion_id,)
            ).fetchone()
            if current is None:
                raise RegistryError(f"promotion not found: {promotion_id}")
            allowed = {
                "proposed": {"accepted", "rejected"},
                "accepted": {"applied", "rejected"},
                "rejected": set(),
                "applied": set(),
            }
            if status not in allowed[current["status"]]:
                raise RegistryError(
                    f"invalid promotion transition: {current['status']} -> {status}"
                )
            connection.execute(
                "UPDATE promotions SET status = ?, updated_at = ? WHERE id = ?",
                (status, _now(), promotion_id),
            )
        return self._get_promotion(promotion_id)

    def supervisor_tick(self) -> dict[str, Any]:
        created = self.propose_promotions()
        ready = self.list_tasks(["ready"])
        needs_user = self.list_tasks(["waiting_user"])
        blocked = self.list_tasks(["blocked"])
        active = [self.get_task(task["id"]) for task in self.list_tasks(["running"])]
        needs_evaluation = [self.get_task(task["id"]) for task in self.list_tasks(["evaluating"])]
        return {
            "dispatch": ready[0] if ready and not active and not needs_evaluation else None,
            "active": active,
            "needs_evaluation": needs_evaluation,
            "needs_user": needs_user,
            "blocked": blocked,
            "new_promotion_proposals": created,
            "promotion_proposals": self.list_promotions("proposed"),
        }

    @staticmethod
    def _promotion_target(feedback: list[dict[str, Any]]) -> str | None:
        kinds = [item["kind"] for item in feedback]
        if kinds.count("failure") >= 2:
            return "control"
        if kinds.count("correction") >= 2:
            return "skill"
        if "preference" in kinds or "observation" in kinds:
            return "memory"
        return None

    @staticmethod
    def _promotion_rationale(
        target: str,
        feedback: list[dict[str, Any]],
        evaluations: list[dict[str, Any]],
    ) -> str:
        passed = sum(item["passed"] for item in evaluations)
        if target == "control":
            count = len(feedback)
            return f"Repeated failure appeared {count} times; propose deterministic enforcement."
        if target == "skill":
            count = len(feedback)
            return (
                f"Repeated correction appeared {count} times with {passed} passing evaluation(s); "
                "propose a tested reusable workflow."
            )
        return "Preference or contextual observation should be available for future retrieval."

    @staticmethod
    def _record_event(
        connection: sqlite3.Connection,
        *,
        task_id: str,
        event_type: str,
        actor: str,
        from_state: str | None = None,
        to_state: str | None = None,
        reason: str | None = None,
        evidence: str | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO task_events(
                task_id, event_type, from_state, to_state, actor, reason, evidence, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (task_id, event_type, from_state, to_state, actor, reason, evidence, _now()),
        )

    def _get_record(self, table: str, record_id: str) -> dict[str, Any]:
        if table not in {"evaluations", "feedback", "run_turns"}:
            raise RegistryError(f"unsupported record table: {table}")
        self.initialize()
        with closing(self._connect()) as connection:
            record = _row(
                connection.execute(f"SELECT * FROM {table} WHERE id = ?", (record_id,)).fetchone()
            )
        if record is None:
            raise RegistryError(f"record not found: {record_id}")
        return record

    def _get_promotion(self, promotion_id: str) -> dict[str, Any]:
        self.initialize()
        with closing(self._connect()) as connection:
            promotion = _row(
                connection.execute(
                    "SELECT * FROM promotions WHERE id = ?", (promotion_id,)
                ).fetchone()
            )
        if promotion is None:
            raise RegistryError(f"promotion not found: {promotion_id}")
        promotion["evidence"] = json.loads(promotion.pop("evidence_json"))
        return promotion
