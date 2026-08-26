from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MAX_TURN_RESULT_BYTES = 1_048_576
MAX_ARTIFACT_SIZE_BYTES = 10_485_760
MAX_ARTIFACTS_PER_RUN = 100
ACCEPTED_DISPOSITIONS = frozenset({"accepted-commit", "central-copy"})
FORBIDDEN_PATH_COMPONENTS = frozenset({".git", ".claude", "tmp", "temp"})


class ArtifactError(RuntimeError):
    """Raised when artifact validation, resolution, or adoption fails."""


@dataclass(frozen=True)
class ArtifactRecord:
    path: str
    kind: str
    disposition: str
    digest: str
    size_bytes: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "kind": self.kind,
            "disposition": self.disposition,
            "digest": self.digest,
            "size_bytes": self.size_bytes,
        }


def validate_relative_artifact_path(path: str) -> str:
    """Validate that path is a normalized repository-relative path without traversal."""
    if not isinstance(path, str):
        raise ArtifactError("artifact path must be a string")
    stripped = path.strip()
    if not stripped:
        raise ArtifactError("artifact path cannot be empty")
    if os.path.isabs(stripped) or stripped.startswith(("/", "\\")):
        raise ArtifactError(f"artifact path must be repository-relative, not absolute: {stripped}")
    parts = Path(stripped).parts
    if not parts or any(part in {"", "."} for part in parts):
        raise ArtifactError(f"artifact path contains empty components: {stripped}")
    if ".." in parts:
        raise ArtifactError(f"artifact path cannot contain directory traversal: {stripped}")
    if parts[0] in FORBIDDEN_PATH_COMPONENTS or any("worktree" in part.lower() for part in parts):
        raise ArtifactError(f"artifact path points to transient or internal directory: {stripped}")
    return Path(stripped).as_posix()


def canonical_result_digest(result: dict[str, Any]) -> str:
    """Compute canonical SHA-256 digest of result payload dictionary."""
    canonical_json = json.dumps(result, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


class CentralArtifactStore:
    """Manages central storage and secure resolution of durable run artifacts."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()

    def _open_directory_descriptor(
        self,
        base_dir: Path,
        relative_parts: tuple[str, ...],
        *,
        create: bool = False,
    ) -> int:
        """Walk path components relative to base_dir securely with O_NOFOLLOW."""
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_flags |= getattr(os, "O_CLOEXEC", 0)
        directory_flags |= getattr(os, "O_NOFOLLOW", 0)

        current_descriptor = os.open(base_dir, directory_flags)
        try:
            for component in relative_parts:
                if create:
                    with contextlib.suppress(FileExistsError):
                        os.mkdir(component, mode=0o700, dir_fd=current_descriptor)
                try:
                    next_descriptor = os.open(component, directory_flags, dir_fd=current_descriptor)
                except OSError as error:
                    if error.errno in {errno.ELOOP, errno.ENOTDIR}:
                        try:
                            stat_res = os.stat(
                                component,
                                dir_fd=current_descriptor,
                                follow_symlinks=False,
                            )
                        except OSError:
                            stat_res = None
                        if stat_res is not None and stat.S_ISLNK(stat_res.st_mode):
                            raise ArtifactError(
                                f"artifact path component cannot be a symlink: {component}"
                            ) from error
                    raise ArtifactError(
                        f"cannot open directory component {component}: {error.strerror}"
                    ) from error
                os.close(current_descriptor)
                current_descriptor = next_descriptor
            return current_descriptor
        except Exception:
            os.close(current_descriptor)
            raise

    def open_read_descriptor(
        self,
        relative_path: str,
        *,
        base_dir: Path | None = None,
    ) -> int:
        """Open a regular file securely relative to base_dir using O_NOFOLLOW."""
        normalized = validate_relative_artifact_path(relative_path)
        base = (base_dir or self.root).resolve()
        parts = Path(normalized).parts
        parent_parts = parts[:-1]
        filename = parts[-1]

        parent_fd = self._open_directory_descriptor(base, parent_parts, create=False)
        try:
            flags = os.O_RDONLY
            flags |= getattr(os, "O_BINARY", 0)
            flags |= getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            flags |= getattr(os, "O_NONBLOCK", 0)
            file_fd = os.open(filename, flags, dir_fd=parent_fd)
            try:
                stat_res = os.fstat(file_fd)
                if not stat.S_ISREG(stat_res.st_mode):
                    raise ArtifactError(f"artifact must be a regular file: {relative_path}")
                return file_fd
            except Exception:
                os.close(file_fd)
                raise
        except OSError as error:
            if error.errno == errno.ELOOP:
                raise ArtifactError(
                    f"artifact file cannot be a symlink: {relative_path}"
                ) from error
            raise ArtifactError(
                f"artifact file unavailable at {relative_path}: {error.strerror}"
            ) from error
        finally:
            os.close(parent_fd)

    def read_bounded_bytes(
        self,
        relative_path: str,
        *,
        max_bytes: int = MAX_ARTIFACT_SIZE_BYTES,
        base_dir: Path | None = None,
    ) -> bytes:
        """Read and return bounded content of an artifact securely."""
        fd = self.open_read_descriptor(relative_path, base_dir=base_dir)
        try:
            with os.fdopen(fd, "rb") as file_obj:
                data = file_obj.read(max_bytes + 1)
                if len(data) > max_bytes:
                    raise ArtifactError(
                        f"artifact {relative_path} exceeds maximum size of {max_bytes} bytes"
                    )
                return data
        except Exception:
            raise

    def adopt_file_to_central(
        self,
        source_relative_path: str,
        *,
        source_base_dir: Path,
        destination_relative_path: str | None = None,
        max_bytes: int = MAX_ARTIFACT_SIZE_BYTES,
    ) -> ArtifactRecord:
        """Copy artifact from source worktree into central storage with full security."""
        norm_src = validate_relative_artifact_path(source_relative_path)
        norm_dest = validate_relative_artifact_path(destination_relative_path or norm_src)

        data = self.read_bounded_bytes(norm_src, max_bytes=max_bytes, base_dir=source_base_dir)
        digest = hashlib.sha256(data).hexdigest()

        dest_parts = Path(norm_dest).parts
        parent_parts = dest_parts[:-1]
        filename = dest_parts[-1]

        self.root.mkdir(parents=True, exist_ok=True)
        dest_parent_fd = self._open_directory_descriptor(self.root, parent_parts, create=True)
        try:
            temp_name = f".{filename}.partial.{digest[:8]}"
            creation_flags = (
                os.O_RDWR
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            try:
                temp_fd = os.open(temp_name, creation_flags, 0o600, dir_fd=dest_parent_fd)
            except FileExistsError:
                os.unlink(temp_name, dir_fd=dest_parent_fd)
                temp_fd = os.open(temp_name, creation_flags, 0o600, dir_fd=dest_parent_fd)

            try:
                view = memoryview(data)
                while view:
                    written = os.write(temp_fd, view)
                    view = view[written:]
                os.fchmod(temp_fd, 0o600)
                os.fsync(temp_fd)
            finally:
                os.close(temp_fd)

            with contextlib.suppress(FileNotFoundError):
                os.unlink(filename, dir_fd=dest_parent_fd)
            os.link(
                temp_name,
                filename,
                src_dir_fd=dest_parent_fd,
                dst_dir_fd=dest_parent_fd,
                follow_symlinks=False,
            )
            os.unlink(temp_name, dir_fd=dest_parent_fd)
            os.fsync(dest_parent_fd)
        finally:
            os.close(dest_parent_fd)

        return ArtifactRecord(
            path=norm_dest,
            kind="file",
            disposition="central-copy",
            digest=digest,
            size_bytes=len(data),
        )

    def validate_result_envelope(
        self,
        raw: bytes | str,
        *,
        expected_turn_id: str,
        expected_run_id: str | None = None,
        expected_task_id: str | None = None,
        expected_prompt_digest: str | None = None,
        expected_registry_id: str | None = None,
        expected_accepted_head: str | None = None,
        expected_summary: str | None = None,
        source_base_dir: Path | None = None,
    ) -> dict[str, Any]:
        """Validate result envelope structure, fields, size, and binding claims."""
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        if len(raw) > MAX_TURN_RESULT_BYTES:
            raise ArtifactError(
                f"turn result exceeds maximum allowed size of {MAX_TURN_RESULT_BYTES} bytes"
            )
        try:
            result = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            stripped = raw.lstrip()
            if stripped.startswith(b"```"):
                raise ArtifactError(
                    "turn result contains markdown code fence instead of raw JSON"
                ) from error
            raise ArtifactError("turn result is not valid JSON") from error

        if not isinstance(result, dict):
            raise ArtifactError("turn result must be a JSON object")

        required = {"turn_id", "outcome", "summary", "artifacts"}
        missing = sorted(required - result.keys())
        if missing:
            raise ArtifactError(f"turn result is missing fields: {', '.join(missing)}")

        if result["turn_id"] != expected_turn_id:
            raise ArtifactError(
                f"turn result ID mismatch: expected={expected_turn_id}, actual={result['turn_id']}"
            )
        if expected_run_id is not None and result.get("run_id") not in (None, expected_run_id):
            raise ArtifactError(
                "turn result run_id mismatch: "
                f"expected={expected_run_id}, actual={result.get('run_id')}"
            )
        if expected_task_id is not None and result.get("task_id") not in (None, expected_task_id):
            raise ArtifactError(
                "turn result task_id mismatch: "
                f"expected={expected_task_id}, actual={result.get('task_id')}"
            )
        if expected_prompt_digest is not None and result.get("prompt_digest") not in (
            None,
            expected_prompt_digest,
        ):
            raise ArtifactError(
                f"turn result prompt_digest mismatch: expected={expected_prompt_digest}, "
                f"actual={result.get('prompt_digest')}"
            )
        if expected_registry_id is not None and result.get("registry_id") not in (
            None,
            expected_registry_id,
        ):
            raise ArtifactError(
                f"turn result registry_id mismatch: expected={expected_registry_id}, "
                f"actual={result.get('registry_id')}"
            )
        if expected_accepted_head is not None and result.get("accepted_head") not in (
            None,
            expected_accepted_head,
        ):
            raise ArtifactError(
                f"turn result accepted_head mismatch: expected={expected_accepted_head}, "
                f"actual={result.get('accepted_head')}"
            )
        if result["outcome"] != "succeeded":
            raise ArtifactError("successful turn result must have outcome succeeded")
        if not isinstance(result["summary"], str) or not result["summary"].strip():
            raise ArtifactError("turn result summary must be a non-empty string")
        if expected_summary is not None and expected_summary != result["summary"]:
            raise ArtifactError("turn result summary does not match --summary")

        artifacts = result["artifacts"]
        if not isinstance(artifacts, list):
            raise ArtifactError("turn result artifacts must be a list")
        if len(artifacts) > MAX_ARTIFACTS_PER_RUN:
            raise ArtifactError(
                f"turn result contains {len(artifacts)} artifacts, "
                f"exceeding maximum {MAX_ARTIFACTS_PER_RUN}"
            )

        validated_artifacts = []
        for item in artifacts:
            if not isinstance(item, dict):
                raise ArtifactError("artifact entries must be JSON objects")
            path = item.get("path")
            kind = item.get("kind")
            if not isinstance(path, str) or not path.strip():
                raise ArtifactError("artifact path must be a non-empty string")
            if not isinstance(kind, str) or not kind.strip():
                raise ArtifactError("artifact kind must be a non-empty string")
            if os.path.isabs(path) and source_base_dir is not None:
                try:
                    path = str(Path(path).resolve().relative_to(Path(source_base_dir).resolve()))
                except ValueError as error:
                    raise ArtifactError(
                        f"artifact path must be repository-relative, not absolute: {path}"
                    ) from error
            norm_path = validate_relative_artifact_path(path)
            disposition = item.get("disposition", "accepted-commit")
            if disposition not in ACCEPTED_DISPOSITIONS:
                raise ArtifactError(
                    f"unsupported artifact disposition: {disposition}; "
                    f"must be one of {sorted(ACCEPTED_DISPOSITIONS)}"
                )
            entry: dict[str, Any] = {
                "path": norm_path,
                "kind": kind.strip(),
                "disposition": disposition,
            }
            if "digest" in item and item["digest"] is not None:
                entry["digest"] = str(item["digest"]).strip()
            if "size_bytes" in item and item["size_bytes"] is not None:
                entry["size_bytes"] = int(item["size_bytes"])
            validated_artifacts.append(entry)

        result["artifacts"] = validated_artifacts
        claimed_digest = result.pop("result_digest", None)
        result_digest = canonical_result_digest(result)
        if claimed_digest is not None and claimed_digest != result_digest:
            raise ArtifactError("turn result digest does not match canonical result")
        result["result_digest"] = result_digest
        return result

    def secure_and_adopt_run_artifacts(
        self,
        artifacts: list[dict[str, Any]],
        *,
        source_base_dir: Path,
    ) -> list[dict[str, Any]]:
        """Validate, adopt, and ensure durability of all declared run artifacts."""
        if not isinstance(artifacts, list):
            raise ArtifactError("artifacts must be a list")
        if len(artifacts) > MAX_ARTIFACTS_PER_RUN:
            raise ArtifactError(
                f"artifacts count {len(artifacts)} exceeds maximum {MAX_ARTIFACTS_PER_RUN}"
            )

        secured: list[dict[str, Any]] = []
        for item in artifacts:
            if not isinstance(item, dict):
                raise ArtifactError("run artifacts must contain non-empty path and kind strings")
            path = item.get("path")
            kind = item.get("kind")
            if not isinstance(path, str) or not path.strip():
                raise ArtifactError("run artifacts must contain non-empty path and kind strings")
            if not isinstance(kind, str) or not kind.strip():
                raise ArtifactError("run artifacts must contain non-empty path and kind strings")
            norm_path = validate_relative_artifact_path(path)
            disposition = item.get("disposition", "accepted-commit")
            if disposition not in ACCEPTED_DISPOSITIONS:
                raise ArtifactError(
                    f"unsupported artifact disposition: {disposition}; "
                    f"must be one of {sorted(ACCEPTED_DISPOSITIONS)}"
                )
            # Preserve the pre-adoption API for callers that supplied their own
            # legacy digest metadata and no source file is available locally.
            if "disposition" not in item and "sha256" in item:
                try:
                    descriptor = self.open_read_descriptor(norm_path, base_dir=source_base_dir)
                except ArtifactError as error:
                    if not any(
                        marker in str(error) for marker in ("unavailable at", "No such file")
                    ):
                        raise
                    legacy = dict(item)
                    legacy["path"] = norm_path
                    legacy["kind"] = kind.strip()
                    secured.append(legacy)
                    continue
                else:
                    os.close(descriptor)

            if disposition == "central-copy":
                adopted = self.adopt_file_to_central(
                    norm_path,
                    source_base_dir=source_base_dir,
                )
                secured.append(
                    {
                        "path": adopted.path,
                        "kind": kind.strip(),
                        "disposition": "central-copy",
                        "digest": adopted.digest,
                        "size_bytes": adopted.size_bytes,
                    }
                )
            else:
                # accepted-commit: if file exists, compute digest and size
                entry: dict[str, Any] = {
                    "path": norm_path,
                    "kind": kind.strip(),
                    "disposition": "accepted-commit",
                }
                try:
                    fd = self.open_read_descriptor(norm_path, base_dir=source_base_dir)
                    try:
                        stat_res = os.fstat(fd)
                        size = stat_res.st_size
                        if size > MAX_ARTIFACT_SIZE_BYTES:
                            raise ArtifactError(
                                f"artifact {norm_path} size {size} exceeds max "
                                f"{MAX_ARTIFACT_SIZE_BYTES}"
                            )
                        with os.fdopen(fd, "rb") as f:
                            digest = hashlib.file_digest(f, "sha256").hexdigest()
                        entry["digest"] = digest
                        entry["size_bytes"] = size
                    finally:
                        pass
                except ArtifactError as error:
                    if not any(
                        marker in str(error) for marker in ("unavailable at", "No such file")
                    ):
                        raise
                except OSError:
                    pass

                secured.append(entry)

        return secured
