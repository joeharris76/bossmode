from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import sqlite3
import stat
import time
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
TERMINAL_TURN_OUTCOMES = {"blocked", "succeeded", "failed", "unknown"}
FEEDBACK_CATEGORIES = {"preference", "correction", "failure", "observation"}
HERDR_NAME_PATTERN = r"^[a-z][a-z0-9_-]{0,31}$"
MAX_TURN_RESULT_BYTES = 1_048_576
SQLITE_BUSY_TIMEOUT_MS = 5_000
SCHEMA_VERSION = 6

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

# States reachable as an explicit `task transition` target. Lifecycle commands
# (`run start`, `run finish`, `evaluate`) own every other state change, so the
# remaining six members of TASK_STATES are never valid transition destinations.
TRANSITION_TARGET_STATES = frozenset().union(*ALLOWED_TRANSITIONS.values())

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    version INTEGER NOT NULL
);
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
    status TEXT NOT NULL CHECK (status IN ('running', 'finished')),
    outcome TEXT CHECK (outcome IS NULL OR outcome IN (
        'blocked', 'succeeded', 'failed', 'unknown'
    )),
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
    category TEXT NOT NULL CHECK (category IN (
        'preference', 'correction', 'failure', 'observation'
    )),
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


def _repository_root(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    for candidate in (resolved, *resolved.parents):
        if (candidate / ".git").exists():
            return candidate
    return resolved


def _validate_registry_ownership(path: Path) -> None:
    """Reject a standard registry path owned by a different worktree."""
    resolved = path.expanduser().resolve()
    if resolved.name != "control.db" or resolved.parent.name != ".bossmode":
        return

    current_root = _repository_root(Path.cwd())
    database_root = resolved.parent.parent
    if database_root == current_root or not (database_root / ".git").exists():
        return
    raise RegistryError(
        "registry path belongs to a different worktree: "
        f"current={current_root}, database={database_root}; "
        "run Bossmode from the owning checkout or use a dedicated shared registry"
    )


class Registry:
    def __init__(self, path: str | Path) -> None:
        _validate_registry_ownership(Path(path))
        self.path = Path(path)
        self._schema_ready = False

    def initialize(self) -> None:
        """Create or migrate the registry, at most once per instance.

        Every transaction helper calls this, so a single `reconcile()` used to
        open four connections and take four BEGIN IMMEDIATE write locks just to
        re-read an unchanged schema version. Only the create-or-migrate path is
        cached here. Compatibility is still enforced on every transaction by
        `_assert_schema_current`, using that transaction's own connection, so a
        long-lived instance cannot keep writing after another process migrates
        the database to an unsupported schema.
        """
        if self._schema_ready:
            return
        self._ensure_schema()
        self._schema_ready = True

    def _assert_schema_current(self, connection: sqlite3.Connection) -> None:
        try:
            row = connection.execute("SELECT version FROM schema_meta").fetchone()
        except sqlite3.Error as error:
            raise RegistryError(
                f"registry schema is unreadable; the database may have been "
                f"replaced or removed: {self.path}"
            ) from error
        if row is None:
            raise RegistryError("registry schema version is missing")
        if row[0] != SCHEMA_VERSION:
            raise RegistryError(
                f"registry schema changed to {row[0]} while in use; "
                f"this build supports {SCHEMA_VERSION}"
            )

    def _ensure_schema(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                schema_exists = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_meta'"
                ).fetchone()
                if schema_exists is None:
                    self._execute_schema(connection)
                    connection.execute(
                        "INSERT INTO schema_meta(version) VALUES (?)", (SCHEMA_VERSION,)
                    )
                    connection.commit()
                    return
                versions = connection.execute("SELECT version FROM schema_meta").fetchall()
                if not versions:
                    raise RegistryError("registry schema version is missing")
                if len(versions) != 1:
                    raise RegistryError("registry schema version must contain exactly one row")
                current = versions[0]["version"]
                if current > SCHEMA_VERSION:
                    raise RegistryError(
                        f"registry schema {current} is newer than supported {SCHEMA_VERSION}"
                    )
                migrations = {
                    1: self._migrate_v1_to_v2,
                    2: self._migrate_v2_to_v3,
                    3: self._migrate_v3_to_v4,
                    4: self._migrate_v4_to_v5,
                    5: self._migrate_v5_to_v6,
                }
                while current < SCHEMA_VERSION:
                    migration = migrations.get(current)
                    if migration is None:
                        raise RegistryError(f"no registry migration from schema {current}")
                    target = current + 1
                    migration(connection)
                    connection.execute("UPDATE schema_meta SET version = ?", (target,))
                    current = target
                connection.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_schema_meta_singleton "
                    "ON schema_meta((1))"
                )
                connection.commit()
            except sqlite3.Error as error:
                connection.rollback()
                if "current" in locals() and current < SCHEMA_VERSION:
                    raise RegistryError(
                        f"registry migration {current} -> {current + 1} failed: {error}"
                    ) from error
                raise
            except Exception:
                connection.rollback()
                raise

    @staticmethod
    def _execute_schema(connection: sqlite3.Connection) -> None:
        for statement in SCHEMA.split(";"):
            if statement.strip():
                connection.execute(statement)

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

    @staticmethod
    def _migrate_v3_to_v4(connection: sqlite3.Connection) -> None:
        connection.execute("ALTER TABLE run_turns ADD COLUMN prompt TEXT NOT NULL DEFAULT ''")

    @staticmethod
    def _migrate_v5_to_v6(connection: sqlite3.Connection) -> None:
        """Split the mixed `run_turns.status` column into status plus outcome.

        Before v6 a turn's single `status` column held both its lifecycle
        position (`running`) and its terminal result, while `runs` already
        modelled those as separate `status`/`outcome` columns. SQLite cannot
        narrow a CHECK constraint in place, so the table is rebuilt.
        """
        connection.execute("DROP INDEX IF EXISTS idx_one_open_turn_per_run")
        connection.execute("DROP INDEX IF EXISTS idx_run_turns_run_ordinal")
        connection.execute("ALTER TABLE run_turns RENAME TO run_turns_v5")
        connection.execute(
            """
            CREATE TABLE run_turns (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                ordinal INTEGER NOT NULL,
                purpose TEXT NOT NULL CHECK (purpose IN (
                    'task', 'correction', 'clarification', 'review_follow_up'
                )),
                prompt TEXT NOT NULL,
                prompt_digest TEXT NOT NULL,
                artifact_path TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('running', 'finished')),
                outcome TEXT CHECK (outcome IS NULL OR outcome IN (
                    'blocked', 'succeeded', 'failed', 'unknown'
                )),
                lifecycle_evidence TEXT,
                summary TEXT,
                result_json TEXT,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                UNIQUE (run_id, ordinal),
                UNIQUE (run_id, artifact_path)
            )
            """
        )
        connection.execute(
            """
            INSERT INTO run_turns(
                id, run_id, ordinal, purpose, prompt, prompt_digest, artifact_path,
                status, outcome, lifecycle_evidence, summary, result_json,
                started_at, finished_at
            )
            SELECT
                id, run_id, ordinal, purpose, prompt, prompt_digest, artifact_path,
                CASE WHEN status = 'running' THEN 'running' ELSE 'finished' END,
                CASE WHEN status = 'running' THEN NULL ELSE status END,
                lifecycle_evidence, summary, result_json, started_at, finished_at
            FROM run_turns_v5
            """
        )
        connection.execute("DROP TABLE run_turns_v5")
        connection.execute("CREATE INDEX idx_run_turns_run_ordinal ON run_turns(run_id, ordinal)")
        connection.execute(
            "CREATE UNIQUE INDEX idx_one_open_turn_per_run "
            "ON run_turns(run_id) WHERE status = 'running'"
        )
        # `kind` named the agent product on herdr_bindings and the feedback
        # category here; qualify it so the token means one thing.
        connection.execute("ALTER TABLE feedback RENAME COLUMN kind TO category")

    @staticmethod
    def _migrate_v4_to_v5(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS maintenance_runs (
                id TEXT PRIMARY KEY,
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('succeeded', 'failed')),
                summary_json TEXT NOT NULL,
                error_message TEXT
            )
            """
        )

    @contextmanager
    def _transaction(self) -> Iterable[sqlite3.Connection]:
        self.initialize()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_schema_current(connection)
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def _read_transaction(self) -> Iterable[sqlite3.Connection]:
        self.initialize()
        connection = self._connect()
        transaction_started = False
        try:
            connection.execute("BEGIN DEFERRED")
            transaction_started = True
            self._assert_schema_current(connection)
            yield connection
            connection.commit()
            transaction_started = False
        except Exception as error:
            if transaction_started:
                try:
                    connection.rollback()
                except Exception as rollback_error:
                    error.add_note(f"read transaction rollback failed: {rollback_error}")
            raise
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=SQLITE_BUSY_TIMEOUT_MS / 1_000,
            isolation_level=None,
        )
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
            deadline = time.perf_counter() + (SQLITE_BUSY_TIMEOUT_MS / 1_000)
            while True:
                try:
                    connection.execute("PRAGMA journal_mode = WAL")
                    break
                except sqlite3.OperationalError as error:
                    if "locked" not in str(error).lower() or time.perf_counter() >= deadline:
                        raise
                    time.sleep(0.005)
        except Exception:
            connection.close()
            raise
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
        with self._read_transaction() as connection:
            return self._get_task_impl(connection, task_id)

    def _get_task_impl(self, connection: sqlite3.Connection, task_id: str) -> dict[str, Any]:
        task = _row(connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone())
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
        task["runs"] = [self._get_run_impl(connection, run_id) for run_id in run_ids]
        return task

    def list_tasks(self, states: Iterable[str] | None = None) -> list[dict[str, Any]]:
        with self._read_transaction() as connection:
            return self._list_tasks_impl(connection, states)

    def _list_tasks_impl(
        self, connection: sqlite3.Connection, states: Iterable[str] | None = None
    ) -> list[dict[str, Any]]:
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
            if to_state == "ready" and blocked_on is None:
                new_blocked_on = None
            else:
                new_blocked_on = blocked_on if blocked_on is not None else task["blocked_on"]
            new_next_action = next_action if next_action is not None else task["next_action"]
            changed = connection.execute(
                """
                UPDATE tasks
                SET state = ?, next_action = ?, blocked_on = ?, updated_at = ?
                WHERE id = ? AND state = ?
                """,
                (to_state, new_next_action, new_blocked_on, _now(), task_id, from_state),
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
        with self._read_transaction() as connection:
            return self._get_run_impl(connection, run_id)

    def _get_run_impl(self, connection: sqlite3.Connection, run_id: str) -> dict[str, Any]:
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
        if status == "stale":
            raise RegistryError(
                "cannot manually set binding status to stale; "
                "stale is set automatically when a run finishes"
            )
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
        artifact_path = f".bossmode/turns/{turn_id}.json"
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
                    id, run_id, ordinal, purpose, prompt, prompt_digest, artifact_path,
                    status, started_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?)
                """,
                (
                    turn_id,
                    run_id,
                    ordinal,
                    purpose,
                    prompt,
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
        outcome: str,
        summary: str | None = None,
        lifecycle_evidence: str | None = None,
    ) -> dict[str, Any]:
        if outcome not in TERMINAL_TURN_OUTCOMES:
            raise RegistryError(f"invalid terminal turn outcome: {outcome}")
        if outcome != "succeeded" and (summary is None or not summary.strip()):
            raise RegistryError("turn summary is required")
        with self._transaction() as connection:
            turn = connection.execute("SELECT * FROM run_turns WHERE id = ?", (turn_id,)).fetchone()
            if turn is None:
                raise RegistryError(f"turn not found: {turn_id}")
            if turn["status"] != "running":
                raise RegistryError(f"turn already finished: {turn_id}")
            result = None
            if outcome == "succeeded":
                result = self._validated_turn_result(dict(turn), expected_summary=summary)
                summary = result["summary"]
            connection.execute(
                """
                UPDATE run_turns
                SET status = 'finished', outcome = ?, summary = ?, lifecycle_evidence = ?,
                    result_json = ?, finished_at = ?
                WHERE id = ? AND status = 'running'
                """,
                (
                    outcome,
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
        flags = os.O_RDONLY
        flags |= getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_NONBLOCK", 0)
        descriptor = -1
        directory_descriptor = -1
        try:
            # Walk parents with O_NOFOLLOW as well as the final component. The
            # opened descriptor is the authority for the read; path metadata
            # is never re-opened after this point.
            absolute_path = Path(os.path.abspath(path))
            directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            directory_flags |= getattr(os, "O_CLOEXEC", 0)
            directory_flags |= getattr(os, "O_NOFOLLOW", 0)
            directory_descriptor = os.open(absolute_path.anchor, directory_flags)
            for component in absolute_path.parts[1:-1]:
                try:
                    next_directory = os.open(
                        component,
                        directory_flags,
                        dir_fd=directory_descriptor,
                    )
                except OSError as error:
                    if error.errno in {errno.ELOOP, errno.ENOTDIR}:
                        try:
                            component_stat = os.stat(
                                component,
                                dir_fd=directory_descriptor,
                                follow_symlinks=False,
                            )
                        except OSError:
                            component_stat = None
                        if component_stat is not None and stat.S_ISLNK(component_stat.st_mode):
                            raise RegistryError(
                                f"turn result artifact parent cannot be a symlink: {path}"
                            ) from error
                    raise
                os.close(directory_descriptor)
                directory_descriptor = next_directory
            descriptor = os.open(
                absolute_path.name,
                flags,
                dir_fd=directory_descriptor,
            )
            descriptor_stat = os.fstat(descriptor)
            if not stat.S_ISREG(descriptor_stat.st_mode):
                raise RegistryError(f"turn result artifact must be a regular file: {path}")
            result_file = os.fdopen(descriptor, "rb")
            descriptor = -1
            with result_file:
                raw = result_file.read(MAX_TURN_RESULT_BYTES + 1)
        except OSError as error:
            if error.errno == errno.ELOOP:
                raise RegistryError(f"turn result artifact cannot be a symlink: {path}") from error
            raise RegistryError(
                f"turn result is unavailable at {path}: {error.strerror}"
            ) from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if directory_descriptor >= 0:
                os.close(directory_descriptor)
        if len(raw) > MAX_TURN_RESULT_BYTES:
            raise RegistryError(f"turn result exceeds {MAX_TURN_RESULT_BYTES} bytes: {path}")
        try:
            result = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            stripped = raw.lstrip()
            if stripped.startswith(b"```"):
                raise RegistryError(
                    f"turn result contains markdown code fence instead of raw JSON: {path}"
                ) from error
            raise RegistryError(f"turn result is not valid JSON: {path}") from error
        if not isinstance(result, dict):
            raise RegistryError("turn result must be a JSON object")
        required = {"turn_id", "outcome", "summary", "artifacts"}
        missing = sorted(required - result.keys())
        if missing:
            raise RegistryError(f"turn result is missing fields: {', '.join(missing)}")
        if result["turn_id"] != turn["id"]:
            raise RegistryError("turn result ID does not match the open turn")
        if result["outcome"] != "succeeded":
            raise RegistryError("successful turn result must have outcome succeeded")
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
            if outcome == "succeeded":
                turns = connection.execute(
                    "SELECT outcome FROM run_turns WHERE run_id = ?", (run_id,)
                ).fetchall()
                herdr_binding = connection.execute(
                    "SELECT 1 FROM herdr_bindings WHERE run_id = ?", (run_id,)
                ).fetchone()
                if (herdr_binding is not None or turns) and (
                    not turns or not any(turn["outcome"] == "succeeded" for turn in turns)
                ):
                    raise RegistryError(
                        "successful run with Herdr worker or turns requires at least "
                        "one succeeded turn"
                    )
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
        run_id: str,
        evaluator: str,
        passed: bool,
        evidence: str,
        score: float | None = None,
        notes: str | None = None,
    ) -> dict[str, Any]:
        if not run_id:
            raise RegistryError("evaluation requires a run_id")
        if score is not None and not 0 <= score <= 1:
            raise RegistryError("evaluation score must be between 0 and 1")
        if not evidence.strip():
            raise RegistryError("evaluation evidence is required")
        evaluation_id = _id("eval")
        with self._transaction() as connection:
            task = connection.execute("SELECT state FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if task is None:
                raise RegistryError(f"task not found: {task_id}")
            if task["state"] != "evaluating":
                raise RegistryError(
                    "task must be in evaluating state to record an evaluation; "
                    f"found {task['state']}"
                )
            run = connection.execute(
                "SELECT task_id, agent_role, status, outcome FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
            if run is None:
                raise RegistryError(f"run not found: {run_id}")
            if run["task_id"] != task_id:
                raise RegistryError("evaluation run does not belong to task")
            if run["status"] != "finished":
                raise RegistryError(f"evaluation requires a finished run; found {run['status']}")
            latest_eval_run = connection.execute(
                """
                SELECT id FROM runs
                WHERE task_id = ? AND outcome = 'succeeded'
                ORDER BY finished_at DESC LIMIT 1
                """,
                (task_id,),
            ).fetchone()
            if latest_eval_run is not None and latest_eval_run["id"] != run_id:
                raise RegistryError(
                    f"evaluation must target the evaluating run: {latest_eval_run['id']}"
                )
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
        category: str,
        recurrence_key: str,
        content: str,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        if category not in FEEDBACK_CATEGORIES:
            raise RegistryError(f"invalid feedback category: {category}")
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
                    id, task_id, run_id, category, recurrence_key, content, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (feedback_id, task_id, run_id, category, recurrence_key, content, _now()),
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
                    """
                    SELECT id, status, evidence_json FROM promotions
                    WHERE recurrence_key = ? AND target_layer = ?
                    """,
                    (key, target),
                ).fetchone()
                if existing is not None and existing["status"] in (
                    "proposed",
                    "accepted",
                    "applied",
                ):
                    continue
                relevant_feedback = [
                    item
                    for item in feedback
                    if self._is_relevant_feedback(target, item["category"])
                ]
                task_ids = sorted({item["task_id"] for item in relevant_feedback})
                if not task_ids:
                    continue
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
                timestamp = _now()
                evidence = {
                    "feedback_ids": [item["id"] for item in relevant_feedback],
                    "evaluation_ids": [item["id"] for item in evaluations],
                    "task_ids": task_ids,
                }
                rationale = self._promotion_rationale(target, relevant_feedback, evaluations)
                if existing is not None and existing["status"] == "rejected":
                    existing_evidence = json.loads(existing["evidence_json"])
                    existing_feedback_ids = set(existing_evidence.get("feedback_ids", []))
                    existing_eval_ids = set(existing_evidence.get("evaluation_ids", []))
                    current_feedback_ids = set(evidence["feedback_ids"])
                    current_eval_ids = set(evidence["evaluation_ids"])
                    if not (
                        (current_feedback_ids - existing_feedback_ids)
                        or (current_eval_ids - existing_eval_ids)
                    ):
                        continue
                    promotion_id = existing["id"]
                    connection.execute(
                        """
                        UPDATE promotions
                        SET status = 'proposed', rationale = ?, evidence_json = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (rationale, _json(evidence), timestamp, promotion_id),
                    )
                else:
                    promotion_id = _id("promotion")
                    connection.execute(
                        """
                        INSERT INTO promotions(
                            id, recurrence_key, target_layer, status, rationale,
                            evidence_json, created_at, updated_at
                        ) VALUES (?, ?, ?, 'proposed', ?, ?, ?, ?)
                        """,
                        (
                            promotion_id,
                            key,
                            target,
                            rationale,
                            _json(evidence),
                            timestamp,
                            timestamp,
                        ),
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
        with self._read_transaction() as connection:
            return self._list_promotions_impl(connection, status)

    def _list_promotions_impl(
        self, connection: sqlite3.Connection, status: str | None = None
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM promotions"
        parameters: list[Any] = []
        if status is not None:
            query += " WHERE status = ?"
            parameters.append(status)
        query += " ORDER BY created_at, id"
        rows = [dict(row) for row in connection.execute(query, parameters)]
        for promotion in rows:
            promotion["evidence"] = json.loads(promotion.pop("evidence_json"))
        return rows

    def _set_promotion_status(self, promotion_id: str, status: str) -> dict[str, Any]:
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

    def accept_promotion(self, promotion_id: str) -> dict[str, Any]:
        return self._set_promotion_status(promotion_id, "accepted")

    def reject_promotion(self, promotion_id: str) -> dict[str, Any]:
        return self._set_promotion_status(promotion_id, "rejected")

    def apply_promotion(self, promotion_id: str) -> dict[str, Any]:
        return self._set_promotion_status(promotion_id, "applied")

    def reconcile(self) -> dict[str, Any]:
        """Converge the registry and report the current control-plane state.

        This is a read-shaped command with real writes: it creates or migrates
        the schema through `initialize()` and materialises promotion proposals
        before reporting. Each task bucket is keyed by the task state it holds.
        """
        self.initialize()
        created = self.propose_promotions()
        with self._read_transaction() as connection:
            ready = self._list_tasks_impl(connection, ["ready"])
            waiting_user = self._list_tasks_impl(connection, ["waiting_user"])
            blocked = self._list_tasks_impl(connection, ["blocked"])
            running = [
                self._get_task_impl(connection, task["id"])
                for task in self._list_tasks_impl(connection, ["running"])
            ]
            evaluating = [
                self._get_task_impl(connection, task["id"])
                for task in self._list_tasks_impl(connection, ["evaluating"])
            ]
            return {
                "next_task": ready[0] if ready and not running and not evaluating else None,
                "running": running,
                "evaluating": evaluating,
                "waiting_user": waiting_user,
                "blocked": blocked,
                "new_promotion_proposals": created,
                "promotion_proposals": self._list_promotions_impl(connection, "proposed"),
            }

    def run_maintenance(self) -> dict[str, Any]:
        self.initialize()
        start_time = time.perf_counter()
        started_at = _now()
        maint_id = _id("maint")

        try:
            report = self._collect_maintenance_report(
                maintenance_id=maint_id,
                started_at=started_at,
                start_time=start_time,
            )
            self._record_maintenance_run(
                maint_id,
                started_at=started_at,
                status="succeeded",
                summary=report,
            )
        except Exception as error:
            try:
                self._record_maintenance_run(
                    maint_id,
                    started_at=started_at,
                    status="failed",
                    summary={},
                    error_message=str(error),
                )
            except Exception as record_error:
                error.add_note(f"could not record failed maintenance run: {record_error}")
            if isinstance(error, RegistryError):
                raise
            raise RegistryError(f"maintenance failed: {error}") from error
        return report

    def _collect_maintenance_report(
        self,
        *,
        maintenance_id: str,
        started_at: str,
        start_time: float,
    ) -> dict[str, Any]:

        with self._read_transaction() as connection:
            integrity_row = connection.execute("PRAGMA integrity_check").fetchone()
            integrity = integrity_row[0] if integrity_row else "unknown"

            page_count_row = connection.execute("PRAGMA page_count").fetchone()
            page_count = page_count_row[0] if page_count_row else 0
            page_size_row = connection.execute("PRAGMA page_size").fetchone()
            page_size = page_size_row[0] if page_size_row else 4096
            db_size_bytes = page_count * page_size

            journal_mode_row = connection.execute("PRAGMA journal_mode").fetchone()
            journal_mode = journal_mode_row[0] if journal_mode_row else "unknown"

            active_runs = connection.execute(
                "SELECT COUNT(*) FROM runs WHERE status = 'running'"
            ).fetchone()[0]
            stale_bindings = connection.execute(
                "SELECT COUNT(*) FROM herdr_bindings WHERE status = 'stale'"
            ).fetchone()[0]
            live_bindings = connection.execute(
                "SELECT COUNT(*) FROM herdr_bindings WHERE status = 'live'"
            ).fetchone()[0]
            orphaned_turns = connection.execute(
                """
                SELECT COUNT(*) FROM run_turns t
                JOIN runs r ON t.run_id = r.id
                WHERE t.status = 'running' AND r.status = 'finished'
                """
            ).fetchone()[0]
            unresolved_evals = connection.execute(
                "SELECT COUNT(*) FROM tasks WHERE state = 'evaluating'"
            ).fetchone()[0]

            telemetry_rows = connection.execute(
                """
                SELECT
                    COALESCE(model, 'unspecified') AS model,
                    COALESCE(reasoning_effort, 'none') AS reasoning_effort,
                    COUNT(*) AS total_runs,
                    COUNT(
                        CASE WHEN tokens IS NOT NULL AND tokens > 0 THEN 1 END
                    ) AS runs_with_tokens,
                    ROUND(
                        AVG(CASE WHEN tokens IS NOT NULL AND tokens > 0 THEN tokens END), 0
                    ) AS avg_tokens,
                    ROUND(AVG(COALESCE(duration_seconds, 0)), 1) AS avg_duration_sec,
                    SUM(retries) AS total_retries,
                    ROUND(
                        AVG(CASE WHEN outcome = 'succeeded' THEN 1.0 ELSE 0.0 END) * 100, 1
                    ) AS success_rate_pct
                FROM runs
                WHERE status = 'finished'
                GROUP BY model, reasoning_effort
                ORDER BY total_runs DESC
                """
            ).fetchall()

            telemetry = [
                {
                    "model": row["model"],
                    "reasoning_effort": row["reasoning_effort"],
                    "total_runs": row["total_runs"],
                    "runs_with_tokens": row["runs_with_tokens"],
                    "avg_tokens": row["avg_tokens"],
                    "avg_duration_sec": row["avg_duration_sec"],
                    "total_retries": row["total_retries"],
                    "success_rate_pct": row["success_rate_pct"],
                }
                for row in telemetry_rows
            ]

        new_proposals = self.propose_promotions()
        pending_promotions = self.list_promotions("proposed")

        health_status = "healthy" if integrity == "ok" and orphaned_turns == 0 else "warning"
        duration_seconds = round(time.perf_counter() - start_time, 4)

        report = {
            "id": maintenance_id,
            "timestamp": started_at,
            "duration_seconds": duration_seconds,
            "database": {
                "path": str(self.path),
                "integrity": integrity,
                "size_bytes": db_size_bytes,
                "journal_mode": journal_mode,
            },
            "health": {
                "active_runs": active_runs,
                "stale_herdr_bindings": stale_bindings,
                "live_herdr_bindings": live_bindings,
                "orphaned_turns": orphaned_turns,
                "unresolved_evaluations": unresolved_evals,
                "status": health_status,
            },
            "telemetry": telemetry,
            "promotions": {
                "new_proposals": new_proposals,
                "new_proposals_count": len(new_proposals),
                "pending_approval_count": len(pending_promotions),
            },
        }

        return report

    def _record_maintenance_run(
        self,
        maintenance_id: str,
        *,
        started_at: str,
        status: str,
        summary: dict[str, Any],
        error_message: str | None = None,
    ) -> None:
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO maintenance_runs(
                    id, started_at, finished_at, status, summary_json, error_message
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    maintenance_id,
                    started_at,
                    _now(),
                    status,
                    _json(summary),
                    error_message,
                ),
            )

    def list_maintenance_runs(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._read_transaction() as connection:
            rows = connection.execute(
                """
                SELECT id, started_at, finished_at, status, summary_json, error_message
                FROM maintenance_runs
                ORDER BY started_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        result = []
        for r in rows:
            item = dict(r)
            item["summary"] = json.loads(item.pop("summary_json"))
            result.append(item)
        return result

    @staticmethod
    def _is_relevant_feedback(target: str, category: str) -> bool:
        if target == "control":
            return category == "failure"
        if target == "skill":
            return category == "correction"
        return category in ("preference", "observation")

    @staticmethod
    def _promotion_target(feedback: list[dict[str, Any]]) -> str | None:
        categories = [item["category"] for item in feedback]
        if categories.count("failure") >= 2:
            return "control"
        if categories.count("correction") >= 2:
            return "skill"
        if "preference" in categories or "observation" in categories:
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
        with self._read_transaction() as connection:
            record = _row(
                connection.execute(f"SELECT * FROM {table} WHERE id = ?", (record_id,)).fetchone()
            )
        if record is None:
            raise RegistryError(f"record not found: {record_id}")
        return record

    def _get_promotion(self, promotion_id: str) -> dict[str, Any]:
        with self._read_transaction() as connection:
            promotion = _row(
                connection.execute(
                    "SELECT * FROM promotions WHERE id = ?", (promotion_id,)
                ).fetchone()
            )
        if promotion is None:
            raise RegistryError(f"promotion not found: {promotion_id}")
        promotion["evidence"] = json.loads(promotion.pop("evidence_json"))
        return promotion
