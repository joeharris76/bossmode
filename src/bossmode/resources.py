from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

RESOURCE_KINDS = frozenset({"herdr_worker", "git_worktree", "git_branch"})
RESOURCE_STATES = frozenset({"reserved", "live", "retiring", "retired", "orphaned"})
TERMINAL_STATES = frozenset({"retired", "orphaned"})

HERDR_NAME_PATTERN = r"[a-z0-9][a-z0-9_-]{1,63}"
HERDR_WORKER_PATTERN = re.compile(f"^{HERDR_NAME_PATTERN}$")
GIT_BRANCH_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/\-]{0,254}$")

MAX_CANONICAL_KEY_BYTES = 1024


class ResourceError(RuntimeError):
    """Raised when resource validation or transition fails."""


@dataclass(frozen=True)
class ResourceRecord:
    id: str
    kind: str
    canonical_key: str
    owner_task_id: str | None
    owner_run_id: str | None
    owner_thread_id: str | None
    state: str
    creation_receipt: dict[str, Any] | None
    generation: int
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "canonical_key": self.canonical_key,
            "owner_task_id": self.owner_task_id,
            "owner_run_id": self.owner_run_id,
            "owner_thread_id": self.owner_thread_id,
            "state": self.state,
            "creation_receipt": self.creation_receipt,
            "generation": self.generation,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def validate_resource_kind(kind: str) -> str:
    if not isinstance(kind, str) or not kind.strip():
        raise ResourceError("resource kind must be a non-empty string")
    k = kind.strip()
    if k not in RESOURCE_KINDS:
        kinds = ", ".join(sorted(RESOURCE_KINDS))
        raise ResourceError(f"unsupported resource kind: {k}; must be one of [{kinds}]")
    return k


def validate_canonical_key(key: str) -> str:
    if not isinstance(key, str) or not key.strip():
        raise ResourceError("canonical key must be a non-empty string")
    k = key.strip()
    if len(k.encode()) > MAX_CANONICAL_KEY_BYTES:
        raise ResourceError(f"canonical key exceeds {MAX_CANONICAL_KEY_BYTES} bytes")
    if ".." in Path(k).parts:
        raise ResourceError(f"canonical key cannot contain traversal: {k}")
    return k


def canonical_key_for_herdr_worker(herdr_session: str, worker_name: str) -> str:
    if not isinstance(herdr_session, str) or not herdr_session.strip():
        raise ResourceError("herdr_session must be a non-empty string")
    if not HERDR_WORKER_PATTERN.fullmatch(worker_name):
        raise ResourceError(f"invalid Herdr worker name: {worker_name}")
    return f"herdr_worker:{herdr_session.strip()}/{worker_name.strip()}"


def canonical_key_for_git_worktree(path: str | Path) -> str:
    raw = str(path).strip() if isinstance(path, str) else str(path).strip()
    p = Path(raw)
    if not p.is_absolute():
        raise ResourceError(f"worktree path must be absolute: {path}")
    # Lexical normalize without following symlinks
    normalized = Path(raw)
    return f"git_worktree:{normalized.as_posix()}"


def canonical_key_for_git_branch(branch: str) -> str:
    if not isinstance(branch, str) or not branch.strip():
        raise ResourceError("branch name must be a non-empty string")
    b = branch.strip().removeprefix("refs/heads/")
    if not GIT_BRANCH_PATTERN.fullmatch(b):
        raise ResourceError(f"invalid branch name: {branch}")
    if b in {".", ".."} or ".." in b or b.startswith("/") or b.endswith("/"):
        raise ResourceError(f"invalid branch name: {branch}")
    return f"git_branch:refs/heads/{b}"


def validate_creation_receipt(kind: str, receipt: dict[str, Any] | None) -> dict[str, Any] | None:
    if receipt is None:
        return None
    if not isinstance(receipt, dict):
        raise ResourceError("creation receipt must be a JSON object")
    k = validate_resource_kind(kind)
    if k == "herdr_worker":
        for field in ("herdr_session", "worker_name", "agent_kind"):
            if not isinstance(receipt.get(field), str) or not receipt[field].strip():
                raise ResourceError(f"herdr_worker receipt requires non-empty {field}")
        # session tuple is optional at reserve time but if present must be complete
        session_fields = ("session_source", "session_agent", "session_ref_kind", "session_value")
        present = [f for f in session_fields if receipt.get(f) is not None]
        if present and len(present) != 4:
            raise ResourceError("herdr_worker session fields must be supplied together")
    elif k == "git_worktree":
        for field in ("path", "branch", "head_sha", "common_dir"):
            if not isinstance(receipt.get(field), str) or not receipt[field].strip():
                raise ResourceError(f"git_worktree receipt requires non-empty {field}")
    elif k == "git_branch":
        for field in ("branch", "head_sha"):
            if not isinstance(receipt.get(field), str) or not receipt[field].strip():
                raise ResourceError(f"git_branch receipt requires non-empty {field}")
    return receipt


# State machine: allowed transitions (from -> set(to))
RESOURCE_TRANSITIONS: dict[str, set[str]] = {
    "reserved": {"live", "retiring", "orphaned"},
    "live": {"retiring", "orphaned"},
    "retiring": {"retired", "orphaned"},
    "retired": set(),
    "orphaned": set(),
}

# Reconciliation observation kinds (read-only, does not mutate state)
RECONCILE_OBSERVATIONS = frozenset(
    {"missing", "changed", "ambiguous", "foreign", "already_gone", "healthy"}
)


def is_terminal_state(state: str) -> bool:
    return state in TERMINAL_STATES


def can_transition(from_state: str, to_state: str) -> bool:
    return to_state in RESOURCE_TRANSITIONS.get(from_state, set())
