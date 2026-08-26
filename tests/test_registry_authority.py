from __future__ import annotations

import shutil
import sqlite3
import subprocess
from contextlib import closing
from pathlib import Path

import pytest

import bossmode.cli as cli_module
from bossmode.cli import main
from bossmode.registry import Registry, RegistryError

REPOSITORY_URL = "https://example.com/acme/bossmode.git"


def run_git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def create_repository(path: Path, *, remote: str = REPOSITORY_URL) -> Path:
    path.mkdir()
    run_git(path, "init", "-b", "main")
    run_git(path, "config", "user.name", "Bossmode Test")
    run_git(path, "config", "user.email", "bossmode-test@example.com")
    run_git(path, "remote", "add", "origin", remote)
    (path / "README.md").write_text("test repository\n")
    run_git(path, "add", "README.md")
    run_git(path, "commit", "-m", "test: initialize repository")
    return path


def create_operational_registry(repository: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(repository)
    assert main(["registry", "create"]) == 0
    return repository / ".bossmode" / "control.db"


def snapshot(path: Path) -> tuple[bytes, int]:
    return path.read_bytes(), path.stat().st_mtime_ns


def create_schema_v7(database: Path) -> None:
    database.parent.mkdir(parents=True, exist_ok=True)
    version_five = Path(__file__).parent / "fixtures" / "schema_v5.sql"
    registry = Registry(database)
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.row_factory = sqlite3.Row
        connection.executescript(version_five.read_text())
        Registry._migrate_v5_to_v6(connection)
        connection.execute("UPDATE schema_meta SET version = 6")
        step = registry._migration_plan()[6]
        step.apply(connection)
        registry._record_applied_migration(connection, step, None)
        connection.execute("UPDATE schema_meta SET version = 7")


def test_registry_create_binds_immutable_primary_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repository = create_repository(tmp_path / "primary")
    database = create_operational_registry(repository, monkeypatch)
    capsys.readouterr()

    identity = Registry.open_for_command(database, explicit_path=False).get_registry_identity()

    assert identity["registry_id"].startswith("registry_")
    assert identity["registry_role"] == "operational"
    assert identity["repository_url"] == REPOSITORY_URL
    assert Path(identity["git_common_dir"]) == repository / ".git"
    assert Path(identity["primary_checkout"]) == repository
    assert identity["creation_metadata"] == {"schema_version": 8}
    with closing(sqlite3.connect(database)) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="registry identity is immutable"):
            connection.execute("UPDATE registry_identity SET repository_url = 'changed'")
        with pytest.raises(sqlite3.IntegrityError, match="registry identity is immutable"):
            connection.execute("DELETE FROM registry_identity")


def test_only_registry_create_can_upgrade_schema_v7_operational_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repository = create_repository(tmp_path / "primary")
    database = repository / ".bossmode" / "control.db"
    create_schema_v7(database)
    before = snapshot(database)
    monkeypatch.chdir(repository)

    assert main(["task", "list"]) == 2
    assert snapshot(database) == before
    assert not (database.parent / "backups").exists()

    assert main(["registry", "create"]) == 0
    identity = Registry.open_for_command(database, explicit_path=False).get_registry_identity()
    first_registry_id = identity["registry_id"]
    backups = list((database.parent / "backups").glob("control.db.schema-7.*.sqlite3"))
    assert len(backups) == 1
    assert identity["registry_role"] == "operational"
    assert identity["repository_url"] == REPOSITORY_URL
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT title FROM tasks WHERE id = 'task_v5'").fetchone()[0] == (
            "v5 task"
        )
    capsys.readouterr()

    assert main(["registry", "create"]) == 0
    repeated = Registry.open_for_command(database, explicit_path=False).get_registry_identity()
    assert repeated["registry_id"] == first_registry_id
    assert len(list((database.parent / "backups").glob("*.sqlite3"))) == 1


@pytest.mark.parametrize(
    "arguments",
    [
        [],
        ["reconcile"],
        ["task", "list"],
        ["task", "show", "task_missing"],
        ["run", "show", "run_missing"],
        ["herdr", "show", "run_missing"],
        ["turn", "show", "turn_missing"],
        [
            "evaluate",
            "task_missing",
            "--run-id",
            "run_missing",
            "--evaluator",
            "reviewer",
            "--passed",
            "--evidence",
            "none",
        ],
        [
            "feedback",
            "task_missing",
            "--category",
            "observation",
            "--key",
            "missing",
            "--content",
            "none",
        ],
        ["promotion", "list"],
        ["maintenance"],
        ["schedule", "status"],
    ],
)
def test_every_operational_command_fails_before_creating_absent_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    arguments: list[str],
) -> None:
    repository = create_repository(tmp_path / "primary")
    monkeypatch.chdir(repository)

    assert main(arguments) == 2

    assert not (repository / ".bossmode").exists()


def test_direct_ephemeral_api_cannot_create_at_operational_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = create_repository(tmp_path / "primary")
    database = repository / ".bossmode" / "control.db"
    monkeypatch.chdir(tmp_path)

    with pytest.raises(RegistryError, match="ephemeral registry access cannot target"):
        Registry(database).initialize()

    assert not (repository / ".bossmode").exists()


def test_linked_worktree_cannot_create_or_open_operational_registry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    primary = create_repository(tmp_path / "primary")
    database = create_operational_registry(primary, monkeypatch)
    capsys.readouterr()
    linked = tmp_path / "linked"
    run_git(primary, "worktree", "add", "-b", "linked-test", str(linked))
    before = snapshot(database)
    monkeypatch.chdir(linked)

    assert main(["registry", "create"]) == 2
    assert main(["--db", str(database), "task", "list"]) == 2

    assert snapshot(database) == before
    assert not (linked / ".bossmode").exists()


def test_existing_operational_object_rechecks_linked_caller_before_write_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    primary = create_repository(tmp_path / "primary")
    database = create_operational_registry(primary, monkeypatch)
    capsys.readouterr()
    registry = Registry.open_for_command(database, explicit_path=False)
    registry.get_registry_identity()
    linked = tmp_path / "linked"
    run_git(primary, "worktree", "add", "-b", "direct-linked-test", str(linked))
    before = snapshot(database)
    monkeypatch.chdir(linked)

    with pytest.raises(RegistryError, match="linked worktrees cannot mutate"):
        registry.list_tasks()

    assert snapshot(database) == before


def test_git_repository_without_unambiguous_remote_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "primary"
    repository.mkdir()
    run_git(repository, "init", "-b", "main")
    monkeypatch.chdir(repository)

    assert main([]) == 2

    assert not (repository / ".bossmode").exists()


def test_noncanonical_default_path_in_repository_is_not_implicit_ephemeral_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = create_repository(tmp_path / "primary")
    monkeypatch.chdir(repository)

    with pytest.raises(RegistryError, match="path is not authoritative"):
        Registry.open_for_command(tmp_path / "alternate.db", explicit_path=False)


def test_operational_registry_rejects_caller_from_different_repository(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    owner = create_repository(tmp_path / "owner")
    database = create_operational_registry(owner, monkeypatch)
    capsys.readouterr()
    other = create_repository(tmp_path / "other")
    before = snapshot(database)
    monkeypatch.chdir(other)

    assert main(["--db", str(database), "task", "list"]) == 2

    assert snapshot(database) == before


def test_registry_constructor_rejects_unknown_role(tmp_path: Path) -> None:
    with pytest.raises(RegistryError, match="unsupported registry role"):
        Registry(tmp_path / "control.db", registry_role="unknown")


@pytest.mark.parametrize(
    ("metadata", "message"),
    [("not-json", "creation metadata is invalid"), ("[]", "must be an object")],
)
def test_registry_rejects_invalid_creation_metadata(
    tmp_path: Path,
    metadata: str,
    message: str,
) -> None:
    database = tmp_path / "control.db"
    Registry(database).initialize()
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("DROP TRIGGER registry_identity_immutable_update")
        connection.execute(
            "UPDATE registry_identity SET creation_metadata_json = ?",
            (metadata,),
        )

    with pytest.raises(RegistryError, match=message):
        Registry(database).get_registry_identity()


def test_registry_rejects_missing_identity_row(tmp_path: Path) -> None:
    database = tmp_path / "control.db"
    Registry(database).initialize()
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("DROP TRIGGER registry_identity_immutable_delete")
        connection.execute("DELETE FROM registry_identity")

    with pytest.raises(RegistryError, match="must contain exactly one row"):
        Registry(database).initialize()


def test_operational_copy_and_wrong_repository_fail_before_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = create_repository(tmp_path / "source")
    source_database = create_operational_registry(source, monkeypatch)
    capsys.readouterr()
    copied = tmp_path / "copied.db"
    shutil.copy2(source_database, copied)
    copied_before = snapshot(copied)

    assert main(["--db", str(copied), "task", "list"]) == 2
    assert snapshot(copied) == copied_before

    other = create_repository(
        tmp_path / "other",
        remote="https://example.com/acme/other.git",
    )
    other_database = other / ".bossmode" / "control.db"
    other_database.parent.mkdir()
    shutil.copy2(source_database, other_database)
    other_before = snapshot(other_database)
    monkeypatch.chdir(other)

    assert main(["task", "list"]) == 2
    assert snapshot(other_database) == other_before


def test_symlinked_registry_path_is_rejected_before_target_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repository = create_repository(tmp_path / "primary")
    database = create_operational_registry(repository, monkeypatch)
    capsys.readouterr()
    target_before = snapshot(database)
    symlink = tmp_path / "registry-link.db"
    symlink.symlink_to(database)

    assert main(["--db", str(symlink), "task", "list"]) == 2

    assert snapshot(database) == target_before

    real_directory = tmp_path / "real-registry"
    real_directory.mkdir()
    copied = real_directory / "control.db"
    shutil.copy2(database, copied)
    copied_before = snapshot(copied)
    linked_directory = tmp_path / "linked-registry"
    linked_directory.symlink_to(real_directory, target_is_directory=True)

    assert main(["--db", str(linked_directory / "control.db"), "task", "list"]) == 2
    assert snapshot(copied) == copied_before


def test_registry_create_rejects_noncanonical_operational_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = create_repository(tmp_path / "primary")
    alternate = tmp_path / "alternate.db"
    monkeypatch.chdir(repository)

    assert main(["--db", str(alternate), "registry", "create"]) == 2

    assert not alternate.exists()
    assert not (repository / ".bossmode").exists()


def test_ephemeral_registry_cannot_become_operational_by_path_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ephemeral = tmp_path / "certification.db"
    Registry(ephemeral).initialize()
    assert main(["--db", str(ephemeral), "task", "list"]) == 0
    capsys.readouterr()
    repository = create_repository(tmp_path / "primary")
    operational_path = repository / ".bossmode" / "control.db"
    operational_path.parent.mkdir()
    shutil.copy2(ephemeral, operational_path)
    before = snapshot(operational_path)
    monkeypatch.chdir(repository)

    assert main(["registry", "create"]) == 2
    with pytest.raises(RegistryError, match="non-operational registry"):
        Registry.create_operational(operational_path)

    assert snapshot(operational_path) == before


def test_scheduler_uses_and_reports_validated_primary_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repository = create_repository(tmp_path / "primary")
    create_operational_registry(repository, monkeypatch)
    capsys.readouterr()
    observed: list[tuple[str, Path]] = []

    def fake_status(repo_dir: Path, *, log_path: str | None = None) -> dict:
        observed.append(("status", repo_dir))
        return {"status": "not_installed", "log_path": log_path}

    def fake_install(repo_dir: Path, **_kwargs) -> dict:
        observed.append(("install", repo_dir))
        return {"status": "installed"}

    def fake_uninstall(repo_dir: Path) -> dict:
        observed.append(("uninstall", repo_dir))
        return {"status": "uninstalled"}

    monkeypatch.setattr(cli_module, "get_schedule_status", fake_status)
    monkeypatch.setattr(cli_module, "install_schedule", fake_install)
    monkeypatch.setattr(cli_module, "uninstall_schedule", fake_uninstall)

    assert main(["schedule", "status"]) == 0
    report = capsys.readouterr().out
    assert '"registry_id": "registry_' in report
    assert f'"repository_url": "{REPOSITORY_URL}"' in report
    assert main(["schedule", "install"]) == 0
    capsys.readouterr()
    assert main(["schedule", "uninstall"]) == 0
    capsys.readouterr()
    assert observed == [
        ("status", repository),
        ("install", repository),
        ("uninstall", repository),
    ]

    assert main(["schedule", "status", "--repo-dir", str(tmp_path)]) == 2
    assert observed[-1] == ("uninstall", repository)
