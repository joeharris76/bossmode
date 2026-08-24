from __future__ import annotations

import errno
import os
import secrets
import stat
import sys
from importlib import resources
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

SKILL_RELATIVE_PATH = Path(".agents/skills/bossmode/SKILL.md")
NEXT_PROMPT = (
    "Use the Bossmode skill for this project. Turn my request into one bounded task with "
    "explicit success criteria and permission limits, record it before delegation, and require "
    "independent evidence before marking it complete: [describe the outcome you want]"
)


class BootstrapError(RuntimeError):
    """Raised when a project cannot be initialized without overwriting user content."""


def packaged_skill_bytes() -> bytes:
    skill = resources.files("bossmode").joinpath("skills", "bossmode", "SKILL.md")
    if not skill.is_file():
        raise BootstrapError("the installed distribution does not contain the Bossmode skill")
    return skill.read_bytes()


def _package_version() -> str:
    try:
        return version("bossmode")
    except PackageNotFoundError as error:
        raise BootstrapError("Bossmode package metadata is unavailable") from error


def _bootstrap_error(action: str, path: Path, error: OSError) -> BootstrapError:
    detail = error.strerror or str(error)
    return BootstrapError(f"cannot {action} {path}: {detail}")


def _close_descriptor(descriptor: int, path: Path) -> None:
    active_error = sys.exception()
    try:
        os.close(descriptor)
    except OSError as error:
        close_error = _bootstrap_error("close", path, error)
        if active_error is None:
            raise close_error from error
        active_error.add_note(str(close_error))


def _verify_path_binding(
    parent_descriptor: int, destination: Path, metadata: os.stat_result
) -> None:
    try:
        current = os.stat(destination.name, dir_fd=parent_descriptor, follow_symlinks=False)
        visible = os.stat(destination, follow_symlinks=False)
    except OSError as error:
        raise BootstrapError(f"skill path changed during installation: {destination}") from error
    expected_identity = (metadata.st_dev, metadata.st_ino)
    if (
        not stat.S_ISREG(current.st_mode)
        or (current.st_dev, current.st_ino) != expected_identity
        or not stat.S_ISREG(visible.st_mode)
        or (visible.st_dev, visible.st_ino) != expected_identity
    ):
        raise BootstrapError(f"skill path changed during installation: {destination}")


def _read_at(parent_descriptor: int, destination: Path, expected: bytes) -> str | None:
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(destination.name, flags, dir_fd=parent_descriptor)
    except FileNotFoundError:
        return None
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise BootstrapError(f"refuse to replace symlinked skill: {destination}") from error
        raise _bootstrap_error("inspect", destination, error) from error

    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise BootstrapError(f"skill path is not a regular file: {destination}")
        chunks: list[bytes] = []
        remaining = len(expected) + 1
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        actual = b"".join(chunks)
        _verify_path_binding(parent_descriptor, destination, metadata)
    except OSError as error:
        raise _bootstrap_error("read", destination, error) from error
    finally:
        _close_descriptor(descriptor, destination)

    if actual != expected:
        raise BootstrapError(f"refuse to overwrite conflicting skill: {destination}")
    return "already_installed"


def _open_skill_parent(project: Path) -> int:
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(project, directory_flags)
    except OSError as error:
        raise _bootstrap_error("open project directory", project, error) from error

    current = project
    try:
        for part in SKILL_RELATIVE_PATH.parent.parts:
            current /= part
            try:
                os.mkdir(part, mode=0o755, dir_fd=descriptor)
            except FileExistsError:
                pass
            except OSError as error:
                raise _bootstrap_error("create directory", current, error) from error

            try:
                child_descriptor = os.open(part, directory_flags, dir_fd=descriptor)
            except OSError as error:
                try:
                    component = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
                except OSError:
                    component = None
                if error.errno == errno.ELOOP or (
                    component is not None and stat.S_ISLNK(component.st_mode)
                ):
                    raise BootstrapError(
                        f"refuse to install through symlinked directory: {current}"
                    ) from error
                if component is not None and not stat.S_ISDIR(component.st_mode):
                    raise BootstrapError(
                        f"skill parent path is not a directory: {current}"
                    ) from error
                raise _bootstrap_error("open skill directory", current, error) from error
            _close_descriptor(descriptor, current.parent)
            descriptor = child_descriptor
    except Exception:
        _close_descriptor(descriptor, current)
        raise
    return descriptor


def _publish_at(parent_descriptor: int, destination: Path, expected: bytes) -> str:
    temporary_name = f".{destination.name}.tmp-{secrets.token_hex(12)}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(temporary_name, flags, 0o644, dir_fd=parent_descriptor)
        with os.fdopen(os.dup(descriptor), "wb") as skill_file:
            skill_file.write(expected)
            skill_file.flush()
            os.fsync(skill_file.fileno())
        temporary_metadata = os.fstat(descriptor)
        if not stat.S_ISREG(temporary_metadata.st_mode):
            raise BootstrapError(f"temporary skill is not a regular file: {destination}")

        try:
            os.link(
                temporary_name,
                destination.name,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileExistsError:
            status = _read_at(parent_descriptor, destination, expected)
            if status is None:  # pragma: no cover - a concurrent unlink is harmless to retry
                return _publish_at(parent_descriptor, destination, expected)
            return status
        os.stat(destination.name, dir_fd=parent_descriptor, follow_symlinks=False)
        _verify_path_binding(parent_descriptor, destination, temporary_metadata)
        os.fsync(parent_descriptor)
        return "installed"
    except BootstrapError:
        raise
    except OSError as error:
        raise _bootstrap_error("install skill at", destination, error) from error
    finally:
        active_error = sys.exception()
        if descriptor is not None:
            _close_descriptor(descriptor, destination)
        try:
            os.unlink(temporary_name, dir_fd=parent_descriptor)
        except FileNotFoundError:
            pass
        except OSError as error:
            cleanup_error = _bootstrap_error("remove temporary skill for", destination, error)
            if active_error is None:
                raise cleanup_error from error
            active_error.add_note(str(cleanup_error))


def install_project_skill(project_dir: str | Path = ".") -> dict[str, Any]:
    project = Path(project_dir).resolve()
    if not project.exists():
        raise BootstrapError(f"project directory does not exist: {project}")
    if not project.is_dir():
        raise BootstrapError(f"project path is not a directory: {project}")

    expected = packaged_skill_bytes()
    destination = project / SKILL_RELATIVE_PATH
    parent_descriptor = _open_skill_parent(project)
    try:
        status = _read_at(parent_descriptor, destination, expected)
        if status is None:
            status = _publish_at(parent_descriptor, destination, expected)
    finally:
        _close_descriptor(parent_descriptor, destination.parent)

    return {
        "project_dir": str(project),
        "skill_path": str(destination),
        "skill_version": _package_version(),
        "status": status,
        "next_prompt": NEXT_PROMPT,
    }
