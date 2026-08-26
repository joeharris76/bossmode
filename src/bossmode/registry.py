from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import sqlite3
import stat
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable, Iterable
from contextlib import closing, contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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
SCHEMA_VERSION = 8
MIGRATION_LEDGER_VERSION = 7
REGISTRY_ROLES = {"operational", "ephemeral"}
MACOS_SYSTEM_PATH_ALIASES = {
    Path("/etc"): Path("/private/etc"),
    Path("/tmp"): Path("/private/tmp"),
    Path("/var"): Path("/private/var"),
}

MIGRATION_V6_TO_V7_ID = "20260825_01_migration_durability"
MIGRATION_V6_TO_V7_SQL = """
CREATE TABLE applied_migrations (
    migration_id TEXT PRIMARY KEY,
    from_version INTEGER NOT NULL,
    to_version INTEGER NOT NULL,
    checksum TEXT NOT NULL,
    applied_at TEXT NOT NULL,
    backup_path TEXT,
    backup_sha256 TEXT,
    CHECK (
        (backup_path IS NULL AND backup_sha256 IS NULL)
        OR (backup_path IS NOT NULL AND backup_sha256 IS NOT NULL)
    ),
    CHECK (to_version = from_version + 1)
);
CREATE UNIQUE INDEX idx_applied_migrations_transition
    ON applied_migrations(from_version, to_version);
"""

MIGRATION_V7_TO_V8_ID = "20260825_02_registry_identity"
REGISTRY_IDENTITY_TABLE_SQL = """
CREATE TABLE registry_identity (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    registry_id TEXT NOT NULL UNIQUE,
    registry_role TEXT NOT NULL CHECK (registry_role IN ('operational', 'ephemeral')),
    repository_url TEXT NOT NULL,
    git_common_dir TEXT NOT NULL,
    primary_checkout TEXT NOT NULL,
    created_at TEXT NOT NULL,
    creation_metadata_json TEXT NOT NULL
)
"""
REGISTRY_IDENTITY_GUARD_SQL = (
    """
    CREATE TRIGGER registry_identity_immutable_update
    BEFORE UPDATE ON registry_identity
    BEGIN
        SELECT RAISE(ABORT, 'registry identity is immutable');
    END
    """,
    """
    CREATE TRIGGER registry_identity_immutable_delete
    BEFORE DELETE ON registry_identity
    BEGIN
        SELECT RAISE(ABORT, 'registry identity is immutable');
    END
    """,
)
REGISTRY_IDENTITY_INSERT_CONTRACT = """
INSERT INTO registry_identity(
    singleton, registry_id, registry_role, repository_url,
    git_common_dir, primary_checkout, created_at, creation_metadata_json
) VALUES (:singleton, :registry_id, :registry_role, :repository_url,
          :git_common_dir, :primary_checkout, :created_at, :creation_metadata_json)
"""
MIGRATION_V7_TO_V8_SQL = "\n".join(
    (
        REGISTRY_IDENTITY_TABLE_SQL,
        *REGISTRY_IDENTITY_GUARD_SQL,
        REGISTRY_IDENTITY_INSERT_CONTRACT,
    )
)

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

CREATE TABLE IF NOT EXISTS applied_migrations (
    migration_id TEXT PRIMARY KEY,
    from_version INTEGER NOT NULL,
    to_version INTEGER NOT NULL,
    checksum TEXT NOT NULL,
    applied_at TEXT NOT NULL,
    backup_path TEXT,
    backup_sha256 TEXT,
    CHECK (
        (backup_path IS NULL AND backup_sha256 IS NULL)
        OR (backup_path IS NOT NULL AND backup_sha256 IS NOT NULL)
    ),
    CHECK (to_version = from_version + 1)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_applied_migrations_transition
    ON applied_migrations(from_version, to_version);

CREATE TABLE IF NOT EXISTS registry_identity (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    registry_id TEXT NOT NULL UNIQUE,
    registry_role TEXT NOT NULL CHECK (registry_role IN ('operational', 'ephemeral')),
    repository_url TEXT NOT NULL,
    git_common_dir TEXT NOT NULL,
    primary_checkout TEXT NOT NULL,
    created_at TEXT NOT NULL,
    creation_metadata_json TEXT NOT NULL
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


@dataclass(frozen=True)
class MigrationStep:
    migration_id: str | None
    from_version: int
    to_version: int
    checksum: str | None
    apply: Callable[[sqlite3.Connection], None]


@dataclass(frozen=True)
class MigrationBackup:
    path: Path
    sha256: str


@dataclass(frozen=True)
class RepositoryIdentity:
    repository_url: str
    git_common_dir: Path
    primary_checkout: Path
    current_checkout: Path


@dataclass(frozen=True)
class DatabaseFileIdentity:
    device: int
    inode: int


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _migration_checksum(migration_id: str, from_version: int, to_version: int, payload: str) -> str:
    canonical = _json(
        {
            "from_version": from_version,
            "migration_id": migration_id,
            "payload": payload.strip(),
            "to_version": to_version,
        }
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _absolute_path(path: Path) -> Path:
    """Return an absolute lexical path without following symlinks."""
    return Path(os.path.abspath(path.expanduser()))


def _run_git(path: Path, *arguments: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(path), *arguments],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as error:
        raise RegistryError(f"cannot inspect repository identity: {error}") from error
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise RegistryError(f"cannot inspect repository identity: {detail}")
    return result.stdout.strip()


def _discover_repository_identity(path: Path) -> RepositoryIdentity:
    current_checkout = Path(_run_git(path, "rev-parse", "--show-toplevel")).resolve()
    common_output = _run_git(
        current_checkout,
        "rev-parse",
        "--path-format=absolute",
        "--git-common-dir",
    )
    git_common_dir = Path(common_output).resolve()
    if git_common_dir.name != ".git":
        raise RegistryError(f"unsupported Git common directory: {git_common_dir}")
    primary_checkout = git_common_dir.parent.resolve()
    primary_git = primary_checkout / ".git"
    try:
        primary_git_metadata = os.lstat(primary_git)
    except OSError as error:
        raise RegistryError(f"cannot inspect primary Git directory: {primary_git}") from error
    if not stat.S_ISDIR(primary_git_metadata.st_mode) or primary_git.resolve() != git_common_dir:
        raise RegistryError(
            f"cannot identify one primary checkout from the Git common directory: {git_common_dir}"
        )
    repository_url = _run_git(primary_checkout, "config", "--get", "remote.origin.url")
    if not repository_url:
        raise RegistryError("primary checkout has no remote.origin.url")
    return RepositoryIdentity(
        repository_url=repository_url,
        git_common_dir=git_common_dir,
        primary_checkout=primary_checkout,
        current_checkout=current_checkout,
    )


def _try_discover_repository_identity(path: Path) -> RepositoryIdentity | None:
    try:
        return _discover_repository_identity(path)
    except RegistryError as error:
        try:
            probe = subprocess.run(
                ["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"],
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError:
            raise error from None
        if probe.returncode == 0 and probe.stdout.strip() == "true":
            raise error
        return None


def _reject_symlink_path(path: Path) -> None:
    """Reject a database or existing parent component that is a symlink."""
    absolute = _absolute_path(path)
    candidates = [absolute, *absolute.parents]
    for candidate in candidates:
        try:
            metadata = os.lstat(candidate)
        except FileNotFoundError:
            continue
        except OSError as error:
            raise RegistryError(f"cannot inspect registry path component: {candidate}") from error
        if stat.S_ISLNK(metadata.st_mode):
            allowed_target = MACOS_SYSTEM_PATH_ALIASES.get(candidate)
            if allowed_target is not None and candidate.resolve() == allowed_target:
                continue
            raise RegistryError(f"registry path must not contain symlinks: {candidate}")


def validate_nonsymlink_path(path: str | Path) -> Path:
    """Return an absolute lexical path after rejecting symlink components."""
    absolute = _absolute_path(Path(path))
    _reject_symlink_path(absolute)
    return absolute


def _normalized_schema_sql(value: str) -> str:
    return " ".join(value.strip().rstrip(";").split())


class Registry:
    def __init__(
        self,
        path: str | Path,
        *,
        registry_role: str = "ephemeral",
        repository_identity: RepositoryIdentity | None = None,
        database_file_identity: DatabaseFileIdentity | None = None,
        allow_create: bool = True,
    ) -> None:
        if registry_role not in REGISTRY_ROLES:
            raise RegistryError(f"unsupported registry role: {registry_role}")
        self.path = _absolute_path(Path(path))
        self.registry_role = registry_role
        self.repository_identity = repository_identity
        self._database_file_identity = database_file_identity
        self.allow_create = allow_create
        self._schema_ready = False

    @classmethod
    def create_operational(
        cls,
        path: str | Path,
        *,
        repository_path: str | Path = ".",
    ) -> Registry:
        repository = _discover_repository_identity(Path(repository_path))
        registry = cls(
            path,
            registry_role="operational",
            repository_identity=repository,
            allow_create=True,
        )
        registry.initialize()
        return registry

    @classmethod
    def open_for_command(
        cls,
        path: str | Path,
        *,
        explicit_path: bool,
        repository_path: str | Path = ".",
    ) -> Registry:
        database = _absolute_path(Path(path))
        _reject_symlink_path(database)
        repository = _try_discover_repository_identity(Path(repository_path))
        raw_identity, database_file_identity = cls._read_registry_identity(database)
        if raw_identity is not None and raw_identity["registry_role"] == "operational":
            recorded_primary = Path(raw_identity["primary_checkout"])
            recorded_repository = _discover_repository_identity(recorded_primary)
            registry = cls(
                database,
                registry_role="operational",
                repository_identity=recorded_repository,
                database_file_identity=database_file_identity,
                allow_create=False,
            )
            registry._validate_operational_identity(raw_identity, caller_repository=repository)
            return registry

        if repository is not None:
            primary_database = repository.primary_checkout / ".bossmode" / "control.db"
            current_database = repository.current_checkout / ".bossmode" / "control.db"
            if database in {primary_database, current_database}:
                registry = cls(
                    database,
                    registry_role="operational",
                    repository_identity=repository,
                    database_file_identity=database_file_identity,
                    allow_create=False,
                )
                registry._validate_operational_location()
                return registry

        if raw_identity is not None and raw_identity["registry_role"] != "ephemeral":
            raise RegistryError(f"unsupported registry role: {raw_identity['registry_role']}")
        if not explicit_path and repository is not None:
            raise RegistryError("operational registry path is not authoritative")
        return cls(
            database,
            registry_role="ephemeral",
            database_file_identity=database_file_identity,
            allow_create=True,
        )

    @staticmethod
    def _assert_database_descriptor_matches_path(path: Path, descriptor: int) -> None:
        _reject_symlink_path(path)
        opened = os.fstat(descriptor)
        try:
            observed = os.stat(path, follow_symlinks=False)
        except OSError as error:
            raise RegistryError(f"registry database changed while in use: {path}") from error
        if not stat.S_ISREG(opened.st_mode) or not stat.S_ISREG(observed.st_mode):
            raise RegistryError(f"registry database must be a regular file: {path}")
        if (opened.st_dev, opened.st_ino) != (observed.st_dev, observed.st_ino):
            raise RegistryError(f"registry database changed while in use: {path}")

    @classmethod
    def _open_database_descriptor(
        cls,
        path: Path,
        expected_file_identity: DatabaseFileIdentity | None = None,
    ) -> int:
        _reject_symlink_path(path)
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as error:
            raise RegistryError(f"cannot open registry database safely: {path}") from error
        try:
            cls._assert_database_descriptor_matches_path(path, descriptor)
            metadata = os.fstat(descriptor)
            observed = DatabaseFileIdentity(metadata.st_dev, metadata.st_ino)
            if expected_file_identity is not None and observed != expected_file_identity:
                raise RegistryError(f"registry database changed after authority preflight: {path}")
        except Exception:
            os.close(descriptor)
            raise
        return descriptor

    def _remember_database_file_identity(
        self,
        observed: DatabaseFileIdentity | None,
    ) -> None:
        if observed is None:
            return
        if self._database_file_identity is None:
            self._database_file_identity = observed
        elif observed != self._database_file_identity:
            raise RegistryError(f"registry database changed after authority preflight: {self.path}")

    @contextmanager
    def _database_file_guard(self) -> Iterable[int | None]:
        if self.registry_role != "operational":
            yield None
            return
        descriptor = self._open_database_descriptor(
            self.path,
            self._database_file_identity,
        )
        try:
            metadata = os.fstat(descriptor)
            self._remember_database_file_identity(
                DatabaseFileIdentity(metadata.st_dev, metadata.st_ino)
            )
            yield descriptor
            self._assert_database_descriptor_matches_path(self.path, descriptor)
        finally:
            os.close(descriptor)

    def _assert_database_guard(self, descriptor: int | None) -> None:
        if descriptor is not None:
            self._assert_database_descriptor_matches_path(self.path, descriptor)

    @classmethod
    def _read_registry_identity(
        cls,
        path: Path,
        *,
        expected_file_identity: DatabaseFileIdentity | None = None,
    ) -> tuple[dict[str, Any] | None, DatabaseFileIdentity | None]:
        if not path.exists():
            return None, None
        descriptor = cls._open_database_descriptor(path, expected_file_identity)
        try:
            metadata = os.fstat(descriptor)
            file_identity = DatabaseFileIdentity(metadata.st_dev, metadata.st_ino)
            if metadata.st_size == 0:
                return None, file_identity
            uri = f"{path.as_uri()}?mode=ro&immutable=1"
            deadline = time.perf_counter() + (SQLITE_BUSY_TIMEOUT_MS / 1_000)
            while True:
                try:
                    with closing(sqlite3.connect(uri, uri=True)) as connection:
                        cls._assert_database_descriptor_matches_path(path, descriptor)
                        connection.row_factory = sqlite3.Row
                        table = connection.execute(
                            "SELECT 1 FROM sqlite_master "
                            "WHERE type = 'table' AND name = 'registry_identity'"
                        ).fetchone()
                        if table is None:
                            cls._assert_database_descriptor_matches_path(path, descriptor)
                            return None, file_identity
                        cls._validate_registry_identity_schema(connection)
                        rows = connection.execute("SELECT * FROM registry_identity").fetchall()
                        cls._assert_database_descriptor_matches_path(path, descriptor)
                    break
                except sqlite3.Error as error:
                    transient = "malformed" in str(error).lower()
                    if not transient or time.perf_counter() >= deadline:
                        raise RegistryError(f"registry identity is unreadable: {path}") from error
                    time.sleep(0.005)
        finally:
            os.close(descriptor)
        if len(rows) != 1:
            raise RegistryError("registry identity must contain exactly one row")
        identity = dict(rows[0])
        cls._validate_registry_identity_material(identity)
        return identity, file_identity

    @staticmethod
    def _validate_registry_identity_schema(connection: sqlite3.Connection) -> None:
        expected = {
            ("table", "registry_identity"): _normalized_schema_sql(REGISTRY_IDENTITY_TABLE_SQL),
            **{
                ("trigger", name): _normalized_schema_sql(statement)
                for name, statement in (
                    ("registry_identity_immutable_update", REGISTRY_IDENTITY_GUARD_SQL[0]),
                    ("registry_identity_immutable_delete", REGISTRY_IDENTITY_GUARD_SQL[1]),
                )
            },
        }
        rows = connection.execute(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE tbl_name = 'registry_identity' AND type IN ('table', 'trigger')"
        ).fetchall()
        observed = {(row["type"], row["name"]): _normalized_schema_sql(row["sql"]) for row in rows}
        if observed != expected:
            raise RegistryError("registry identity schema or immutability enforcement is altered")

    @staticmethod
    def _validate_registry_identity_material(identity: dict[str, Any]) -> dict[str, Any]:
        if identity["singleton"] != 1:
            raise RegistryError("registry identity singleton is invalid")
        registry_id = identity["registry_id"]
        if (
            not isinstance(registry_id, str)
            or re.fullmatch(r"registry_[0-9a-f]{12}", registry_id) is None
        ):
            raise RegistryError("registry ID is invalid")
        created_at = identity["created_at"]
        try:
            created = datetime.fromisoformat(created_at)
        except (TypeError, ValueError) as error:
            raise RegistryError("registry creation time is invalid") from error
        if (
            created.tzinfo is None
            or created.utcoffset() != timedelta(0)
            or created.isoformat() != created_at
        ):
            raise RegistryError("registry creation time is invalid")
        expected_metadata = _json({"schema_version": SCHEMA_VERSION})
        if identity["creation_metadata_json"] != expected_metadata:
            raise RegistryError("registry creation metadata does not match the schema contract")
        return {"schema_version": SCHEMA_VERSION}

    def _validate_operational_location(self) -> None:
        repository = self.repository_identity
        if repository is None:
            raise RegistryError("operational registry is missing repository authority")
        if repository.current_checkout != repository.primary_checkout:
            raise RegistryError(
                "operational registry commands must run from the primary checkout: "
                f"current={repository.current_checkout}, primary={repository.primary_checkout}"
            )
        expected = repository.primary_checkout / ".bossmode" / "control.db"
        if self.path != expected:
            raise RegistryError(
                "operational registry must use the primary checkout path: "
                f"expected={expected}, actual={self.path}"
            )
        caller_repository = _try_discover_repository_identity(Path.cwd())
        if caller_repository is not None:
            if caller_repository.git_common_dir != repository.git_common_dir:
                raise RegistryError("caller belongs to a different Git repository")
            if caller_repository.current_checkout != caller_repository.primary_checkout:
                raise RegistryError(
                    "linked worktrees cannot mutate the operational registry: "
                    f"current={caller_repository.current_checkout}, "
                    f"primary={caller_repository.primary_checkout}"
                )

    def _identity_values(self) -> dict[str, str | int]:
        if self.registry_role == "operational":
            repository = self.repository_identity
            if repository is None:
                raise RegistryError("operational registry is missing repository authority")
            repository_url = repository.repository_url
            git_common_dir = str(repository.git_common_dir)
            primary_checkout = str(repository.primary_checkout)
        else:
            repository_url = ""
            git_common_dir = ""
            primary_checkout = ""
        return {
            "singleton": 1,
            "registry_role": self.registry_role,
            "repository_url": repository_url,
            "git_common_dir": git_common_dir,
            "primary_checkout": primary_checkout,
        }

    def _validate_operational_identity(
        self,
        identity: dict[str, Any],
        *,
        caller_repository: RepositoryIdentity | None = None,
    ) -> None:
        if identity["registry_role"] != "operational":
            raise RegistryError(
                "operational commands reject a non-operational registry: "
                f"role={identity['registry_role']}"
            )
        expected = self._identity_values()
        for field in (
            "registry_role",
            "repository_url",
            "git_common_dir",
            "primary_checkout",
        ):
            if identity[field] != expected[field]:
                raise RegistryError(
                    f"operational registry identity mismatch for {field}: "
                    f"expected={expected[field]}, actual={identity[field]}"
                )
        expected_path = Path(identity["primary_checkout"]) / ".bossmode" / "control.db"
        if self.path != expected_path:
            raise RegistryError(
                "operational registry was copied or selected through a non-authoritative path: "
                f"expected={expected_path}, actual={self.path}"
            )
        if caller_repository is not None:
            if caller_repository.git_common_dir != self.repository_identity.git_common_dir:
                raise RegistryError("caller belongs to a different Git repository")
            if caller_repository.current_checkout != caller_repository.primary_checkout:
                raise RegistryError(
                    "linked worktrees cannot mutate the operational registry: "
                    f"current={caller_repository.current_checkout}, "
                    f"primary={caller_repository.primary_checkout}"
                )

    def _assert_live_repository_authority(self) -> None:
        if self.registry_role != "operational":
            return
        expected = self.repository_identity
        if expected is None:
            raise RegistryError("operational registry is missing repository authority")
        observed = _discover_repository_identity(expected.primary_checkout)
        for field in ("repository_url", "git_common_dir", "primary_checkout"):
            if getattr(observed, field) != getattr(expected, field):
                raise RegistryError(
                    f"live repository authority changed for {field}: "
                    f"expected={getattr(expected, field)}, actual={getattr(observed, field)}"
                )
        if observed.current_checkout != observed.primary_checkout:
            raise RegistryError("live repository authority is not the primary checkout")

    def _validate_registry_identity(
        self,
        connection: sqlite3.Connection,
    ) -> dict[str, Any]:
        self._validate_registry_identity_schema(connection)
        try:
            rows = connection.execute("SELECT * FROM registry_identity").fetchall()
        except sqlite3.Error as error:
            raise RegistryError("registry identity is missing or unreadable") from error
        if len(rows) != 1:
            raise RegistryError("registry identity must contain exactly one row")
        identity = dict(rows[0])
        if identity["registry_role"] not in REGISTRY_ROLES:
            raise RegistryError(f"unsupported registry role: {identity['registry_role']}")
        if identity["registry_role"] != self.registry_role:
            raise RegistryError(
                f"registry role mismatch: expected={self.registry_role}, "
                f"actual={identity['registry_role']}"
            )
        creation_metadata = self._validate_registry_identity_material(identity)
        if self.registry_role == "operational":
            self._validate_operational_identity(
                identity,
                caller_repository=_try_discover_repository_identity(Path.cwd()),
            )
        elif any(
            identity[field] for field in ("repository_url", "git_common_dir", "primary_checkout")
        ):
            raise RegistryError("ephemeral registry must not claim repository authority")
        identity["creation_metadata"] = creation_metadata
        return identity

    def _insert_registry_identity(self, connection: sqlite3.Connection) -> None:
        values = self._identity_values()
        connection.execute(
            REGISTRY_IDENTITY_INSERT_CONTRACT,
            {
                **values,
                "registry_id": _id("registry"),
                "created_at": _now(),
                "creation_metadata_json": _json({"schema_version": SCHEMA_VERSION}),
            },
        )

    def _preflight_schema_access(self) -> None:
        _reject_symlink_path(self.path)
        if self.registry_role == "operational":
            self._validate_operational_location()
        elif self.path.name == "control.db" and self.path.parent.name == ".bossmode":
            checkout = self.path.parent.parent
            repository = _try_discover_repository_identity(checkout)
            if repository is not None and checkout.resolve() == repository.current_checkout:
                raise RegistryError(
                    "ephemeral registry access cannot target a Git checkout's operational path; "
                    "use `bossmode registry create` from the primary checkout"
                )
        raw_identity, observed_file_identity = self._read_registry_identity(
            self.path,
            expected_file_identity=self._database_file_identity,
        )
        self._remember_database_file_identity(observed_file_identity)
        if raw_identity is not None:
            if self.registry_role == "operational":
                self._validate_operational_identity(
                    raw_identity,
                    caller_repository=_try_discover_repository_identity(Path.cwd()),
                )
            elif raw_identity["registry_role"] != "ephemeral":
                raise RegistryError(
                    f"ephemeral access rejects an operational registry: {self.path}"
                )
            return
        if self.registry_role == "operational" and not self.allow_create:
            raise RegistryError(
                "operational registry authority is absent; run `bossmode registry create` "
                "from the primary checkout"
            )

    def _prepare_operational_database_file(self) -> None:
        if self.registry_role != "operational" or self._database_file_identity is not None:
            return
        _reject_symlink_path(self.path)
        flags = (
            os.O_RDWR
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            descriptor = os.open(self.path, flags, 0o600)
        except FileExistsError:
            descriptor = self._open_database_descriptor(self.path)
        except OSError as error:
            raise RegistryError(f"cannot create registry database safely: {self.path}") from error
        try:
            os.fchmod(descriptor, 0o600)
            os.fsync(descriptor)
            self._assert_database_descriptor_matches_path(self.path, descriptor)
            metadata = os.fstat(descriptor)
            self._remember_database_file_identity(
                DatabaseFileIdentity(metadata.st_dev, metadata.st_ino)
            )
        finally:
            os.close(descriptor)

    def get_registry_identity(self) -> dict[str, Any]:
        with self._read_transaction() as connection:
            identity = self._validate_registry_identity(connection)
        return identity

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
        self._preflight_schema_access()
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
        self._validate_migration_ledger(connection, SCHEMA_VERSION)
        self._validate_registry_identity(connection)

    def _migration_plan(self) -> dict[int, MigrationStep]:
        ledger_checksum = _migration_checksum(
            MIGRATION_V6_TO_V7_ID,
            6,
            7,
            MIGRATION_V6_TO_V7_SQL,
        )
        identity_checksum = _migration_checksum(
            MIGRATION_V7_TO_V8_ID,
            7,
            8,
            MIGRATION_V7_TO_V8_SQL,
        )
        return {
            1: MigrationStep(None, 1, 2, None, self._migrate_v1_to_v2),
            2: MigrationStep(None, 2, 3, None, self._migrate_v2_to_v3),
            3: MigrationStep(None, 3, 4, None, self._migrate_v3_to_v4),
            4: MigrationStep(None, 4, 5, None, self._migrate_v4_to_v5),
            5: MigrationStep(None, 5, 6, None, self._migrate_v5_to_v6),
            6: MigrationStep(
                MIGRATION_V6_TO_V7_ID,
                6,
                7,
                ledger_checksum,
                self._migrate_v6_to_v7,
            ),
            7: MigrationStep(
                MIGRATION_V7_TO_V8_ID,
                7,
                8,
                identity_checksum,
                self._migrate_v7_to_v8,
            ),
        }

    def _inspect_existing_schema_version(self) -> int | None:
        if not self.path.exists():
            return None
        if self.path.stat().st_size == 0:
            return None
        with self._readonly_connection_context() as connection:
            schema_exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_meta'"
            ).fetchone()
            if schema_exists is None:
                raise RegistryError(
                    "existing nonempty registry is missing schema_meta; refusing initialization"
                )
            versions = connection.execute("SELECT version FROM schema_meta").fetchall()
            if not versions:
                raise RegistryError("registry schema version is missing")
            if len(versions) != 1:
                raise RegistryError("registry schema version must contain exactly one row")
            current = int(versions[0]["version"])
            if current > SCHEMA_VERSION:
                raise RegistryError(
                    f"registry schema {current} is newer than supported {SCHEMA_VERSION}"
                )
            if current >= MIGRATION_LEDGER_VERSION:
                self._validate_migration_ledger(connection, current)
            if current >= 8:
                self._validate_registry_identity(connection)
            return current

    def _validate_migration_path(self, current: int) -> None:
        plan = self._migration_plan()
        probe = current
        while probe < SCHEMA_VERSION:
            step = plan.get(probe)
            if step is None or step.to_version != probe + 1:
                raise RegistryError(f"no registry migration from schema {probe}")
            probe = step.to_version

    @staticmethod
    def _assert_open_directory_matches_path(path: Path, descriptor: int, label: str) -> None:
        opened = os.fstat(descriptor)
        try:
            observed = os.stat(path, follow_symlinks=False)
        except OSError as error:
            raise RegistryError(f"{label} changed while backup was created: {path}") from error
        if not stat.S_ISDIR(observed.st_mode) or (opened.st_dev, opened.st_ino) != (
            observed.st_dev,
            observed.st_ino,
        ):
            raise RegistryError(f"{label} changed while backup was created: {path}")

    def _open_migration_backup_directory(self) -> tuple[Path, int, int]:
        registry_directory = self.path.parent
        backup_directory = registry_directory / "backups"
        directory_flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            registry_descriptor = os.open(registry_directory, directory_flags)
        except OSError as error:
            raise RegistryError(
                f"registry directory must be a real directory: {registry_directory}"
            ) from error
        try:
            created = False
            try:
                os.mkdir("backups", mode=0o700, dir_fd=registry_descriptor)
                created = True
            except FileExistsError:
                pass
            try:
                backup_descriptor = os.open("backups", directory_flags, dir_fd=registry_descriptor)
            except OSError as error:
                raise RegistryError(
                    f"registry backup directory must be a real directory: {backup_directory}"
                ) from error
            try:
                if not stat.S_ISDIR(os.fstat(backup_descriptor).st_mode):
                    raise RegistryError(
                        f"registry backup directory must be a real directory: {backup_directory}"
                    )
                os.fchmod(backup_descriptor, 0o700)
                os.fsync(backup_descriptor)
                if created:
                    os.fsync(registry_descriptor)
                self._assert_open_directory_matches_path(
                    registry_directory, registry_descriptor, "registry directory"
                )
                observed_backup = os.stat(
                    "backups", dir_fd=registry_descriptor, follow_symlinks=False
                )
                opened_backup = os.fstat(backup_descriptor)
                if not stat.S_ISDIR(observed_backup.st_mode) or (
                    opened_backup.st_dev,
                    opened_backup.st_ino,
                ) != (observed_backup.st_dev, observed_backup.st_ino):
                    raise RegistryError(
                        f"registry backup directory changed while backup was created: "
                        f"{backup_directory}"
                    )
            except Exception:
                os.close(backup_descriptor)
                raise
        except Exception:
            os.close(registry_descriptor)
            raise
        return backup_directory, registry_descriptor, backup_descriptor

    def _create_migration_backup(self, schema_version: int) -> MigrationBackup:
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        backup_stem = f"{self.path.name}.schema-{schema_version}.{timestamp}-{uuid.uuid4().hex[:8]}"
        source_uri = f"{self.path.as_uri()}?mode=ro"
        backup_directory, registry_descriptor, backup_descriptor = (
            self._open_migration_backup_directory()
        )
        try:
            with tempfile.TemporaryDirectory(
                prefix="bossmode-migration-backup-"
            ) as staging_directory:
                staging_path = Path(staging_directory) / "backup.sqlite3"
                with (
                    self._database_file_guard() as source_descriptor,
                    closing(sqlite3.connect(source_uri, uri=True)) as source,
                    closing(sqlite3.connect(staging_path)) as destination,
                ):
                    self._assert_database_guard(source_descriptor)
                    source.backup(destination)
                    self._assert_database_guard(source_descriptor)
                    destination.commit()
                os.chmod(staging_path, 0o600)
                self._verify_migration_backup(staging_path, schema_version)
                with staging_path.open("rb") as staging_file:
                    digest = hashlib.file_digest(staging_file, "sha256").hexdigest()

                self._assert_open_directory_matches_path(
                    self.path.parent, registry_descriptor, "registry directory"
                )
                temporary_name = f".{backup_stem}.partial"
                backup_name = f"{backup_stem}.sha256-{digest}.sqlite3"
                temporary_created = False
                backup_published = False
                try:
                    creation_flags = (
                        os.O_RDWR
                        | os.O_CREAT
                        | os.O_EXCL
                        | getattr(os, "O_CLOEXEC", 0)
                        | getattr(os, "O_NOFOLLOW", 0)
                    )
                    destination_descriptor = os.open(
                        temporary_name,
                        creation_flags,
                        0o600,
                        dir_fd=backup_descriptor,
                    )
                    temporary_created = True
                    try:
                        source_descriptor = os.open(
                            staging_path,
                            os.O_RDONLY
                            | getattr(os, "O_CLOEXEC", 0)
                            | getattr(os, "O_NOFOLLOW", 0),
                        )
                        try:
                            while block := os.read(source_descriptor, 1024 * 1024):
                                view = memoryview(block)
                                while view:
                                    written = os.write(destination_descriptor, view)
                                    view = view[written:]
                        finally:
                            os.close(source_descriptor)
                        os.fchmod(destination_descriptor, 0o600)
                        os.fsync(destination_descriptor)
                        os.lseek(destination_descriptor, 0, os.SEEK_SET)
                        with os.fdopen(os.dup(destination_descriptor), "rb") as backup_file:
                            published_digest = hashlib.file_digest(
                                backup_file, "sha256"
                            ).hexdigest()
                        if published_digest != digest:
                            raise RegistryError(
                                "registry migration backup changed during publication"
                            )
                    finally:
                        os.close(destination_descriptor)

                    os.link(
                        temporary_name,
                        backup_name,
                        src_dir_fd=backup_descriptor,
                        dst_dir_fd=backup_descriptor,
                        follow_symlinks=False,
                    )
                    backup_published = True
                    os.unlink(temporary_name, dir_fd=backup_descriptor)
                    temporary_created = False
                    os.fsync(backup_descriptor)
                    self._assert_open_directory_matches_path(
                        self.path.parent, registry_descriptor, "registry directory"
                    )
                    observed_backup = os.stat(
                        "backups", dir_fd=registry_descriptor, follow_symlinks=False
                    )
                    opened_backup = os.fstat(backup_descriptor)
                    if not stat.S_ISDIR(observed_backup.st_mode) or (
                        opened_backup.st_dev,
                        opened_backup.st_ino,
                    ) != (observed_backup.st_dev, observed_backup.st_ino):
                        raise RegistryError(
                            f"registry backup directory changed while backup was created: "
                            f"{backup_directory}"
                        )
                except Exception:
                    if temporary_created:
                        with suppress(FileNotFoundError):
                            os.unlink(temporary_name, dir_fd=backup_descriptor)
                    if backup_published:
                        with suppress(FileNotFoundError):
                            os.unlink(backup_name, dir_fd=backup_descriptor)
                    with suppress(OSError):
                        os.fsync(backup_descriptor)
                    raise
        finally:
            os.close(backup_descriptor)
            os.close(registry_descriptor)
        return MigrationBackup(backup_directory / backup_name, digest)

    @staticmethod
    def _verify_migration_backup(backup_path: Path, schema_version: int) -> None:
        uri = f"{backup_path.resolve().as_uri()}?mode=ro"
        try:
            with closing(sqlite3.connect(uri, uri=True)) as connection:
                integrity = connection.execute("PRAGMA integrity_check").fetchone()
                if integrity is None or integrity[0] != "ok":
                    raise RegistryError(
                        f"registry migration backup failed integrity check: {backup_path}"
                    )
                version = connection.execute("SELECT version FROM schema_meta").fetchone()
                if version is None or version[0] != schema_version:
                    raise RegistryError(
                        f"registry migration backup has unexpected schema version: {backup_path}"
                    )
        except sqlite3.Error as error:
            raise RegistryError(
                f"registry migration backup is unreadable: {backup_path}: {error}"
            ) from error

    def _validate_migration_ledger(
        self, connection: sqlite3.Connection, schema_version: int
    ) -> None:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'applied_migrations'"
        ).fetchone()
        if table is None:
            raise RegistryError(
                f"registry schema {schema_version} is missing the applied migration ledger"
            )
        known = {
            step.migration_id: step
            for step in self._migration_plan().values()
            if step.migration_id is not None
        }
        rows = connection.execute(
            "SELECT migration_id, from_version, to_version, checksum "
            "FROM applied_migrations ORDER BY to_version, migration_id"
        ).fetchall()
        migration_ids = [row["migration_id"] for row in rows]
        transitions = [(row["from_version"], row["to_version"]) for row in rows]
        if len(migration_ids) != len(set(migration_ids)) or len(transitions) != len(
            set(transitions)
        ):
            raise RegistryError("registry applied migration ledger contains duplicate entries")
        required_ids = {
            step.migration_id for step in known.values() if step.to_version <= schema_version
        }
        observed_ids = set(migration_ids)
        if observed_ids != required_ids:
            missing = sorted(required_ids - observed_ids)
            unknown = sorted(observed_ids - required_ids)
            detail = []
            if missing:
                detail.append(f"missing {', '.join(missing)}")
            if unknown:
                detail.append(f"unknown {', '.join(unknown)}")
            raise RegistryError(
                "registry applied migration lineage is invalid: " + "; ".join(detail)
            )
        for row in rows:
            step = known.get(row["migration_id"])
            if step is None:
                raise RegistryError(
                    f"registry contains unknown applied migration: {row['migration_id']}"
                )
            observed = (row["from_version"], row["to_version"], row["checksum"])
            expected = (step.from_version, step.to_version, step.checksum)
            if observed != expected:
                raise RegistryError(f"registry migration checksum mismatch: {row['migration_id']}")
            if step.to_version > schema_version:
                raise RegistryError(
                    f"registry migration {row['migration_id']} exceeds schema {schema_version}"
                )

    def _record_applied_migration(
        self,
        connection: sqlite3.Connection,
        step: MigrationStep,
        backup: MigrationBackup | None,
    ) -> None:
        if step.migration_id is None or step.checksum is None:
            return
        connection.execute(
            """
            INSERT INTO applied_migrations(
                migration_id, from_version, to_version, checksum, applied_at,
                backup_path, backup_sha256
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                step.migration_id,
                step.from_version,
                step.to_version,
                step.checksum,
                _now(),
                None if backup is None else backup.path.relative_to(self.path.parent).as_posix(),
                None if backup is None else backup.sha256,
            ),
        )

    @staticmethod
    def _rollback_schema_transaction(connection: sqlite3.Connection) -> None:
        connection.rollback()

    def _ensure_schema(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._prepare_operational_database_file()
        inspected_version = self._inspect_existing_schema_version()
        if inspected_version is not None:
            self._validate_migration_path(inspected_version)
        with self._connection_context(write_ahead_log=False) as (connection, descriptor):
            while True:
                backup: MigrationBackup | None = None
                migration_step: MigrationStep | None = None
                self._assert_database_guard(descriptor)
                self._assert_live_repository_authority()
                connection.execute("BEGIN IMMEDIATE")
                try:
                    schema_exists = connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_meta'"
                    ).fetchone()
                    if schema_exists is None:
                        existing_objects = connection.execute(
                            "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' LIMIT 1"
                        ).fetchone()
                        if existing_objects is not None:
                            raise RegistryError(
                                "existing registry is missing schema_meta; refusing initialization"
                            )
                        self._execute_schema(connection)
                        self._insert_registry_identity(connection)
                        connection.execute(
                            "INSERT INTO schema_meta(version) VALUES (?)", (SCHEMA_VERSION,)
                        )
                        for step in self._migration_plan().values():
                            if step.migration_id is not None:
                                self._record_applied_migration(connection, step, None)
                        self._assert_live_repository_authority()
                        self._assert_database_guard(descriptor)
                        connection.commit()
                        self._assert_database_guard(descriptor)
                        return
                    versions = connection.execute("SELECT version FROM schema_meta").fetchall()
                    if not versions:
                        raise RegistryError("registry schema version is missing")
                    if len(versions) != 1:
                        raise RegistryError("registry schema version must contain exactly one row")
                    current = int(versions[0]["version"])
                    if current > SCHEMA_VERSION:
                        raise RegistryError(
                            f"registry schema {current} is newer than supported {SCHEMA_VERSION}"
                        )
                    self._validate_migration_path(current)
                    if current == SCHEMA_VERSION:
                        self._validate_migration_ledger(connection, current)
                        self._validate_registry_identity(connection)
                        self._assert_live_repository_authority()
                        self._assert_database_guard(descriptor)
                        connection.commit()
                        self._assert_database_guard(descriptor)
                        return

                    integrity = connection.execute("PRAGMA integrity_check").fetchone()
                    if integrity is None or integrity[0] != "ok":
                        raise RegistryError("registry failed integrity check; refusing migration")
                    backup = self._create_migration_backup(current)
                    self._assert_live_repository_authority()
                    self._assert_database_guard(descriptor)
                    step = self._migration_plan().get(current)
                    if step is None:
                        raise RegistryError(f"no registry migration from schema {current}")
                    migration_step = step
                    step.apply(connection)
                    self._record_applied_migration(connection, step, backup)
                    connection.execute("UPDATE schema_meta SET version = ?", (step.to_version,))
                    connection.execute(
                        "CREATE UNIQUE INDEX IF NOT EXISTS idx_schema_meta_singleton "
                        "ON schema_meta((1))"
                    )
                    if step.to_version >= MIGRATION_LEDGER_VERSION:
                        self._validate_migration_ledger(connection, step.to_version)
                    if step.to_version == SCHEMA_VERSION:
                        self._validate_registry_identity(connection)
                    self._assert_live_repository_authority()
                    self._assert_database_guard(descriptor)
                    connection.commit()
                    self._assert_database_guard(descriptor)
                    if step.to_version == SCHEMA_VERSION:
                        return
                except Exception as error:
                    rollback_error: Exception | None = None
                    try:
                        self._rollback_schema_transaction(connection)
                    except Exception as caught_rollback_error:
                        rollback_error = caught_rollback_error
                    if backup is not None and migration_step is not None:
                        migration_error = RegistryError(
                            f"registry migration {migration_step.from_version} -> "
                            f"{migration_step.to_version} failed: {error}; restore from "
                            f"{backup.path} (sha256 {backup.sha256})"
                        )
                        if rollback_error is not None:
                            migration_error.add_note(
                                f"registry migration rollback failed: {rollback_error}"
                            )
                        raise migration_error from error
                    if rollback_error is not None:
                        error.add_note(f"registry schema rollback failed: {rollback_error}")
                    raise

    @staticmethod
    def _execute_schema(connection: sqlite3.Connection) -> None:
        for statement in SCHEMA.split(";"):
            if statement.strip():
                connection.execute(statement)
        for statement in REGISTRY_IDENTITY_GUARD_SQL:
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

    @staticmethod
    def _migrate_v6_to_v7(connection: sqlite3.Connection) -> None:
        """Add durable, checksum-bound migration lineage without feature tables."""
        for statement in MIGRATION_V6_TO_V7_SQL.split(";"):
            if statement.strip():
                connection.execute(statement)

    def _migrate_v7_to_v8(self, connection: sqlite3.Connection) -> None:
        """Bind the registry to one immutable operational or ephemeral identity."""
        connection.execute(REGISTRY_IDENTITY_TABLE_SQL)
        for statement in REGISTRY_IDENTITY_GUARD_SQL:
            connection.execute(statement)
        self._insert_registry_identity(connection)

    @contextmanager
    def _transaction(self) -> Iterable[sqlite3.Connection]:
        self.initialize()
        if self.registry_role == "operational":
            self._preflight_schema_access()
        with self._connection_context(write_ahead_log=True) as (connection, descriptor):
            try:
                self._assert_database_guard(descriptor)
                self._assert_live_repository_authority()
                connection.execute("BEGIN IMMEDIATE")
                self._assert_schema_current(connection)
                yield connection
                self._assert_live_repository_authority()
                self._assert_database_guard(descriptor)
                connection.commit()
                self._assert_database_guard(descriptor)
            except Exception:
                connection.rollback()
                raise

    @contextmanager
    def _read_transaction(self) -> Iterable[sqlite3.Connection]:
        self.initialize()
        if self.registry_role == "operational":
            self._preflight_schema_access()
        with self._connection_context(write_ahead_log=True) as (connection, descriptor):
            transaction_started = False
            try:
                self._assert_database_guard(descriptor)
                self._assert_live_repository_authority()
                connection.execute("BEGIN DEFERRED")
                transaction_started = True
                self._assert_schema_current(connection)
                yield connection
                self._assert_database_guard(descriptor)
                connection.commit()
                self._assert_database_guard(descriptor)
                transaction_started = False
            except Exception as error:
                if transaction_started:
                    try:
                        connection.rollback()
                    except Exception as rollback_error:
                        error.add_note(f"read transaction rollback failed: {rollback_error}")
                raise

    @contextmanager
    def _connection_context(
        self,
        *,
        write_ahead_log: bool,
    ) -> Iterable[tuple[sqlite3.Connection, int | None]]:
        with self._database_file_guard() as descriptor:
            if descriptor is None:
                connection = self._connect() if write_ahead_log else self._connect_for_schema()
            else:
                connection = self._connect_for_schema(guard_descriptor=descriptor)
            try:
                if write_ahead_log and descriptor is not None:
                    self._enable_write_ahead_log(connection, descriptor)
                yield connection, descriptor
            finally:
                connection.close()

    @contextmanager
    def _readonly_connection_context(self) -> Iterable[sqlite3.Connection]:
        with self._database_file_guard() as descriptor:
            uri = f"{self.path.as_uri()}?mode=ro&immutable=1"
            connection = sqlite3.connect(uri, uri=True)
            try:
                self._assert_database_guard(descriptor)
                connection.row_factory = sqlite3.Row
                yield connection
                self._assert_database_guard(descriptor)
            finally:
                connection.close()

    def _connect(self) -> sqlite3.Connection:
        if self.registry_role == "operational":
            raise RegistryError("operational SQLite opens require a database file guard")
        connection = self._connect_for_schema()
        try:
            self._enable_write_ahead_log(connection, None)
        except Exception:
            connection.close()
            raise
        return connection

    def _enable_write_ahead_log(
        self,
        connection: sqlite3.Connection,
        descriptor: int | None,
    ) -> None:
        deadline = time.perf_counter() + (SQLITE_BUSY_TIMEOUT_MS / 1_000)
        while True:
            try:
                self._assert_database_guard(descriptor)
                connection.execute("PRAGMA journal_mode = WAL")
                self._assert_database_guard(descriptor)
                return
            except sqlite3.OperationalError as error:
                if "locked" not in str(error).lower() or time.perf_counter() >= deadline:
                    raise
                time.sleep(0.005)

    def _connect_for_schema(
        self,
        *,
        guard_descriptor: int | None = None,
    ) -> sqlite3.Connection:
        if self.registry_role == "operational" and guard_descriptor is None:
            raise RegistryError("operational SQLite opens require a database file guard")
        connection = sqlite3.connect(
            self.path,
            timeout=SQLITE_BUSY_TIMEOUT_MS / 1_000,
            isolation_level=None,
        )
        try:
            self._assert_database_guard(guard_descriptor)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
            self._assert_database_guard(guard_descriptor)
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
