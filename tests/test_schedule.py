from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bossmode.cli import main
from bossmode.scheduler import (
    SCHEDULE_TARGETS,
    CrontabAdapter,
    LaunchdAdapter,
    SchedulerError,
    get_repo_hash,
    get_schedule_status,
    install_schedule,
    resolve_bossmode_command,
    resolve_uv_path,
    rotate_log_if_needed,
    scheduled_command,
    uninstall_schedule,
)


def operational_registry(repository: Path) -> MagicMock:
    registry = MagicMock()
    registry.get_registry_identity.return_value = {
        "registry_id": "registry_schedule_test",
        "registry_role": "operational",
        "repository_url": "https://example.com/acme/bossmode.git",
        "primary_checkout": str(repository),
    }
    return registry


def test_get_repo_hash_deterministic(tmp_path: Path) -> None:
    h1 = get_repo_hash(tmp_path)
    h2 = get_repo_hash(tmp_path)
    assert h1 == h2
    assert len(h1) == 8


def test_resolve_uv_path() -> None:
    path = resolve_uv_path()
    assert isinstance(path, str)
    assert len(path) > 0


def test_command_falls_back_to_active_python_when_uv_is_unavailable() -> None:
    with (
        patch("bossmode.scheduler.shutil.which", return_value=None),
        patch("bossmode.scheduler.Path.is_file", return_value=False),
    ):
        with pytest.raises(SchedulerError, match="uv executable not found"):
            resolve_uv_path()
        assert resolve_bossmode_command() == [
            str(Path(sys.executable).resolve()),
            "-m",
            "bossmode.cli",
        ]

    with pytest.raises(SchedulerError, match="unsupported schedule target"):
        scheduled_command("unknown")


def test_scheduled_command_names_every_target_explicitly() -> None:
    """`reconcile` used to be scheduled as a naked `bossmode`, hiding the target."""
    for target in sorted(SCHEDULE_TARGETS):
        assert scheduled_command(target)[-1] == target


def test_rotate_log_if_needed(tmp_path: Path) -> None:
    log_file = tmp_path / "test.log"
    log_file.write_text("a" * 100)

    # Threshold 500: should not rotate
    rotate_log_if_needed(log_file, max_bytes=500)
    assert log_file.exists()
    assert not (tmp_path / "test.log.old").exists()

    # Threshold 50: should rotate
    rotate_log_if_needed(log_file, max_bytes=50)
    assert not log_file.exists()
    assert (tmp_path / "test.log.old").exists()
    assert (tmp_path / "test.log.old").read_text() == "a" * 100

    log_file.write_text("replacement")
    rotate_log_if_needed(log_file, max_bytes=1)
    assert (tmp_path / "test.log.old").read_text() == "replacement"

    with patch.object(Path, "is_file", side_effect=OSError("stat failed")):
        rotate_log_if_needed(log_file, max_bytes=1)


def test_launchd_generate_plist(tmp_path: Path) -> None:
    log_path = tmp_path / "logs" / "schedule.log"
    plist = LaunchdAdapter.generate_plist(
        tmp_path, target="maintenance", interval_seconds=3600, log_path=log_path
    )
    repo_hash = get_repo_hash(tmp_path)
    assert plist["Label"] == f"com.bossmode.{repo_hash}"
    assert plist["StartInterval"] == 3600
    assert plist["WorkingDirectory"] == str(tmp_path.resolve())
    assert plist["StandardOutPath"] == str(log_path.resolve())
    assert "PATH" in plist["EnvironmentVariables"]


def test_launchd_lifecycle_mocked(tmp_path: Path, monkeypatch) -> None:
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home_dir)

    log_path = tmp_path / "logs" / "schedule.log"

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        res = LaunchdAdapter.install(
            tmp_path, target="maintenance", interval_seconds=1800, log_path=log_path
        )
        assert res["status"] == "installed"
        assert res["platform"] == "macos_launchd"

        plist_file = LaunchdAdapter.get_plist_path(get_repo_hash(tmp_path))
        assert plist_file.is_file()

        # Check status
        status_res = LaunchdAdapter.status(tmp_path, log_path)
        assert status_res["installed"] is True
        assert status_res["loaded"] is True
        assert status_res["status"] == "loaded"

        # Uninstall
        uninst_res = LaunchdAdapter.uninstall(tmp_path)
        assert uninst_res["status"] == "uninstalled"
        assert not plist_file.is_file()


@pytest.mark.parametrize("legacy_returncode", [0, 1])
def test_launchd_fallback_is_fail_closed(
    tmp_path: Path, monkeypatch, legacy_returncode: int
) -> None:
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home_dir)
    monkeypatch.setattr(LaunchdAdapter, "uninstall", lambda _repo: {})
    results = [
        MagicMock(returncode=1, stdout="", stderr="bootstrap failed"),
        MagicMock(returncode=legacy_returncode, stdout="", stderr="load failed"),
    ]

    with patch("subprocess.run", side_effect=results):
        if legacy_returncode == 0:
            installed = LaunchdAdapter.install(
                tmp_path, "maintenance", 300, tmp_path / "schedule.log"
            )
            assert installed["status"] == "installed"
        else:
            with pytest.raises(SchedulerError, match="bootstrap failed.*load failed"):
                LaunchdAdapter.install(tmp_path, "maintenance", 300, tmp_path / "schedule.log")
            plist = LaunchdAdapter.get_plist_path(get_repo_hash(tmp_path))
            assert not plist.exists()


def test_launchd_status_uses_legacy_probe_and_reports_log_activity(
    tmp_path: Path, monkeypatch
) -> None:
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home_dir)
    plist = LaunchdAdapter.get_plist_path(get_repo_hash(tmp_path))
    plist.parent.mkdir(parents=True)
    plist.write_text("installed")
    log_path = tmp_path / "schedule.log"
    log_path.write_text("ran")
    probes = [MagicMock(returncode=1), MagicMock(returncode=0)]

    with patch("subprocess.run", side_effect=probes):
        status = LaunchdAdapter.status(tmp_path, log_path)

    assert status["loaded"] is True
    assert status["log_size_bytes"] == 3
    assert status["last_log_activity"] is not None


def test_launchd_uninstall_failure_preserves_plist(tmp_path: Path, monkeypatch) -> None:
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home_dir)
    plist = LaunchdAdapter.get_plist_path(get_repo_hash(tmp_path))
    plist.parent.mkdir(parents=True)
    plist.write_text("installed")
    failures = [
        MagicMock(returncode=1, stderr="bootout failed"),
        MagicMock(returncode=1, stderr="unload failed"),
    ]

    with (
        patch("subprocess.run", side_effect=failures),
        pytest.raises(SchedulerError, match="bootout failed.*unload failed"),
    ):
        LaunchdAdapter.uninstall(tmp_path)

    assert plist.is_file()


def test_crontab_interval_to_cron() -> None:
    assert CrontabAdapter.interval_to_cron(60) == "* * * * *"
    assert CrontabAdapter.interval_to_cron(300) == "*/5 * * * *"
    assert CrontabAdapter.interval_to_cron(3600) == "0 * * * *"
    assert CrontabAdapter.interval_to_cron(7200) == "0 */2 * * *"
    assert CrontabAdapter.interval_to_cron(86400) == "0 0 * * *"


@pytest.mark.parametrize("interval", [30, 90, 420, 5400, 90000])
def test_crontab_rejects_inexact_intervals(interval: int) -> None:
    with pytest.raises(SchedulerError, match="cron interval"):
        CrontabAdapter.interval_to_cron(interval)


def test_crontab_lifecycle_mocked(tmp_path: Path) -> None:
    fake_crontab = "# existing user cron\n0 5 * * * /usr/bin/backup\n"
    current_cron = [fake_crontab]

    def mock_read():
        return current_cron[0]

    def mock_write(content: str):
        current_cron[0] = content

    with (
        patch.object(CrontabAdapter, "read_crontab", side_effect=mock_read),
        patch.object(CrontabAdapter, "write_crontab", side_effect=mock_write),
    ):
        log_path = tmp_path / "logs" / "schedule.log"
        res = CrontabAdapter.install(
            tmp_path,
            target="maintenance",
            interval_seconds=3600,
            cron_expr=None,
            log_path=log_path,
        )
        assert res["status"] == "installed"
        assert res["platform"] == "unix_crontab"

        repo_hash = get_repo_hash(tmp_path)
        assert f"# BEGIN BOSSMODE {repo_hash}" in current_cron[0]
        assert "0 5 * * * /usr/bin/backup" in current_cron[0]

        status_res = CrontabAdapter.status(tmp_path, log_path)
        assert status_res["installed"] is True
        assert status_res["status"] == "registered"

        uninst_res = CrontabAdapter.uninstall(tmp_path)
        assert uninst_res["status"] == "uninstalled"
        assert f"# BEGIN BOSSMODE {repo_hash}" not in current_cron[0]
        assert "0 5 * * * /usr/bin/backup" in current_cron[0]


@pytest.mark.parametrize("operation", ["install", "uninstall"])
def test_crontab_unmatched_fence_preserves_user_entries(tmp_path: Path, operation: str) -> None:
    repo_hash = get_repo_hash(tmp_path)
    original = f"0 5 * * * backup\n# BEGIN BOSSMODE {repo_hash}\n0 6 * * * user-job\n"
    written: list[str] = []
    with (
        patch.object(CrontabAdapter, "read_crontab", return_value=original),
        patch.object(CrontabAdapter, "write_crontab", side_effect=written.append),
        pytest.raises(SchedulerError, match="unmatched Bossmode begin marker"),
    ):
        if operation == "install":
            CrontabAdapter.install(tmp_path, "maintenance", 3600, None, tmp_path / "schedule.log")
        else:
            CrontabAdapter.uninstall(tmp_path)

    assert written == []


@pytest.mark.parametrize(
    "content",
    [
        "# END BOSSMODE hash\n# BEGIN BOSSMODE hash\n",
        "# BEGIN BOSSMODE hash\n# END BOSSMODE hash\n",
        "# BEGIN BOSSMODE hash\none\ntwo\n# END BOSSMODE hash\n",
    ],
)
def test_crontab_status_rejects_malformed_managed_blocks(tmp_path: Path, content: str) -> None:
    repo_hash = get_repo_hash(tmp_path)
    current = content.replace("hash", repo_hash)
    with (
        patch.object(CrontabAdapter, "read_crontab", return_value=current),
        pytest.raises(SchedulerError, match="crontab"),
    ):
        CrontabAdapter.status(tmp_path, tmp_path / "schedule.log")


def test_crontab_read_failure_does_not_look_empty() -> None:
    failed = MagicMock(returncode=2, stdout="", stderr="permission denied")
    with (
        patch("subprocess.run", return_value=failed),
        pytest.raises(SchedulerError, match="permission denied"),
    ):
        CrontabAdapter.read_crontab()

    no_crontab = MagicMock(returncode=1, stdout="", stderr="no crontab for user")
    with patch("subprocess.run", return_value=no_crontab):
        assert CrontabAdapter.read_crontab() == ""

    with (
        patch("subprocess.run", side_effect=FileNotFoundError("missing")),
        pytest.raises(SchedulerError, match="crontab invocation failed"),
    ):
        CrontabAdapter.read_crontab()


def test_crontab_write_failure_is_reported() -> None:
    failed = MagicMock(returncode=1, stdout="", stderr="write denied")
    with (
        patch("subprocess.run", return_value=failed),
        pytest.raises(SchedulerError, match="write denied"),
    ):
        CrontabAdapter.write_crontab("* * * * * true")


def test_crontab_quotes_paths_and_fallback_command(tmp_path: Path) -> None:
    repo = tmp_path / "repo with spaces"
    repo.mkdir()
    log_path = repo / "logs and output" / "schedule.log"
    written: list[str] = []
    with (
        patch.object(CrontabAdapter, "read_crontab", return_value=""),
        patch.object(CrontabAdapter, "write_crontab", side_effect=written.append),
        patch(
            "bossmode.scheduler.resolve_bossmode_command",
            return_value=["/python with spaces", "-m", "bossmode.cli"],
        ),
    ):
        CrontabAdapter.install(repo, "maintenance", 3600, None, log_path)

    assert "cd '" in written[0]
    assert "'/python with spaces' -m bossmode.cli maintenance" in written[0]
    assert "'" + str(log_path.resolve()) + "' 2>&1" in written[0]


@pytest.mark.parametrize("expression", ["* * * *", "* * * * *\n@reboot bad"])
def test_crontab_rejects_malformed_or_multiline_expressions(
    tmp_path: Path, expression: str
) -> None:
    with pytest.raises(SchedulerError, match="exactly five fields"):
        CrontabAdapter.install(
            tmp_path,
            target="maintenance",
            interval_seconds=3600,
            cron_expr=expression,
            log_path=tmp_path / "schedule.log",
        )


def test_high_level_schedule_functions(tmp_path: Path) -> None:
    with (
        patch("bossmode.scheduler.LaunchdAdapter.install") as mock_install,
        patch("bossmode.scheduler.LaunchdAdapter.status") as mock_status,
        patch("bossmode.scheduler.LaunchdAdapter.uninstall") as mock_uninstall,
        patch("bossmode.scheduler.is_macos", return_value=True),
    ):
        mock_install.return_value = {"status": "installed"}
        mock_status.return_value = {"status": "loaded"}
        mock_uninstall.return_value = {"status": "uninstalled"}

        assert install_schedule(tmp_path)["status"] == "installed"
        assert get_schedule_status(tmp_path)["status"] == "loaded"
        assert uninstall_schedule(tmp_path)["status"] == "uninstalled"

    with (
        patch("bossmode.scheduler.is_macos", return_value=True),
        pytest.raises(SchedulerError, match="not supported by launchd"),
    ):
        install_schedule(tmp_path, cron_expr="0 * * * *")


@pytest.mark.parametrize("operation", [install_schedule, get_schedule_status, uninstall_schedule])
def test_high_level_schedule_rejects_unsupported_platform(tmp_path: Path, operation) -> None:
    with (
        patch("bossmode.scheduler.is_macos", return_value=False),
        patch("bossmode.scheduler.is_linux", return_value=False),
        patch("bossmode.scheduler.sys.platform", "win32"),
        pytest.raises(SchedulerError, match="unsupported scheduler platform: win32"),
    ):
        operation(tmp_path)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"target": "unknown"}, "unsupported schedule target"),
        ({"interval_seconds": 59}, "at least 60 seconds"),
    ],
)
def test_schedule_install_rejects_invalid_requests(tmp_path: Path, kwargs, message: str) -> None:
    with pytest.raises(SchedulerError, match=message):
        install_schedule(tmp_path, **kwargs)


def test_cli_schedule_commands(tmp_path: Path, capsys) -> None:
    registry = operational_registry(tmp_path)
    with (
        patch("bossmode.cli.Registry.open_for_command", return_value=registry),
        patch("bossmode.cli.install_schedule") as mock_install,
        patch("bossmode.cli.get_schedule_status") as mock_status,
        patch("bossmode.cli.uninstall_schedule") as mock_uninstall,
    ):
        mock_install.return_value = {"job_id": "test_job", "status": "installed"}
        mock_status.return_value = {"job_id": "test_job", "status": "loaded"}
        mock_uninstall.return_value = {"job_id": "test_job", "status": "uninstalled"}

        # install
        assert main(["schedule", "install", "--interval", "1800"]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "installed"
        assert out["registry_id"] == "registry_schedule_test"
        mock_install.assert_called_once()
        assert mock_install.call_args.args[0] == tmp_path

        # status
        assert main(["schedule", "status"]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "loaded"
        assert out["registry_id"] == "registry_schedule_test"
        mock_status.assert_called_once_with(tmp_path, log_path=None)

        # uninstall
        assert main(["schedule", "uninstall"]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "uninstalled"
        assert out["registry_id"] == "registry_schedule_test"
        mock_uninstall.assert_called_once_with(tmp_path)


def test_cli_serializes_scheduler_errors(tmp_path: Path, capsys) -> None:
    registry = operational_registry(tmp_path)
    with (
        patch("bossmode.cli.Registry.open_for_command", return_value=registry),
        patch("bossmode.cli.install_schedule", side_effect=SchedulerError("scheduler failed")),
    ):
        assert main(["schedule", "install"]) == 2
    assert json.loads(capsys.readouterr().err) == {"error": "scheduler failed"}
