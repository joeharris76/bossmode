from __future__ import annotations

import sqlite3

import pytest

from bossmode.registry import Registry, RegistryError
from bossmode.resources import (
    ResourceError,
    canonical_key_for_git_branch,
    canonical_key_for_git_worktree,
    canonical_key_for_herdr_worker,
    validate_canonical_key,
    validate_creation_receipt,
    validate_resource_kind,
)


def test_resource_helpers_cover_all_branches(tmp_path):
    from bossmode.resources import ResourceRecord, can_transition, is_terminal_state

    assert validate_resource_kind("herdr_worker") == "herdr_worker"
    assert validate_resource_kind("  herdr_worker  ") == "herdr_worker"
    with pytest.raises(ResourceError, match="unsupported resource kind"):
        validate_resource_kind("unknown")  # type: ignore[arg-type]
    with pytest.raises(ResourceError, match="non-empty string"):
        validate_resource_kind("")
    with pytest.raises(ResourceError, match="non-empty string"):
        validate_resource_kind(None)  # type: ignore[arg-type]
    # canonical key branches
    with pytest.raises(ResourceError, match="non-empty string"):
        validate_canonical_key("")
    with pytest.raises(ResourceError, match="non-empty string"):
        validate_canonical_key(None)  # type: ignore[arg-type]
    long = "x" * 2000
    with pytest.raises(ResourceError, match="exceeds"):
        validate_canonical_key(long)
    assert validate_canonical_key("  a/b  ") == "a/b"
    # herdr worker session validation
    with pytest.raises(ResourceError, match="herdr_session"):
        canonical_key_for_herdr_worker("", "worker-a")
    with pytest.raises(ResourceError, match="invalid Herdr"):
        canonical_key_for_herdr_worker("s", "")
    # git branch branches
    with pytest.raises(ResourceError, match="branch name"):
        canonical_key_for_git_branch("")
    with pytest.raises(ResourceError, match="branch name"):
        canonical_key_for_git_branch("/bad")
    assert canonical_key_for_git_branch("feat/foo") == "git_branch:refs/heads/feat/foo"
    # git worktree already covered but ensure strip works
    with pytest.raises(ResourceError, match="absolute"):
        canonical_key_for_git_worktree("")
    # creation receipt git_worktree branches
    with pytest.raises(ResourceError, match="requires non-empty"):
        validate_creation_receipt("git_worktree", {"path": "/p", "branch": "b"})
    with pytest.raises(ResourceError, match="requires non-empty"):
        validate_creation_receipt("git_branch", {"branch": "b"})
    with pytest.raises(ResourceError, match="requires non-empty"):
        validate_creation_receipt("git_branch", {"branch": "", "head_sha": "h"})
    # ResourceRecord serialization
    rec = ResourceRecord(
        "id1",
        "herdr_worker",
        "herdr_worker:s/w",
        "t1",
        "r1",
        "th1",
        "reserved",
        None,
        1,
        "2026-01-01T00:00:00Z",
        "2026-01-01T00:00:00Z",
    )
    d = rec.to_dict()
    assert d["id"] == "id1"
    assert d["kind"] == "herdr_worker"
    # is_terminal/can_transition branches
    assert is_terminal_state("retired") is True
    assert is_terminal_state("orphaned") is True
    assert is_terminal_state("live") is False
    assert can_transition("reserved", "live") is True
    assert can_transition("retired", "live") is False
    assert can_transition("unknown", "live") is False


def test_resource_kind_validation(tmp_path):
    assert validate_resource_kind("herdr_worker") == "herdr_worker"
    with pytest.raises(ResourceError, match="unsupported resource kind"):
        validate_resource_kind("unknown")  # type: ignore[arg-type]
    with pytest.raises(ResourceError, match="non-empty string"):
        validate_resource_kind("")


def test_canonical_key_helpers(tmp_path):
    assert canonical_key_for_herdr_worker("sess1", "worker-a") == "herdr_worker:sess1/worker-a"
    with pytest.raises(ResourceError, match="invalid Herdr worker name"):
        canonical_key_for_herdr_worker("s", "BAD")
    assert canonical_key_for_git_branch("main") == "git_branch:refs/heads/main"
    assert canonical_key_for_git_branch("refs/heads/main") == "git_branch:refs/heads/main"
    with pytest.raises(ResourceError, match="invalid branch name"):
        canonical_key_for_git_branch("..")
    assert canonical_key_for_git_worktree("/tmp/a/b") == "git_worktree:/tmp/a/b"
    with pytest.raises(ResourceError, match="absolute"):
        canonical_key_for_git_worktree("relative/path")
    with pytest.raises(ResourceError, match="cannot contain traversal"):
        validate_canonical_key("a/../b")


def test_creation_receipt_validation(tmp_path):
    assert validate_creation_receipt("herdr_worker", None) is None
    with pytest.raises(ResourceError, match="must be a JSON object"):
        validate_creation_receipt("herdr_worker", "bad")  # type: ignore[arg-type]
    # herdr_worker requires fields
    with pytest.raises(ResourceError, match="requires non-empty"):
        validate_creation_receipt("herdr_worker", {"herdr_session": "s"})
    receipt = {"herdr_session": "s", "worker_name": "w", "agent_kind": "pi"}
    assert validate_creation_receipt("herdr_worker", receipt) == receipt
    # partial session tuple rejected
    with pytest.raises(ResourceError, match="supplied together"):
        validate_creation_receipt("herdr_worker", {**receipt, "session_source": "x"})


def test_reserve_is_idempotent_and_ownership_checked(tmp_path):
    reg = Registry(tmp_path / "control.db")
    a = reg.reserve_owned_resource(
        kind="herdr_worker",
        canonical_key=canonical_key_for_herdr_worker("sess", "w1"),
        owner_task_id="task_1",
        owner_run_id="run_1",
    )
    b = reg.reserve_owned_resource(
        kind="herdr_worker",
        canonical_key=canonical_key_for_herdr_worker("sess", "w1"),
        owner_task_id="task_1",
        owner_run_id="run_1",
    )
    assert a["id"] == b["id"]
    assert a["state"] == "reserved"
    # different owner must fail
    with pytest.raises(RegistryError, match="different owner"):
        reg.reserve_owned_resource(
            kind="herdr_worker",
            canonical_key=canonical_key_for_herdr_worker("sess", "w1"),
            owner_task_id="task_2",
        )


def test_bind_live_requires_reserved_and_receipt(tmp_path):
    reg = Registry(tmp_path / "control.db")
    r = reg.reserve_owned_resource(
        kind="git_branch",
        canonical_key=canonical_key_for_git_branch("feat/foo"),
        owner_task_id="t1",
    )
    receipt = {"branch": "feat/foo", "head_sha": "abc123"}
    live = reg.bind_owned_resource_live(r["id"], creation_receipt=receipt)
    assert live["state"] == "live"
    assert live["generation"] == 2
    # idempotent with same receipt
    again = reg.bind_owned_resource_live(r["id"], creation_receipt=receipt)
    assert again["state"] == "live"
    # different receipt rejected
    with pytest.raises(RegistryError, match="different receipt"):
        reg.bind_owned_resource_live(
            r["id"], creation_receipt={"branch": "feat/foo", "head_sha": "different"}
        )


def test_illegal_transitions(tmp_path):
    reg = Registry(tmp_path / "control.db")
    r = reg.reserve_owned_resource(
        kind="git_worktree",
        canonical_key=canonical_key_for_git_worktree("/tmp/wt"),
    )
    # retire without retiring state
    with pytest.raises(RegistryError, match="not retiring"):
        reg.retire_owned_resource(r["id"])
    # begin retirement from reserved ok, then retire
    retiring = reg.begin_retirement(r["id"])
    assert retiring["state"] == "retiring"
    # idempotent
    retiring2 = reg.begin_retirement(r["id"])
    assert retiring2["state"] == "retiring"
    retired = reg.retire_owned_resource(r["id"])
    assert retired["state"] == "retired"
    # retiring again from terminal fails
    with pytest.raises(RegistryError, match="cannot begin retirement"):
        reg.begin_retirement(r["id"])
    # orphan from retired fails
    with pytest.raises(RegistryError, match="cannot be orphaned"):
        reg.orphan_owned_resource(r["id"])


def test_orphan_and_wrong_owner(tmp_path):
    reg = Registry(tmp_path / "control.db")
    r = reg.reserve_owned_resource(
        kind="herdr_worker",
        canonical_key=canonical_key_for_herdr_worker("s", "w-orphan"),
    )
    orphaned = reg.orphan_owned_resource(r["id"], reason="foreign")
    assert orphaned["state"] == "orphaned"
    # idempotent
    orphaned2 = reg.orphan_owned_resource(r["id"])
    assert orphaned2["state"] == "orphaned"


def test_list_and_query_and_events(tmp_path):
    reg = Registry(tmp_path / "control.db")
    r1 = reg.reserve_owned_resource(
        kind="git_branch",
        canonical_key=canonical_key_for_git_branch("feat/a"),
        owner_task_id="t1",
    )
    _ = reg.reserve_owned_resource(
        kind="git_branch",
        canonical_key=canonical_key_for_git_branch("feat/b"),
        owner_task_id="t1",
    )
    all_resources = reg.list_owned_resources()
    assert len(all_resources) == 2
    filtered = reg.list_owned_resources(owner_task_id="t1")
    assert len(filtered) == 2
    filtered_kind = reg.list_owned_resources(kind="git_branch", state="reserved")
    assert len(filtered_kind) == 2
    detail = reg.get_owned_resource(r1["id"])
    assert detail["id"] == r1["id"]
    assert "events" in detail
    assert detail["events"][0]["to_state"] == "reserved"


def test_reconciliation_is_read_only(tmp_path):
    reg = Registry(tmp_path / "control.db")
    r = reg.reserve_owned_resource(
        kind="git_branch",
        canonical_key=canonical_key_for_git_branch("feat/recon"),
    )
    receipt = {"branch": "feat/recon", "head_sha": "abc"}
    reg.bind_owned_resource_live(r["id"], creation_receipt=receipt)
    observed = reg.reconcile_owned_resources()
    assert any(item["resource"]["id"] == r["id"] for item in observed)
    # state unchanged
    after = reg.get_owned_resource(r["id"])
    assert after["state"] == "live"
    assert all("observation" in item for item in observed)


def test_retry_after_crash_reserved_stays_reserved(tmp_path):
    """Crash between reserve and bind leaves reserved; retry is idempotent."""
    reg = Registry(tmp_path / "control.db")
    ck = canonical_key_for_git_branch("feat/crash")
    r = reg.reserve_owned_resource(kind="git_branch", canonical_key=ck, owner_task_id="t1")
    # simulate retry after crash: same reserve returns same row, no duplicate
    r2 = reg.reserve_owned_resource(kind="git_branch", canonical_key=ck, owner_task_id="t1")
    assert r["id"] == r2["id"]
    # now bind should still work
    live = reg.bind_owned_resource_live(
        r["id"], creation_receipt={"branch": "feat/crash", "head_sha": "h1"}
    )
    assert live["state"] == "live"


def test_identity_reuse_after_terminal(tmp_path):
    """After retired, reusing same canonical key succeeds as new generation (partial uniqueness)."""
    reg = Registry(tmp_path / "control.db")
    ck = canonical_key_for_git_branch("feat/reuse")
    r = reg.reserve_owned_resource(kind="git_branch", canonical_key=ck)
    reg.begin_retirement(r["id"])
    reg.retire_owned_resource(r["id"])
    # w5: retired allows reuse as new generation
    r2 = reg.reserve_owned_resource(kind="git_branch", canonical_key=ck)
    assert r2["generation"] == r["generation"] + 1
    assert r2["id"] != r["id"]
    # But while the new one is active, a third reserve must fail (partial uniqueness)
    with pytest.raises(RegistryError, match="already reserved"):
        reg.reserve_owned_resource(kind="git_branch", canonical_key=ck)


def test_canonical_key_mismatch_on_bind(tmp_path):
    reg = Registry(tmp_path / "control.db")
    r = reg.reserve_owned_resource(
        kind="git_branch", canonical_key=canonical_key_for_git_branch("feat/a")
    )
    with pytest.raises(RegistryError, match="canonical key mismatch"):
        reg.bind_owned_resource_live(
            r["id"],
            creation_receipt={"branch": "feat/a", "head_sha": "h"},
            expected_canonical_key="git_branch:refs/heads/other",
        )


def test_migration_creates_owned_resources_tables(tmp_path):
    # Owned-resource tables are soft-created without bumping schema version; version stays 9
    reg = Registry(tmp_path / "control.db")
    reg.initialize()
    conn = sqlite3.connect(reg.path)
    try:
        row = conn.execute("SELECT version FROM schema_meta").fetchone()
        assert row[0] == 9
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        assert "owned_resources" in tables
        assert "owned_resource_events" in tables
    finally:
        conn.close()


def test_display_name_not_deletion_authority(tmp_path):
    """A stored name/pane ID must not by itself authorize deletion."""
    reg = Registry(tmp_path / "control.db")
    r = reg.reserve_owned_resource(
        kind="herdr_worker",
        canonical_key=canonical_key_for_herdr_worker("sess", "worker-x"),
        creation_receipt={"herdr_session": "sess", "worker_name": "worker-x", "agent_kind": "pi"},
    )
    # Even though we know the worker name, retirement requires proper state transition + receipt.
    with pytest.raises(RegistryError, match="not retiring"):
        reg.retire_owned_resource(r["id"])
    # Proper path: reserve -> live -> retiring -> retired
    reg.bind_owned_resource_live(
        r["id"],
        creation_receipt={"herdr_session": "sess", "worker_name": "worker-x", "agent_kind": "pi"},
    )
    reg.begin_retirement(r["id"])
    retired = reg.retire_owned_resource(r["id"])
    assert retired["state"] == "retired"


def test_resources_coverage_additional_branches(tmp_path):
    """Cover remaining resource branches for the coverage gate."""

    from bossmode.resources import (
        ResourceRecord,
        can_transition,
        is_terminal_state,
    )

    # to_dict + helpers
    rec = ResourceRecord(
        "id1",
        "herdr_worker",
        "herdr_worker:s/w",
        "t1",
        "r1",
        "th1",
        "reserved",
        None,
        1,
        "2026-01-01",
        "2026-01-02",
    )
    assert rec.to_dict()["id"] == "id1"
    assert is_terminal_state("retired") is True
    assert is_terminal_state("reserved") is False
    assert can_transition("reserved", "live") is True
    assert can_transition("retired", "live") is False
    assert can_transition("unknown_kind", "live") is False  # line 148 default

    # max key bytes exceeded
    with pytest.raises(ResourceError, match="exceeds"):
        validate_canonical_key("a" * 2000)
    # None/non-string
    with pytest.raises(ResourceError, match="non-empty string"):
        validate_canonical_key(None)  # type: ignore[arg-type]
    with pytest.raises(ResourceError, match="non-empty string"):
        validate_resource_kind(None)  # type: ignore[arg-type]
    # herdr session empty
    with pytest.raises(ResourceError, match="non-empty string"):
        canonical_key_for_herdr_worker("", "w")
    with pytest.raises(ResourceError, match="non-empty string"):
        canonical_key_for_git_branch("")
    with pytest.raises(ResourceError, match="branch name"):
        canonical_key_for_git_branch("bad?.branch")
    # receipt: git_worktree missing field, git_branch missing field, herdr dict non-string field
    with pytest.raises(ResourceError, match="requires non-empty"):
        validate_creation_receipt(
            "git_worktree", {"path": "p", "branch": "b"}
        )  # missing head_sha, common_dir
    with pytest.raises(ResourceError, match="requires non-empty"):
        validate_creation_receipt("git_branch", {"branch": "b"})  # missing head_sha
    with pytest.raises(ResourceError, match="must be a JSON object"):
        validate_creation_receipt(  # type: ignore[arg-type]
            "git_worktree",
            "not a dict",  # type: ignore[arg-type]
        )
    with pytest.raises(ResourceError, match="requires non-empty"):
        validate_creation_receipt(
            "git_worktree", {"path": "", "branch": "b", "head_sha": "h", "common_dir": "c"}
        )
    # git_worktree with surrounding whitespace is normalized (strip before absolute check)
    k = canonical_key_for_git_worktree("  /tmp/foo  ")
    assert k == "git_worktree:/tmp/foo"
    with pytest.raises(ResourceError, match="absolute"):
        canonical_key_for_git_worktree("relative/path")
