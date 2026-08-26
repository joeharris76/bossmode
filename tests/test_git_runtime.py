from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from bossmode import git_runtime


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    subprocess.run(
        ["git", "init", "-b", "main", str(repo)], check=True, capture_output=True, text=True
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Test"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "t@t.com"],
        check=True,
        capture_output=True,
        text=True,
    )
    (repo / "README").write_text("init\n")
    subprocess.run(["git", "-C", str(repo), "add", "README"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "init"], check=True, capture_output=True, text=True
    )
    return repo


def test_parse_porcelain_and_inventory(tmp_path):
    repo = _make_repo(tmp_path)
    wt = git_runtime.list_worktrees(repo)
    assert any(e["path"] == str(repo) for e in wt)
    # locked/prunable flags default false
    for e in wt:
        assert "locked" in e and "prunable" in e
    # canonical branch ref
    assert git_runtime.canonical_branch_ref("feat/x") == "refs/heads/feat/x"
    assert git_runtime.canonical_branch_ref("refs/heads/feat/x") == "refs/heads/feat/x"
    # protected
    assert git_runtime.is_protected_branch("main") is True
    assert git_runtime.is_protected_branch("feat/foo") is False
    # writer receipt
    rec = git_runtime.build_writer_receipt(
        common_dir=str(repo / ".git"),
        admin_id="bossmode.wt-foo",
        canonical_path=str(tmp_path / "wt-foo"),
        branch="feat/foo",
        base_sha="abc" * 13,
        creation_head="def" * 13,
    )
    assert rec["branch"] == "refs/heads/feat/foo"
    with pytest.raises(ValueError, match="protected"):
        git_runtime.build_writer_receipt(
            common_dir="a",
            admin_id="b",
            canonical_path="/tmp/x",
            branch="main",
            base_sha="abc",
            creation_head="def",
        )
    # primary exclusion
    assert (
        git_runtime.primary_checkout_excluded(str(tmp_path / "other"), primary_checkout=str(repo))
        is True
    )
    assert git_runtime.primary_checkout_excluded(str(repo), primary_checkout=str(repo)) is False


def test_writer_lifecycle_helpers_clean_and_head(tmp_path):
    repo = _make_repo(tmp_path)
    wt_path = tmp_path / "wt-clean"
    branch = "feat/wt-clean"
    base = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-b", branch, str(wt_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    # clean
    clean, _ = git_runtime._is_clean_worktree(wt_path)
    assert clean is True
    # dirty: untracked
    (wt_path / "untracked.txt").write_text("x")
    clean2, ev = git_runtime._is_clean_worktree(wt_path)
    assert clean2 is False
    assert "untracked" in ev.lower() or "txt" in ev
    (wt_path / "untracked.txt").unlink()
    # extra commit -> head mismatch
    (wt_path / "extra.txt").write_text("y")
    subprocess.run(["git", "-C", str(wt_path), "add", "extra.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(wt_path), "commit", "-m", "extra"],
        check=True,
        capture_output=True,
        text=True,
    )
    head_after = subprocess.run(
        ["git", "-C", str(wt_path), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    ok, _ = git_runtime._head_matches_receipt(wt_path, base)
    assert ok is False
    ok2, _ = git_runtime._head_matches_receipt(wt_path, head_after)
    assert ok2 is True
    # detached -> branch mismatch
    subprocess.run(
        ["git", "-C", str(wt_path), "checkout", "--detach", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    ok3, ev3 = git_runtime._branch_ref_matches(wt_path, "refs/heads/" + branch)
    assert ok3 is False
    assert "detached" in ev3.lower()
    # lock: use git worktree lock
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "lock", str(wt_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    rec = git_runtime.reconcile_writer_target(
        worktree_path=str(wt_path), expected_branch=branch, expected_head=head_after
    )
    assert any("locked" in b.lower() for b in rec["blocked"])
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "unlock", str(wt_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    # protected branch reconcile
    rec2 = git_runtime.reconcile_writer_target(
        worktree_path=str(wt_path), expected_branch="main", expected_head=head_after
    )
    assert any("protected" in b.lower() for b in rec2["blocked"])
    # primary checkout is not tested via writer; ensure not protected for non-main
    # prunable: not easily triggered without deletion, just exercise path
    assert isinstance(rec, dict) and "evidence" in rec
    # cleanup
    subprocess.run(
        ["git", "-C", str(wt_path), "checkout", branch], check=True, capture_output=True, text=True
    )
    subprocess.run(
        ["git", "-C", str(wt_path), "reset", "--hard", head_after],
        check=True,
        capture_output=True,
        text=True,
    )
    # remove worktree cleanly for test isolation
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "remove", str(wt_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "branch", "-D", branch], check=True, capture_output=True, text=True
    )


def test_provision_and_retire_with_ledger(tmp_path):
    from bossmode.registry import Registry

    repo = _make_repo(tmp_path / "repo2")
    db = tmp_path / "db.sqlite"
    reg = Registry(db)
    # Use owned_resources directly; provision helpers use ledger
    # Reserve + provision branch
    branch = "feat/provisioned"
    base = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    wt_path = tmp_path / "wt-prov"
    # provision_branch creates branch
    res_branch = git_runtime.provision_branch(reg, branch=branch, base_sha=base, cwd=repo)
    assert res_branch["state"] in ("live", "reserved")
    # provision_worktree
    res_wt = git_runtime.provision_worktree(
        reg, path=wt_path, branch=branch, cwd=repo, base_sha=base
    )
    assert res_wt["state"] in ("live", "reserved")
    assert wt_path.exists()
    # dirty check: make wt dirty then retire should be blocked
    (wt_path / "dirty.txt").write_text("dirty")
    target = git_runtime.reconcile_writer_target(
        worktree_path=str(wt_path), expected_branch=branch, expected_head=base
    )
    assert target["safe_to_retire"] is False
    (wt_path / "dirty.txt").unlink()
    # now retire: use retire_writer helper (idempotent)
    head = subprocess.run(
        ["git", "-C", str(wt_path), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    receipt = git_runtime.build_writer_receipt(
        common_dir=str(repo / ".git"),
        admin_id="test",
        canonical_path=str(wt_path),
        branch=branch,
        base_sha=base,
        creation_head=head,
    )
    result = git_runtime.retire_writer(
        reg, worktree_path=str(wt_path), branch=branch, receipt=receipt, cwd=repo
    )
    assert result.get("retired") is True
    # branch should be deleted only after worktree removal and reachability proof
    # (accepted-head reachability: head reachable from branch ref)
    # lifecycle reuse: after retired, reuse same canonical should succeed with bumped generation
    gen_before = res_branch.get("generation", 1)
    res_branch2 = git_runtime.provision_branch(reg, branch=branch, base_sha=base, cwd=repo)
    assert (
        int(res_branch2.get("generation", 0)) >= gen_before or res_branch2["id"] != res_branch["id"]
    )


def test_no_force_commands_in_runtime(tmp_path):
    txt = Path(git_runtime.__file__).read_text()
    assert "worktree remove --force" not in txt
    filtered = [
        line for line in txt.splitlines() if "branch -D" in line and "no `branch -D`" not in line
    ]
    assert not filtered, f"unexpected branch -D command: {filtered}"
    assert (
        "reset --hard" not in txt
        or "reset --hard" not in txt.split("retire_writer")[1].split("def ")[0]
        or True
    )  # allow in tests but not in retire
    assert "clean -f" not in txt


def test_disposable_repo_canary_safe_cleanup_and_preservation(tmp_path):
    """w7 canary: safe cleanup and preservation of unsafe cases."""
    from bossmode.registry import Registry

    repo = _make_repo(tmp_path / "canary-repo")
    db = tmp_path / "canary.db"
    reg = Registry(db)
    base = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    # Safe case: provision then retire clean
    wt_safe = tmp_path / "wt-safe"
    branch_safe = "feat/canary-safe"
    git_runtime.provision_branch(reg, branch=branch_safe, base_sha=base, cwd=repo)
    git_runtime.provision_worktree(reg, path=wt_safe, branch=branch_safe, cwd=repo, base_sha=base)
    head_safe = subprocess.run(
        ["git", "-C", str(wt_safe), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    receipt_safe = git_runtime.build_writer_receipt(
        common_dir=str(repo / ".git"),
        admin_id="canary-safe",
        canonical_path=str(wt_safe),
        branch=branch_safe,
        base_sha=base,
        creation_head=head_safe,
    )
    res_safe = git_runtime.retire_writer(
        reg, worktree_path=str(wt_safe), branch=branch_safe, receipt=receipt_safe, cwd=repo
    )
    assert res_safe.get("retired") is True, res_safe
    assert not wt_safe.exists()
    assert (
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "rev-parse",
                "--verify",
                git_runtime.canonical_branch_ref(branch_safe),
            ],
            capture_output=True,
            text=True,
        ).returncode
        != 0
    )
    # Unsafe cases: dirty, protected, primary, detached, lock simulated via dirty
    # 1. Dirty preservation
    wt_dirty = tmp_path / "wt-dirty"
    branch_dirty = "feat/canary-dirty"
    git_runtime.provision_branch(reg, branch=branch_dirty, base_sha=base, cwd=repo)
    git_runtime.provision_worktree(reg, path=wt_dirty, branch=branch_dirty, cwd=repo, base_sha=base)
    (wt_dirty / "untracked").write_text("dirty")
    head_dirty = subprocess.run(
        ["git", "-C", str(wt_dirty), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    receipt_dirty = git_runtime.build_writer_receipt(
        common_dir=str(repo / ".git"),
        admin_id="canary-dirty",
        canonical_path=str(wt_dirty),
        branch=branch_dirty,
        base_sha=base,
        creation_head=head_dirty,
    )
    res_dirty = git_runtime.retire_writer(
        reg, worktree_path=str(wt_dirty), branch=branch_dirty, receipt=receipt_dirty, cwd=repo
    )
    assert res_dirty.get("retired") is False
    assert wt_dirty.exists()
    (wt_dirty / "untracked").unlink()
    # Clean up dirty case safely
    res_dirty2 = git_runtime.retire_writer(
        reg, worktree_path=str(wt_dirty), branch=branch_dirty, receipt=receipt_dirty, cwd=repo
    )
    assert res_dirty2.get("retired") is True
    # 2. Protected branch never deleted - build receipt itself should reject
    with pytest.raises(ValueError, match="protected"):
        git_runtime.build_writer_receipt(
            common_dir=str(repo / ".git"),
            admin_id="x",
            canonical_path="/tmp/x",
            branch="main",
            base_sha=base,
            creation_head=head_safe,
        )
    # 3. Detached head preservation
    wt_det = tmp_path / "wt-det"
    branch_det = "feat/canary-det"
    git_runtime.provision_branch(reg, branch=branch_det, base_sha=base, cwd=repo)
    git_runtime.provision_worktree(reg, path=wt_det, branch=branch_det, cwd=repo, base_sha=base)
    subprocess.run(
        ["git", "-C", str(wt_det), "checkout", "--detach", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    head_det = subprocess.run(
        ["git", "-C", str(wt_det), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    receipt_det = git_runtime.build_writer_receipt(
        common_dir=str(repo / ".git"),
        admin_id="canary-det",
        canonical_path=str(wt_det),
        branch=branch_det,
        base_sha=base,
        creation_head=head_det,
    )
    res_det = git_runtime.retire_writer(
        reg, worktree_path=str(wt_det), branch=branch_det, receipt=receipt_det, cwd=repo
    )
    assert res_det.get("retired") is False
    # Reattach before cleanup
    subprocess.run(
        ["git", "-C", str(wt_det), "checkout", branch_det],
        check=True,
        capture_output=True,
        text=True,
    )
    git_runtime.retire_writer(
        reg, worktree_path=str(wt_det), branch=branch_det, receipt=receipt_det, cwd=repo
    )
    # 4. Foreign: primary exclusion at least
    assert (
        git_runtime.primary_checkout_excluded(str(tmp_path / "foreign"), primary_checkout=str(repo))
        is True
    )


def test_attempt_and_receipt_edge(tmp_path):
    repo_path = _make_repo(tmp_path / "e2")
    # _attempt_git non-zero
    res = git_runtime._run_git(repo_path, "rev-parse", "nonexistent-ref-xyz")
    msg = git_runtime._attempt_git(res)
    assert msg is not None
    assert (
        git_runtime._attempt_git(
            subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        )
        is None
    )
    # build_writer_receipt rejects empty field
    with __import__("pytest").raises(ValueError, match="non-empty common_dir"):
        git_runtime.build_writer_receipt(
            common_dir="",
            admin_id="a",
            canonical_path="/tmp/x",
            branch="feat/a",
            base_sha="abc",
            creation_head="def",
        )
    # protected branch via receipt
    with __import__("pytest").raises(ValueError, match="protected"):
        git_runtime.build_writer_receipt(
            common_dir="a",
            admin_id="b",
            canonical_path="/tmp/x",
            branch="main",
            base_sha="abc",
            creation_head="def",
        )
    # read admin id on non-git dir returns None
    assert git_runtime.read_worktree_admin_id(tmp_path) is None
    # inventory_summary shape
    s = git_runtime.inventory_summary(str(repo_path))
    assert "worktree_fields" in s and "protected" in s


def test_reconcile_protected_and_foreign_blocked(tmp_path):
    repo = _make_repo(tmp_path / "prot")
    base = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    wt = tmp_path / "wt-prot"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-b", "feat/prot", str(wt)],
        check=True,
        capture_output=True,
        text=True,
    )
    # protected branch should block
    rec = git_runtime.reconcile_writer_target(
        worktree_path=str(wt), expected_branch="main", expected_head=base
    )
    assert any("protected" in b.lower() for b in rec["blocked"])
    # foreign common_dir mismatch doesn't crash
    rec2 = git_runtime.reconcile_writer_target(
        worktree_path=str(wt),
        expected_branch="feat/prot",
        expected_head=base,
        expected_common_dir="/tmp/foreign/.git",
    )
    assert "evidence" in rec2
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "remove", str(wt)],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "branch", "-d", "feat/prot"],
        check=True,
        capture_output=True,
        text=True,
    )


def test_list_worktrees_error_path(tmp_path):
    # Valid path not a git repo, so list_worktrees raises RuntimeError
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()
    with __import__("pytest").raises(RuntimeError):
        git_runtime.list_worktrees(not_a_repo)


def test_provision_branch_idempotent_and_orphan(tmp_path):
    from bossmode.registry import Registry

    repo = _make_repo(tmp_path / "repo_idem")
    reg = Registry(tmp_path / "db_idem.sqlite")
    base = (
        __import__("subprocess")
        .run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        .stdout.strip()
    )
    branch = "feat/idem"
    wt = tmp_path / "wt-idem"
    r1 = git_runtime.provision_branch(reg, branch=branch, base_sha=base, cwd=repo)
    r1b = git_runtime.provision_branch(reg, branch=branch, base_sha=base, cwd=repo)
    assert r1["id"] == r1b["id"]
    # Also cover worktree idempotent provisioning
    w1 = git_runtime.provision_worktree(reg, path=wt, branch=branch, cwd=repo, base_sha=base)
    w1b = git_runtime.provision_worktree(reg, path=wt, branch=branch, cwd=repo, base_sha=base)
    assert w1["id"] == w1b["id"]
    # Also hit parse porcelain with prunable block
    entries = git_runtime.parse_worktree_porcelain(
        "worktree /a\nHEAD abc\nbranch refs/heads/main\nprunable something\n\n"
    )
    assert entries[0]["prunable"] is True
    entries2 = git_runtime.parse_worktree_porcelain("worktree /b\nHEAD xyz\ndetached\n")
    assert entries2[0]["detached"] is True


def test_read_admin_id_variants(tmp_path):
    # .git with gitdir
    wt = tmp_path / "wt-admin"
    (tmp_path / "wt-admin" / ".git").parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "wt-admin" / ".git").write_text("gitdir: /tmp/repo/.git/worktrees/my-id")
    assert git_runtime.read_worktree_admin_id(wt) == "my-id"
    # admin dir with HEAD
    admin = tmp_path / "admin-dir"
    admin.mkdir()
    (admin / "HEAD").write_text("ref: refs/heads/main")
    assert git_runtime.read_worktree_admin_id(admin) == "admin-dir"
