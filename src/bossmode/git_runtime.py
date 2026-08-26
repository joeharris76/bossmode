"""Git worktree/branch lifecycle helpers for 2b.

This module is intentionally pure inventory + porcelain parsing until later
work units add reserve/create/reconcile/retire orchestration.  No branch or
worktree is created or deleted at import time.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

PROTECTED_BRANCHES = frozenset({"main", "master", "develop"})
LOCKED_MARKER = "locked"
PRUNABLE_MARKER = "prunable"


def _run_git(cwd: Path | str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )


def parse_worktree_porcelain(
    text: str,
) -> list[dict[str, Any]]:
    """Parse `git worktree list --porcelain` into entries.

    Each entry has at minimum ``path``, ``head`` and optionally ``branch`` and
    flags ``locked``/``prunable``/``detached``.  Missing required fields are
    preserved as ``None`` so callers can treat ambiguity explicitly.
    """
    entries: list[dict[str, Any]] = []
    for block in text.strip().split("\n\n") if text.strip() else []:
        cur: dict[str, Any] = {
            "path": None,
            "head": None,
            "branch": None,
            "locked": False,
            "prunable": False,
            "detached": False,
        }
        for line in block.splitlines():
            if line.startswith("worktree "):
                cur["path"] = line.removeprefix("worktree ").strip()
            elif line.startswith("HEAD "):
                cur["head"] = line.removeprefix("HEAD ").strip()
            elif line.startswith("branch "):
                ref = line.removeprefix("branch ").strip()
                cur["branch"] = (
                    ref.removeprefix("refs/heads/") if ref.startswith("refs/heads/") else ref
                )
            elif line == "detached":
                cur["detached"] = True
            elif line.startswith("locked"):
                cur["locked"] = True
            elif line.startswith("prunable"):
                cur["prunable"] = True
        entries.append(cur)
    return entries


def list_worktrees(common_dir: Path | str) -> list[dict[str, Any]]:
    """Invoke porcelain from the primary checkout that owns *common_dir*."""
    # Caller is expected to pass common_dir's owner checkout path; prefer common_dir itself.
    res = _run_git(common_dir, "worktree", "list", "--porcelain")
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or res.stdout.strip() or "git worktree list failed")
    return parse_worktree_porcelain(res.stdout)


def read_worktree_admin_id(worktree_gitdir: Path) -> str | None:
    """Return the administrative worktree id from a worktree's gitdir."""
    # gitdir file lives inside the worktree (e.g. /path/to/wt/.git -> gitdir: /repo/.git/worktrees/<id>)
    # The admin id is the basename of that worktrees/<id> directory.
    try:
        gitdir_ref = (
            (worktree_gitdir / ".git").read_text().strip()
            if (worktree_gitdir / ".git").exists()
            else ""
        )
        if gitdir_ref.startswith("gitdir: "):
            admin_path = Path(gitdir_ref.removeprefix("gitdir: ").strip())
            return admin_path.name
        # Inside common dir the entry itself is the id dir
        if worktree_gitdir.name and (worktree_gitdir / "HEAD").exists():
            return worktree_gitdir.name
    except Exception:
        return None
    return None


def canonical_branch_ref(branch: str) -> str:
    branch = branch.strip()
    if branch.startswith("refs/heads/"):
        return branch
    return f"refs/heads/{branch}"


def is_protected_branch(branch: str, *, configured: set[str] | None = None) -> bool:
    name = branch.removeprefix("refs/heads/").strip()
    if name in PROTECTED_BRANCHES:
        return True
    if configured and name in configured:
        return True
    # bossmode.protected-branch config is queried live; this is scope
    return False


def parse_rev_parse_short_head(head_output: str) -> str:
    return head_output.strip()


def build_writer_receipt(
    *,
    common_dir: str,
    admin_id: str,
    canonical_path: str,
    branch: str,
    base_sha: str,
    creation_head: str,
) -> dict[str, str]:
    """Construct a validated writer ownership receipt."""
    for field, value in (
        ("common_dir", common_dir),
        ("admin_id", admin_id),
        ("canonical_path", canonical_path),
        ("branch", branch),
        ("base_sha", base_sha),
        ("creation_head", creation_head),
    ):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"writer receipt requires non-empty {field}")
    full_ref = canonical_branch_ref(branch)
    name = full_ref.removeprefix("refs/heads/")
    if is_protected_branch(name):
        raise ValueError(f"protected branch not allowed for writer: {name}")
    return {
        "common_dir": common_dir.strip(),
        "admin_id": admin_id.strip(),
        "canonical_path": Path(canonical_path).resolve().as_posix()
        if Path(canonical_path).is_absolute()
        else canonical_path.strip(),
        "branch": full_ref,
        "base_sha": base_sha.strip(),
        "creation_head": creation_head.strip(),
    }


def primary_checkout_excluded(candidate_path: str, *, primary_checkout: str) -> bool:
    """True if candidate is not the primary checkout itself."""
    return Path(candidate_path).resolve() != Path(primary_checkout).resolve()


# Expose inventory summary for w0 evidence
def inventory_summary(cwd: Path | str = ".") -> dict[str, Any]:
    wt = list_worktrees(cwd)
    return {
        "worktree_fields": ["worktree", "HEAD", "branch", "locked", "prunable", "detached"],
        "admin_id_source": ".git/worktrees/<id> + .git gitdir file",
        "branch_refs": ["refs/heads/<name>"],
        "lock": "locked flag in porcelain + .git/worktrees/<id>/locked file",
        "prunable": "prunable flag in porcelain",
        "protected": list(sorted(PROTECTED_BRANCHES)),
        "process_use": "worktree still mounted blocks removal (locked + dirty/-untracked check)",
        "live_worktrees_sample": wt[:5],
    }
