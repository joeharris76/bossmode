from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import stat
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from threading import Barrier

import pytest

from bossmode.registry import (
    MIGRATION_V6_TO_V7_ID,
    SCHEMA_VERSION,
    Registry,
    RegistryError,
)


def create_schema_v6(database: Path) -> None:
    version_five = Path(__file__).parent / "fixtures" / "schema_v5.sql"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.row_factory = sqlite3.Row
        connection.executescript(version_five.read_text())
        Registry._migrate_v5_to_v6(connection)
        connection.execute("UPDATE schema_meta SET version = 6")


def backup_paths(database: Path) -> list[Path]:
    return sorted((database.parent / "backups").glob(f"{database.name}.schema-*.sqlite3"))


def file_sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def migration_lineage(database: Path) -> list[tuple[str, int, int, str]]:
    with closing(sqlite3.connect(database)) as connection:
        return connection.execute(
            "SELECT migration_id, from_version, to_version, checksum "
            "FROM applied_migrations ORDER BY to_version, migration_id"
        ).fetchall()


def test_fresh_schema_seeds_lineage_without_backup(tmp_path: Path) -> None:
    database = tmp_path / "control.db"

    Registry(database).initialize()

    assert SCHEMA_VERSION == 7
    assert not (tmp_path / "backups").exists()
    with closing(sqlite3.connect(database)) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute("SELECT * FROM applied_migrations").fetchone()
        assert row is not None
        assert row["migration_id"] == MIGRATION_V6_TO_V7_ID
        assert row["from_version"] == 6
        assert row["to_version"] == 7
        assert len(row["checksum"]) == 64
        assert row["backup_path"] is None
        assert row["backup_sha256"] is None


def test_fresh_and_migrated_registries_have_identical_lineage(tmp_path: Path) -> None:
    fresh = tmp_path / "fresh.db"
    migrated = tmp_path / "migrated.db"
    Registry(fresh).initialize()
    create_schema_v6(migrated)

    Registry(migrated).initialize()

    assert migration_lineage(fresh) == migration_lineage(migrated)


def test_v6_migration_creates_verified_backup_and_no_team_schema(tmp_path: Path) -> None:
    database = tmp_path / "control.db"
    create_schema_v6(database)

    Registry(database).initialize()

    backups = backup_paths(database)
    assert len(backups) == 1
    backup = backups[0]
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert stat.S_IMODE(backup.parent.stat().st_mode) == 0o700
    assert backup.name.endswith(f".sha256-{file_sha256(backup)}.sqlite3")
    with closing(sqlite3.connect(database)) as connection:
        connection.row_factory = sqlite3.Row
        assert connection.execute("SELECT version FROM schema_meta").fetchone()[0] == 7
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'teams'"
            ).fetchone()[0]
            == 0
        )
        migration = connection.execute("SELECT * FROM applied_migrations").fetchone()
        assert migration["migration_id"] == MIGRATION_V6_TO_V7_ID
        assert migration["backup_path"] == f"backups/{backup.name}"
        assert migration["backup_sha256"] == file_sha256(backup)
    with closing(sqlite3.connect(backup)) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT version FROM schema_meta").fetchone()[0] == 6
        assert connection.execute("SELECT title FROM tasks WHERE id = 'task_v5'").fetchone()[0] == (
            "v5 task"
        )


def test_legacy_migrations_are_individually_bounded_and_backed_up(tmp_path: Path) -> None:
    database = tmp_path / "control.db"
    version_five = Path(__file__).parent / "fixtures" / "schema_v5.sql"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.executescript(version_five.read_text())

    Registry(database).initialize()

    backups = backup_paths(database)
    assert len(backups) == 2
    backup_versions: dict[int, Path] = {}
    for backup in backups:
        with closing(sqlite3.connect(backup)) as connection:
            version = connection.execute("SELECT version FROM schema_meta").fetchone()[0]
            backup_versions[version] = backup
    assert set(backup_versions) == {5, 6}
    with closing(sqlite3.connect(database)) as connection:
        connection.row_factory = sqlite3.Row
        migration = connection.execute("SELECT * FROM applied_migrations").fetchone()
        assert migration["backup_path"] == f"backups/{backup_versions[6].name}"
        assert migration["backup_sha256"] == file_sha256(backup_versions[6])


def test_backup_restores_original_schema_and_records(tmp_path: Path) -> None:
    database = tmp_path / "control.db"
    failed_database = tmp_path / "control.db.failed"
    create_schema_v6(database)
    Registry(database).initialize()
    backup = backup_paths(database)[0]

    database.replace(failed_database)
    shutil.copyfile(backup, database)

    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT version FROM schema_meta").fetchone()[0] == 6
        assert (
            connection.execute("SELECT summary FROM run_turns WHERE id = 'turn_done'").fetchone()[0]
            == "finished turn"
        )
        assert (
            connection.execute("SELECT category FROM feedback WHERE id = 'fb_v5'").fetchone()[0]
            == "correction"
        )

    Registry(database).initialize()

    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT version FROM schema_meta").fetchone()[0] == 7
        assert connection.execute("SELECT title FROM tasks WHERE id = 'task_v5'").fetchone()[0] == (
            "v5 task"
        )
    assert failed_database.exists()
    assert len(backup_paths(database)) == 2


def test_failed_migration_rolls_back_and_retains_valid_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "control.db"
    create_schema_v6(database)
    registry = Registry(database)

    def fail_migration(connection: sqlite3.Connection) -> None:
        Registry._migrate_v6_to_v7(connection)
        connection.execute("INVALID SQL")

    monkeypatch.setattr(registry, "_migrate_v6_to_v7", fail_migration)

    with pytest.raises(RegistryError, match=r"migration 6 -> 7 failed.*restore from"):
        registry.initialize()

    backups = backup_paths(database)
    assert len(backups) == 1
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT version FROM schema_meta").fetchone()[0] == 6
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE type = 'table' AND name = 'applied_migrations'"
            ).fetchone()[0]
            == 0
        )
    with closing(sqlite3.connect(backups[0])) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT version FROM schema_meta").fetchone()[0] == 6


def test_interrupted_migration_reports_recovery_for_non_sql_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "control.db"
    create_schema_v6(database)
    registry = Registry(database)

    def interrupt_migration(connection: sqlite3.Connection) -> None:
        Registry._migrate_v6_to_v7(connection)
        raise RuntimeError("simulated interruption")

    monkeypatch.setattr(registry, "_migrate_v6_to_v7", interrupt_migration)

    with pytest.raises(
        RegistryError,
        match=r"migration 6 -> 7 failed: simulated interruption; restore from.*sha256",
    ):
        registry.initialize()

    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT version FROM schema_meta").fetchone()[0] == 6
    assert len(backup_paths(database)) == 1


def test_backup_verification_failure_prevents_migration_and_removes_partial_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "control.db"
    create_schema_v6(database)
    registry = Registry(database)

    original_verify = registry._verify_migration_backup

    def corrupt_backup(path: Path, version: int) -> None:
        path.write_bytes(b"not a sqlite database")
        original_verify(path, version)

    monkeypatch.setattr(registry, "_verify_migration_backup", corrupt_backup)

    with pytest.raises(RegistryError, match="backup is unreadable"):
        registry.initialize()

    assert backup_paths(database) == []
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT version FROM schema_meta").fetchone()[0] == 6


@pytest.mark.parametrize("corruption", ["missing", "checksum", "unknown", "duplicate"])
def test_schema_v7_rejects_invalid_migration_lineage_without_mutation(
    tmp_path: Path, corruption: str
) -> None:
    database = tmp_path / "control.db"
    Registry(database).initialize()
    with closing(sqlite3.connect(database)) as connection, connection:
        if corruption == "missing":
            connection.execute("DELETE FROM applied_migrations")
        elif corruption == "checksum":
            connection.execute("UPDATE applied_migrations SET checksum = 'wrong'")
        elif corruption == "unknown":
            connection.execute(
                "UPDATE applied_migrations SET migration_id = 'unmerged_branch_migration'"
            )
        else:
            connection.execute("ALTER TABLE applied_migrations RENAME TO old_migrations")
            connection.execute(
                """
                CREATE TABLE applied_migrations (
                    migration_id TEXT,
                    from_version INTEGER,
                    to_version INTEGER,
                    checksum TEXT,
                    applied_at TEXT,
                    backup_path TEXT,
                    backup_sha256 TEXT
                )
                """
            )
            connection.execute("INSERT INTO applied_migrations SELECT * FROM old_migrations")
            connection.execute("INSERT INTO applied_migrations SELECT * FROM old_migrations")
            connection.execute("DROP TABLE old_migrations")
    before = database.read_bytes()
    before_mtime = database.stat().st_mtime_ns

    with pytest.raises(RegistryError, match="migration"):
        Registry(database).initialize()

    assert database.read_bytes() == before
    assert database.stat().st_mtime_ns == before_mtime


def test_pr6_shaped_schema_v7_without_ledger_fails_closed(tmp_path: Path) -> None:
    database = tmp_path / "control.db"
    create_schema_v6(database)
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("ALTER TABLE tasks ADD COLUMN team_id TEXT")
        connection.execute("UPDATE schema_meta SET version = 7")
    before = database.read_bytes()

    with pytest.raises(RegistryError, match="missing the applied migration ledger"):
        Registry(database).initialize()

    assert database.read_bytes() == before
    assert backup_paths(database) == []


def test_newer_schema_fails_closed_before_backup_or_mutation(tmp_path: Path) -> None:
    database = tmp_path / "control.db"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("CREATE TABLE schema_meta(version INTEGER NOT NULL)")
        connection.execute("INSERT INTO schema_meta(version) VALUES (?)", (SCHEMA_VERSION + 1,))
    before = database.read_bytes()
    before_mtime = database.stat().st_mtime_ns

    with pytest.raises(RegistryError, match="newer than supported"):
        Registry(database).initialize()

    assert database.read_bytes() == before
    assert database.stat().st_mtime_ns == before_mtime
    assert backup_paths(database) == []


def test_concurrent_v6_initialization_creates_one_backup(tmp_path: Path) -> None:
    database = tmp_path / "control.db"
    create_schema_v6(database)
    barrier = Barrier(6)

    def initialize(_: int) -> Exception | None:
        barrier.wait()
        try:
            Registry(database).initialize()
        except Exception as error:
            return error
        return None

    with ThreadPoolExecutor(max_workers=6) as executor:
        errors = [result for result in executor.map(initialize, range(6)) if result]

    assert errors == []
    assert len(backup_paths(database)) == 1
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT version FROM schema_meta").fetchone()[0] == 7
        assert connection.execute("SELECT COUNT(*) FROM applied_migrations").fetchone()[0] == 1


def test_backup_includes_committed_wal_records(tmp_path: Path) -> None:
    database = tmp_path / "control.db"
    create_schema_v6(database)

    with closing(sqlite3.connect(database)) as writer:
        assert writer.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        writer.execute(
            "INSERT INTO tasks(id, title, goal, success_criteria, state, created_at, updated_at) "
            "VALUES ('task_wal', 'WAL task', 'goal', 'criteria', 'ready', '2026-01-01', "
            "'2026-01-01')"
        )
        writer.commit()
        assert database.with_name(f"{database.name}-wal").exists()

        Registry(database).initialize()

        backup = backup_paths(database)[0]
        with closing(sqlite3.connect(backup)) as connection:
            assert (
                connection.execute("SELECT title FROM tasks WHERE id = 'task_wal'").fetchone()[0]
                == "WAL task"
            )


def test_symlinked_backup_directory_is_rejected_without_migration(tmp_path: Path) -> None:
    database = tmp_path / "control.db"
    external = tmp_path / "external"
    external.mkdir()
    create_schema_v6(database)
    (tmp_path / "backups").symlink_to(external, target_is_directory=True)

    with pytest.raises(RegistryError, match="backup directory must be a real directory"):
        Registry(database).initialize()

    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT version FROM schema_meta").fetchone()[0] == 6
    assert list(external.iterdir()) == []


def test_backup_publication_uses_exclusive_no_follow_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "control.db"
    create_schema_v6(database)
    original_open = os.open
    observed_flags: list[int] = []

    def recording_open(path: str | Path, flags: int, *args, **kwargs):
        candidate = Path(path)
        if candidate.parent.name == "backups" and candidate.name.endswith(".partial"):
            observed_flags.append(flags)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", recording_open)

    Registry(database).initialize()

    assert observed_flags
    creation_flags = observed_flags[0]
    assert creation_flags & os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        assert creation_flags & os.O_NOFOLLOW


def test_migration_does_not_prune_existing_backups_before_owned_cleanup_exists(
    tmp_path: Path,
) -> None:
    database = tmp_path / "control.db"
    create_schema_v6(database)
    backup_directory = tmp_path / "backups"
    backup_directory.mkdir()
    retained = [
        backup_directory / f"control.db.schema-5.retained-{index}.sqlite3" for index in range(6)
    ]
    for path in retained:
        path.write_bytes(b"retained by policy")

    Registry(database).initialize()

    assert all(path.read_bytes() == b"retained by policy" for path in retained)
    assert len(backup_paths(database)) == 7
