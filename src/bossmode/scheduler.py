from __future__ import annotations

import hashlib
import os
import plistlib
import shlex
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_LOG_PATH = Path(".bossmode/logs/schedule.log")
MAX_LOG_BYTES = 5 * 1024 * 1024  # 5 MiB
SCHEDULE_TARGETS = {"maintenance", "reconcile"}


class SchedulerError(RuntimeError):
    """Raised when an OS scheduling operation fails."""


def run_command(arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
    try:
        return subprocess.run(arguments, **kwargs)
    except OSError as error:
        raise SchedulerError(f"{arguments[0]} invocation failed: {error}") from error


def get_repo_hash(repo_dir: Path) -> str:
    """Generate a deterministic 8-char SHA256 hash of the canonical repository path."""
    return hashlib.sha256(str(repo_dir.resolve()).encode("utf-8")).hexdigest()[:8]


def resolve_uv_path() -> str:
    """Resolve the absolute path to the uv binary."""
    found = shutil.which("uv")
    if found:
        return str(Path(found).resolve())

    home = Path.home()
    candidates = [
        home / ".cargo" / "bin" / "uv",
        home / ".local" / "bin" / "uv",
        Path("/opt/homebrew/bin/uv"),
        Path("/usr/local/bin/uv"),
        Path("/usr/bin/uv"),
    ]
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate.resolve())

    raise SchedulerError("uv executable not found")


def resolve_bossmode_command() -> list[str]:
    """Return an executable command for the active Bossmode environment."""
    try:
        return [resolve_uv_path(), "run", "bossmode"]
    except SchedulerError:
        return [str(Path(sys.executable).resolve()), "-m", "bossmode.cli"]


def scheduled_command(target: str) -> list[str]:
    if target not in SCHEDULE_TARGETS:
        raise SchedulerError(f"unsupported schedule target: {target}")
    # Always name the target explicitly. `reconcile` used to be scheduled as a
    # naked `bossmode`, so the installed crontab or plist entry did not say
    # which target an operator had asked for.
    return [*resolve_bossmode_command(), target]


def rotate_log_if_needed(log_path: Path, max_bytes: int = MAX_LOG_BYTES) -> None:
    """Rotate log file if it exceeds max_bytes."""
    try:
        if log_path.is_file() and log_path.stat().st_size >= max_bytes:
            backup = log_path.with_name(f"{log_path.name}.old")
            if backup.exists():
                backup.unlink()
            log_path.rename(backup)
    except OSError:
        pass


def is_macos() -> bool:
    return sys.platform == "darwin"


def is_linux() -> bool:
    return sys.platform.startswith("linux")


def validate_cron_expression(expression: str) -> str:
    if "\n" in expression or "\r" in expression or len(expression.split()) != 5:
        raise SchedulerError("cron expression must contain exactly five fields on one line")
    return expression


class LaunchdAdapter:
    """Manages macOS user LaunchAgents."""

    @staticmethod
    def get_label(repo_hash: str) -> str:
        return f"com.bossmode.{repo_hash}"

    @classmethod
    def get_plist_path(cls, repo_hash: str) -> Path:
        return Path.home() / "Library" / "LaunchAgents" / f"{cls.get_label(repo_hash)}.plist"

    @classmethod
    def generate_plist(
        cls,
        repo_dir: Path,
        target: str,
        interval_seconds: int,
        log_path: Path,
    ) -> dict[str, Any]:
        repo_hash = get_repo_hash(repo_dir)
        args = scheduled_command(target)
        bin_dir = str(Path(args[0]).parent)
        env_path = os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
        if bin_dir not in env_path:
            env_path = f"{bin_dir}:{env_path}"

        return {
            "Label": cls.get_label(repo_hash),
            "ProgramArguments": args,
            "WorkingDirectory": str(repo_dir.resolve()),
            "StartInterval": max(60, interval_seconds),
            "StandardOutPath": str(log_path.resolve()),
            "StandardErrorPath": str(log_path.resolve()),
            "EnvironmentVariables": {
                "PATH": env_path,
            },
            "RunAtLoad": False,
        }

    @classmethod
    def install(
        cls,
        repo_dir: Path,
        target: str,
        interval_seconds: int,
        log_path: Path,
    ) -> dict[str, Any]:
        repo_hash = get_repo_hash(repo_dir)
        label = cls.get_label(repo_hash)
        plist_path = cls.get_plist_path(repo_hash)
        plist_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.parent.mkdir(parents=True, exist_ok=True)

        # Unload existing if loaded
        cls.uninstall(repo_dir)

        rotate_log_if_needed(log_path)

        plist_data = cls.generate_plist(repo_dir, target, interval_seconds, log_path)
        with plist_path.open("wb") as f:
            plistlib.dump(plist_data, f)

        # Try modern launchctl bootstrap first, fallback to load
        uid = os.getuid() if hasattr(os, "getuid") else 501
        res = run_command(
            ["launchctl", "bootstrap", f"gui/{uid}", str(plist_path)],
            capture_output=True,
            text=True,
        )
        if res.returncode != 0:
            fallback = run_command(
                ["launchctl", "load", str(plist_path)],
                capture_output=True,
                text=True,
            )
            if fallback.returncode != 0:
                plist_path.unlink(missing_ok=True)
                raise SchedulerError(
                    "launchd install failed: "
                    f"bootstrap={res.stderr.strip() or res.returncode}; "
                    f"load={fallback.stderr.strip() or fallback.returncode}"
                )

        return {
            "platform": "macos_launchd",
            "job_id": label,
            "plist_path": str(plist_path),
            "target": target,
            "interval_seconds": interval_seconds,
            "log_path": str(log_path),
            "status": "installed",
        }

    @classmethod
    def status(cls, repo_dir: Path, log_path: Path) -> dict[str, Any]:
        repo_hash = get_repo_hash(repo_dir)
        label = cls.get_label(repo_hash)
        plist_path = cls.get_plist_path(repo_hash)
        installed = plist_path.is_file()

        loaded = False
        if installed:
            uid = os.getuid() if hasattr(os, "getuid") else 501
            res = run_command(
                ["launchctl", "print", f"gui/{uid}/{label}"],
                capture_output=True,
                text=True,
            )
            if res.returncode == 0:
                loaded = True
            else:
                list_res = run_command(["launchctl", "list", label], capture_output=True)
                loaded = list_res.returncode == 0

        last_log_mtime = None
        log_size_bytes = 0
        if log_path.is_file():
            stat = log_path.stat()
            log_size_bytes = stat.st_size
            last_log_mtime = datetime.fromtimestamp(stat.st_mtime, UTC).isoformat()

        return {
            "platform": "macos_launchd",
            "job_id": label,
            "installed": installed,
            "loaded": loaded,
            "plist_path": str(plist_path),
            "log_path": str(log_path),
            "log_size_bytes": log_size_bytes,
            "last_log_activity": last_log_mtime,
            "status": "loaded"
            if (installed and loaded)
            else ("installed" if installed else "not_installed"),
        }

    @classmethod
    def uninstall(cls, repo_dir: Path) -> dict[str, Any]:
        repo_hash = get_repo_hash(repo_dir)
        label = cls.get_label(repo_hash)
        plist_path = cls.get_plist_path(repo_hash)

        uid = os.getuid() if hasattr(os, "getuid") else 501
        bootout = run_command(
            ["launchctl", "bootout", f"gui/{uid}/{label}"],
            capture_output=True,
            text=True,
        )
        if plist_path.is_file() and bootout.returncode != 0:
            unload = run_command(
                ["launchctl", "unload", str(plist_path)],
                capture_output=True,
                text=True,
            )
            if unload.returncode != 0:
                raise SchedulerError(
                    "launchd uninstall failed: "
                    f"bootout={bootout.stderr.strip() or bootout.returncode}; "
                    f"unload={unload.stderr.strip() or unload.returncode}"
                )
        elif not plist_path.is_file() and bootout.returncode != 0:
            probe = run_command(
                ["launchctl", "print", f"gui/{uid}/{label}"],
                capture_output=True,
                text=True,
            )
            if probe.returncode == 0:
                raise SchedulerError("launchd uninstall failed: job remains loaded")

        if plist_path.is_file():
            plist_path.unlink(missing_ok=True)

        return {
            "platform": "macos_launchd",
            "job_id": label,
            "plist_path": str(plist_path),
            "status": "uninstalled",
        }


class CrontabAdapter:
    """Manages Linux/Unix crontab entries with tagged fences."""

    @staticmethod
    def get_tag_begin(repo_hash: str) -> str:
        return f"# BEGIN BOSSMODE {repo_hash}"

    @staticmethod
    def get_tag_end(repo_hash: str) -> str:
        return f"# END BOSSMODE {repo_hash}"

    @staticmethod
    def interval_to_cron(interval_seconds: int) -> str:
        if interval_seconds < 60:
            raise SchedulerError("cron interval must be at least 60 seconds")
        if interval_seconds == 60:
            return "* * * * *"
        if interval_seconds < 3600:
            if interval_seconds % 60:
                raise SchedulerError("cron interval must be an exact number of minutes")
            mins = interval_seconds // 60
            if 60 % mins:
                raise SchedulerError("cron interval must divide evenly into one hour")
            return f"*/{mins} * * * *"
        if interval_seconds == 3600:
            return "0 * * * *"
        if interval_seconds == 86400:
            return "0 0 * * *"
        if interval_seconds > 86400 or interval_seconds % 3600:
            raise SchedulerError("cron interval must be an exact number of hours up to 24")
        hours = interval_seconds // 3600
        if 24 % hours:
            raise SchedulerError("cron interval must divide evenly into one day")
        return f"0 */{hours} * * *"

    @classmethod
    def read_crontab(cls) -> str:
        res = run_command(["crontab", "-l"], capture_output=True, text=True)
        if res.returncode == 0:
            return res.stdout
        if res.returncode == 1 and (not res.stderr.strip() or "no crontab" in res.stderr.lower()):
            return ""
        raise SchedulerError(f"crontab read failed: {res.stderr.strip() or res.returncode}")

    @classmethod
    def write_crontab(cls, content: str) -> None:
        content = content.strip() + "\n" if content.strip() else ""
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as tf:
            tf.write(content)
            temp_name = tf.name
        try:
            res = run_command(["crontab", temp_name], capture_output=True, text=True)
            if res.returncode != 0:
                raise SchedulerError(f"crontab write failed: {res.stderr.strip()}")
        finally:
            Path(temp_name).unlink(missing_ok=True)

    @staticmethod
    def remove_managed_block(current: str, tag_begin: str, tag_end: str) -> list[str]:
        lines: list[str] = []
        in_block = False
        for line in current.splitlines():
            if line.strip() == tag_begin:
                if in_block:
                    raise SchedulerError("crontab contains nested Bossmode schedule markers")
                in_block = True
                continue
            if line.strip() == tag_end:
                if not in_block:
                    raise SchedulerError("crontab contains an unmatched Bossmode end marker")
                in_block = False
                continue
            if not in_block:
                lines.append(line)
        if in_block:
            raise SchedulerError("crontab contains an unmatched Bossmode begin marker")
        return lines

    @staticmethod
    def managed_command(current: str, tag_begin: str, tag_end: str) -> str | None:
        in_block = False
        found_block = False
        commands: list[str] = []
        for line in current.splitlines():
            if line.strip() == tag_begin:
                if in_block or found_block:
                    raise SchedulerError("crontab contains duplicate Bossmode schedule markers")
                in_block = True
                found_block = True
                continue
            if line.strip() == tag_end:
                if not in_block:
                    raise SchedulerError("crontab contains an unmatched Bossmode end marker")
                in_block = False
                continue
            if in_block and line.strip():
                commands.append(line.strip())
        if in_block:
            raise SchedulerError("crontab contains an unmatched Bossmode begin marker")
        if not found_block:
            return None
        if len(commands) != 1:
            raise SchedulerError("crontab Bossmode block must contain exactly one command")
        return commands[0]

    @classmethod
    def install(
        cls,
        repo_dir: Path,
        target: str,
        interval_seconds: int,
        cron_expr: str | None,
        log_path: Path,
    ) -> dict[str, Any]:
        repo_hash = get_repo_hash(repo_dir)
        tag_begin = cls.get_tag_begin(repo_hash)
        tag_end = cls.get_tag_end(repo_hash)
        log_path.parent.mkdir(parents=True, exist_ok=True)

        rotate_log_if_needed(log_path)

        expr = (
            validate_cron_expression(cron_expr)
            if cron_expr
            else cls.interval_to_cron(interval_seconds)
        )
        command = scheduled_command(target)
        bin_dir = str(Path(command[0]).parent)
        env_path = os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
        if bin_dir not in env_path:
            env_path = f"{bin_dir}:{env_path}"

        cron_line = (
            f"{expr} cd {shlex.quote(str(repo_dir.resolve()))} && "
            f"PATH={shlex.quote(env_path)} {shlex.join(command)} >> "
            f"{shlex.quote(str(log_path.resolve()))} 2>&1"
        )

        block = f"{tag_begin}\n{cron_line}\n{tag_end}"

        current = cls.read_crontab()
        lines = cls.remove_managed_block(current, tag_begin, tag_end)

        lines.append(block)
        cls.write_crontab("\n".join(lines))

        return {
            "platform": "unix_crontab",
            "job_id": f"cron_{repo_hash}",
            "cron_expression": expr,
            "target": target,
            "log_path": str(log_path),
            "status": "installed",
        }

    @classmethod
    def status(cls, repo_dir: Path, log_path: Path) -> dict[str, Any]:
        repo_hash = get_repo_hash(repo_dir)
        tag_begin = cls.get_tag_begin(repo_hash)
        tag_end = cls.get_tag_end(repo_hash)
        current = cls.read_crontab()

        cron_line = cls.managed_command(current, tag_begin, tag_end)
        installed = cron_line is not None

        last_log_mtime = None
        log_size_bytes = 0
        if log_path.is_file():
            stat = log_path.stat()
            log_size_bytes = stat.st_size
            last_log_mtime = datetime.fromtimestamp(stat.st_mtime, UTC).isoformat()

        return {
            "platform": "unix_crontab",
            "job_id": f"cron_{repo_hash}",
            "installed": installed,
            "cron_line": cron_line,
            "log_path": str(log_path),
            "log_size_bytes": log_size_bytes,
            "last_log_activity": last_log_mtime,
            "status": "registered" if installed else "not_installed",
        }

    @classmethod
    def uninstall(cls, repo_dir: Path) -> dict[str, Any]:
        repo_hash = get_repo_hash(repo_dir)
        tag_begin = cls.get_tag_begin(repo_hash)
        tag_end = cls.get_tag_end(repo_hash)
        current = cls.read_crontab()

        lines = cls.remove_managed_block(current, tag_begin, tag_end)

        cls.write_crontab("\n".join(lines))
        return {
            "platform": "unix_crontab",
            "job_id": f"cron_{repo_hash}",
            "status": "uninstalled",
        }


def install_schedule(
    repo_dir: str | Path = ".",
    *,
    target: str = "maintenance",
    interval_seconds: int = 3600,
    cron_expr: str | None = None,
    log_path: str | Path | None = None,
) -> dict[str, Any]:
    if target not in SCHEDULE_TARGETS:
        raise SchedulerError(f"unsupported schedule target: {target}")
    if interval_seconds < 60:
        raise SchedulerError("schedule interval must be at least 60 seconds")
    repo = Path(repo_dir).resolve()
    log = Path(log_path) if log_path else repo / DEFAULT_LOG_PATH
    if is_macos():
        if cron_expr is not None:
            raise SchedulerError("custom cron expressions are not supported by launchd")
        return LaunchdAdapter.install(repo, target, interval_seconds, log)
    if is_linux():
        return CrontabAdapter.install(repo, target, interval_seconds, cron_expr, log)
    raise SchedulerError(f"unsupported scheduler platform: {sys.platform}")


def get_schedule_status(
    repo_dir: str | Path = ".",
    *,
    log_path: str | Path | None = None,
) -> dict[str, Any]:
    repo = Path(repo_dir).resolve()
    log = Path(log_path) if log_path else repo / DEFAULT_LOG_PATH
    if is_macos():
        return LaunchdAdapter.status(repo, log)
    if is_linux():
        return CrontabAdapter.status(repo, log)
    raise SchedulerError(f"unsupported scheduler platform: {sys.platform}")


def uninstall_schedule(
    repo_dir: str | Path = ".",
) -> dict[str, Any]:
    repo = Path(repo_dir).resolve()
    if is_macos():
        return LaunchdAdapter.uninstall(repo)
    if is_linux():
        return CrontabAdapter.uninstall(repo)
    raise SchedulerError(f"unsupported scheduler platform: {sys.platform}")
