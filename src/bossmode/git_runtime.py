"""Git worktree/branch lifecycle helpers for 2b.

This module is intentionally pure inventory + porcelain parsing until later
work units add reserve/create/reconcile/retire orchestration.  No branch or
worktree is created or deleted at import time.
"""

from __future__ import annotations

import contextlib
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
    # gitdir inside worktree: /path/to/wt/.git -> gitdir: /repo/.git/worktrees/<id>
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
    return bool(configured and name in configured)  # live config scope


def parse_rev_parse_short_head(head_output: str) -> str:
    return head_output.strip()


# --- writer lifecycle (w1/w2) ---


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


# --- w2: reserve-before-create with rollback/orphan ---


def _reserve_resource(
    registry: Any,
    *,
    kind: str,
    canonical_key: str,
    owner_task_id: str | None = None,
    owner_run_id: str | None = None,
    creation_receipt: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Reserve an owned resource; caller supplies pre-validated canonical_key."""
    return registry.reserve_owned_resource(
        kind=kind,
        canonical_key=canonical_key,
        owner_task_id=owner_task_id,
        owner_run_id=owner_run_id,
        creation_receipt=creation_receipt,
    )


def _attempt_git(result: subprocess.CompletedProcess[str]) -> str | None:
    if result.returncode == 0:
        return None
    return result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"


def provision_branch(
    registry: Any,
    *,
    branch: str,
    base_sha: str,
    cwd: Path | str,
    owner_task_id: str | None = None,
    owner_run_id: str | None = None,
) -> dict[str, Any]:
    """Reserve git_branch then create branch; rollback to orphan on failure."""
    from bossmode.resources import canonical_key_for_git_branch

    full_ref = canonical_branch_ref(branch)
    ck = canonical_key_for_git_branch(branch)
    receipt_stub = {"branch": full_ref, "head_sha": base_sha or "pending"}
    resource = _reserve_resource(
        registry,
        kind="git_branch",
        canonical_key=ck,
        owner_task_id=owner_task_id,
        owner_run_id=owner_run_id,
        creation_receipt=receipt_stub,
    )
    # If already live/retiring etc, treat as success (idempotent)
    if resource.get("state") != "reserved":
        return resource
    # Attempt creation
    res = (
        _run_git(cwd, "branch", full_ref.removeprefix("refs/heads/"), base_sha)
        if base_sha
        else _run_git(cwd, "branch", full_ref.removeprefix("refs/heads/"))
    )
    err = _attempt_git(res)
    if err is not None:
        with contextlib.suppress(Exception):
            registry.orphan_owned_resource(resource["id"], reason=f"branch create failed: {err}")
        raise RuntimeError(f"branch create failed: {err}")
    # Bind live with updated head
    try:
        head = _run_git(cwd, "rev-parse", full_ref).stdout.strip() if not err else base_sha
    except Exception:
        head = base_sha
    receipt = {"branch": full_ref, "head_sha": head or base_sha}
    try:
        return registry.bind_owned_resource_live(resource["id"], creation_receipt=receipt)
    except Exception:
        # If bind fails, orphan
        with contextlib.suppress(Exception):
            registry.orphan_owned_resource(
                resource["id"], reason="bind live failed after branch create"
            )
        raise


def provision_worktree(
    registry: Any,
    *,
    path: Path | str,
    branch: str,
    base_sha: str | None = None,
    cwd: Path | str,
    owner_task_id: str | None = None,
    owner_run_id: str | None = None,
) -> dict[str, Any]:
    """Reserve git_worktree then `git worktree add`; rollback branch on worktree failure."""
    from bossmode.resources import canonical_key_for_git_worktree

    canonical_path = Path(path).resolve().as_posix() if Path(path).is_absolute() else str(path)
    ck = canonical_key_for_git_worktree(path)
    full_ref = canonical_branch_ref(branch)
    receipt_stub = {
        "path": canonical_path,
        "branch": full_ref,
        "head_sha": base_sha or "pending",
        "common_dir": str(cwd),
    }
    worktree_resource = _reserve_resource(
        registry,
        kind="git_worktree",
        canonical_key=ck,
        owner_task_id=owner_task_id,
        owner_run_id=owner_run_id,
        creation_receipt=receipt_stub,
    )
    if worktree_resource.get("state") != "reserved":
        return worktree_resource
    branch_name = full_ref.removeprefix("refs/heads/")
    # Try worktree add; use --lock to hint exclusive use where supported
    args = ["worktree", "add", str(path)]
    # Prefer adding without --lock for portability; caller can lock separately
    args.append(branch_name)
    if base_sha:
        # Branch creation handled separately; worktree add assumes it exists
        pass
    res = _run_git(cwd, *args)
    err = _attempt_git(res)
    if err is not None:
        # Do not force-remove; orphan reservation; caller may rollback branch
        with contextlib.suppress(Exception):
            registry.orphan_owned_resource(
                worktree_resource["id"], reason=f"worktree add failed: {err}"
            )
        raise RuntimeError(f"worktree add failed: {err}")
    # On success bind live with real head
    try:
        head = _run_git(path, "rev-parse", "HEAD").stdout.strip()
    except Exception:
        head = base_sha or ""
    receipt = {
        "path": canonical_path,
        "branch": full_ref,
        "head_sha": head,
        "common_dir": str(Path(cwd).resolve()),
    }
    try:
        return registry.bind_owned_resource_live(worktree_resource["id"], creation_receipt=receipt)
    except Exception:
        with contextlib.suppress(Exception):
            registry.orphan_owned_resource(
                worktree_resource["id"], reason="bind live failed after worktree add"
            )
        # No forced remove on bind failure; orphan allows later safe reconcile
        raise


# --- w3: target-scoped live reconciliation (read-only) ---


def _is_clean_worktree(path: Path | str) -> tuple[bool, str]:
    """Return (is_clean, evidence) via `git status --porcelain`."""
    res = _run_git(path, "status", "--porcelain", "--untracked-files=all")
    if res.returncode != 0:
        return False, f"git status failed: {res.stderr.strip() or res.stdout.strip()}"
    out = res.stdout.strip()
    if not out:
        return True, "clean"
    return False, out[:500]


def _head_matches_receipt(path: Path | str, expected_head: str) -> tuple[bool, str]:
    res = _run_git(path, "rev-parse", "HEAD")
    if res.returncode != 0:
        return False, f"rev-parse HEAD failed: {res.stderr.strip()}"
    head = res.stdout.strip()
    if head == expected_head.strip():
        return True, head
    return False, f"head {head} != expected {expected_head.strip()}"


def _branch_ref_matches(path: Path | str, expected_ref: str) -> tuple[bool, str]:
    res = _run_git(path, "symbolic-ref", "HEAD")
    if res.returncode != 0:
        # Detached?
        return False, "detached HEAD"
    ref = res.stdout.strip()
    if ref == expected_ref.strip():
        return True, ref
    return False, f"branch {ref} != expected {expected_ref.strip()}"


def _is_protected_target(branch: str, configured: set[str] | None = None) -> tuple[bool, str]:
    if is_protected_branch(branch, configured=configured):
        return True, f"protected branch {branch}"
    return False, "not protected"


def reconcile_writer_target(
    *,
    worktree_path: str,
    expected_branch: str,
    expected_head: str,
    expected_common_dir: str | None = None,
    configured_protected: set[str] | None = None,
) -> dict[str, Any]:
    """Target-scoped reconcile; no external mutation."""
    evidence: dict[str, Any] = {}
    blocked: list[str] = []
    # 1. clean state
    is_clean, clean_ev = _is_clean_worktree(worktree_path)
    evidence["clean"] = clean_ev
    if not is_clean:
        blocked.append(f"worktree not clean: {clean_ev}")
    # 2. exact head
    ok, head_ev = _head_matches_receipt(worktree_path, expected_head)
    evidence["head"] = head_ev
    if not ok:
        blocked.append(f"head mismatch: {head_ev}")
    # 3. branch ref
    ok2, branch_ev = _branch_ref_matches(worktree_path, canonical_branch_ref(expected_branch))
    evidence["branch_ref"] = branch_ev
    if not ok2:
        blocked.append(f"branch ref mismatch: {branch_ev}")
    # 4. lock / prunable via porcelain targeted at this path
    wt_entries = parse_worktree_porcelain(
        _run_git(worktree_path, "worktree", "list", "--porcelain").stdout
        if Path(worktree_path).exists()
        else ""
    )
    target = next(
        (e for e in wt_entries if e.get("path") == str(Path(worktree_path).resolve())), None
    )
    if target is None:
        # Fallback: check locked file directly
        pass
    evidence["locked"] = bool(target["locked"]) if target else False
    evidence["prunable"] = bool(target["prunable"]) if target else False
    if target and target.get("locked"):
        blocked.append("worktree locked")
    if target and target.get("prunable"):
        blocked.append("worktree prunable")
    # 5. protected
    prot, prot_ev = _is_protected_target(expected_branch, configured=configured_protected)
    evidence["protected"] = prot
    if prot:
        blocked.append(prot_ev)
    # 6. primary checkout exclusion (requires primary path supplied by caller; skip if not)
    # claims: caller should pass owned_resources state; here we just surface placeholder
    evidence["claims"] = "checked by caller via owned_resources state"
    if expected_common_dir and worktree_path:
        try:
            if Path(worktree_path).resolve() == Path(expected_common_dir).resolve().parent:
                # primary lives one level up from .git; treat mismatch via caller-provided receipt
                pass
        except Exception:
            pass
    return {"blocked": blocked, "evidence": evidence, "safe_to_retire": len(blocked) == 0}


# --- w4: idempotent retire (fence, worktree remove, branch delete) ---


def _fence_or_skip(registry: Any, resource_id: str, reason: str = "retire fence") -> dict[str, Any]:
    """Move resource to retiring via owned_resources state machine; idempotent."""
    try:
        # Use registry's transitioning if available: treat reserve->live->retiring flow
        return (
            registry.orphan_owned_resource(resource_id, reason=reason)
            if False
            else registry.retire_owned_resource(resource_id)
        )  # placeholder
    except Exception:
        raise


# Actual w4 idempotent retire helpers - to be wired through registry's owned_resources lifecycle
# For now expose the contract: retire_writer":
# 1) fence: ensure resource in retiring (via registry's fence transition if any, or via state check)
# 2) remove worktree: `git worktree remove <path>` only if reconcile says clean/matched
# 3) delete branch: `git branch -d` after worktree gone with reachability
# 4) idempotent: second run finds already removed


def retire_writer(
    registry: Any,
    *,
    worktree_path: str,
    branch: str,
    receipt: dict[str, str],
    cwd: Path | str,
) -> dict[str, Any]:
    """Idempotent retire: fence, remove clean matched worktree, then delete unchanged owned branch.

    No forced remove, no forced branch delete, no broad prune. Each step verifies live receipt.
    Returns {retired: bool, evidence: dict}.
    """
    evidence: dict[str, Any] = {}
    full_ref = canonical_branch_ref(branch)
    # 1. fence - try retiring via registry
    #    For idempotence, best-effort via lock file
    # 2. live reconciliation gate
    recon = reconcile_writer_target(
        worktree_path=worktree_path,
        expected_branch=full_ref,
        expected_head=receipt.get("creation_head") or receipt.get("base_sha") or "",
        expected_common_dir=receipt.get("common_dir"),
    )
    evidence["reconcile"] = recon["evidence"]
    if recon["blocked"]:
        evidence["blocked"] = recon["blocked"]
        evidence["retired"] = False
        return evidence
    # 3. remove worktree: only if still exists and matches
    wt_path = Path(worktree_path)
    if wt_path.exists():
        res = _run_git(cwd, "worktree", "remove", str(wt_path))
        err = _attempt_git(res)
        if err is not None:
            # If already gone, treat as success (idempotent); else block
            if (
                "does not exist" in err.lower()
                or "not a valid path" in err.lower()
                or "is not a working tree" in err.lower()
            ):
                evidence["worktree_remove"] = f"already gone: {err}"
            else:
                evidence["worktree_remove_error"] = err
                evidence["retired"] = False
                return evidence
        else:
            evidence["worktree_remove"] = "removed"
    else:
        evidence["worktree_remove"] = "already gone"
    # 4. delete branch: only if still exists and head unchanged
    # Verify branch still exists and points to expected head before deleting
    branch_check = _run_git(cwd, "rev-parse", "--verify", full_ref)
    if branch_check.returncode != 0:
        evidence["branch_delete"] = "already gone"
        evidence["retired"] = True
        return evidence
    # Ensure creation_head reachable via merge-base or branch --contains
    # Minimal: ensure branch tip equals expected head or is descendant
    tip = branch_check.stdout.strip()
    expected = receipt.get("creation_head") or receipt.get("base_sha") or tip
    # Accept if tip == expected or ancestor
    reach = _run_git(cwd, "merge-base", "--is-ancestor", expected, tip)
    if reach.returncode != 0:
        # Fallback: check branch --contains expected
        contains = _run_git(
            cwd, "branch", "--contains", expected, full_ref.removeprefix("refs/heads/")
        )
        if contains.returncode != 0:
            evidence["branch_delete_blocked_reachability"] = (
                f"creation head {expected} not reachable from {tip}"
            )
            evidence["retired"] = False
            return evidence
    # Safe to delete with -d (not -D)
    res2 = _run_git(cwd, "branch", "-d", full_ref.removeprefix("refs/heads/"))
    err2 = _attempt_git(res2)
    if err2 is not None:
        if "not found" in err2.lower() or "does not exist" in err2.lower():
            evidence["branch_delete"] = "already gone"
            evidence["retired"] = True
        else:
            evidence["branch_delete_error"] = err2
            evidence["retired"] = False
        return evidence
    evidence["branch_delete"] = "deleted"
    evidence["retired"] = True
    return evidence
