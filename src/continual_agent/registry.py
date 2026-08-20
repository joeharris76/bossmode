from __future__ import annotations

import json
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

TERMINAL_RUN_OUTCOMES = {"waiting_user", "blocked", "succeeded", "failed"}

ALLOWED_TRANSITIONS = {
    "backlog": {"ready", "archived"},
    "ready": {"running", "blocked", "archived"},
    "running": {"waiting_user", "blocked", "evaluating", "failed"},
    "evaluating": {"ready", "blocked", "archived"},
    "waiting_user": {"ready", "running", "blocked", "archived"},
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
            connection.executescript(SCHEMA)
            version = connection.execute("SELECT version FROM schema_meta LIMIT 1").fetchone()
            if version is None:
                connection.execute("INSERT INTO schema_meta(version) VALUES (1)")

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
        if state not in TASK_STATES:
            raise RegistryError(f"unknown task state: {state}")
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
            task["runs"] = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM runs WHERE task_id = ? ORDER BY started_at", (task_id,)
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
        if run is None:
            raise RegistryError(f"run not found: {run_id}")
        run["artifacts"] = json.loads(run.pop("artifacts_json"))
        return run

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
                    "SELECT task_id FROM runs WHERE id = ?", (run_id,)
                ).fetchone()
                if run is None:
                    raise RegistryError(f"run not found: {run_id}")
                if run["task_id"] != task_id:
                    raise RegistryError("evaluation run does not belong to task")
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
        active = self.list_tasks(["running"])
        needs_evaluation = self.list_tasks(["evaluating"])
        return {
            "dispatch": ready[0] if ready else None,
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
        if table not in {"evaluations", "feedback"}:
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
