"""Tests for 2a herdr_runtime — bounded subprocess, parsing, receipt-matched close."""

import subprocess

from bossmode import herdr_runtime


def test_parse_version_and_lists():
    assert herdr_runtime.parse_version("herdr 0.8.2\n") == "0.8.2"
    assert (
        herdr_runtime.parse_agent_list('{"result":{"agents":[{"agent":"pi"}]}}')[0]["agent"] == "pi"
    )
    assert herdr_runtime.parse_agent_get('{"result":{"agent":{"agent":"pi"}}}')["agent"] == "pi"
    assert (
        herdr_runtime.parse_pane_list('{"result":{"panes":[{"pane_id":"w1:p1"}]}}')[0]["pane_id"]
        == "w1:p1"
    )
    assert herdr_runtime.parse_agent_list("not json") == []


def test_run_herdr_timeout_and_missing(monkeypatch):
    def fake_run(*a, **kw):
        raise subprocess.TimeoutExpired(cmd=["herdr"], timeout=1, output=b"out", stderr=b"err")

    monkeypatch.setattr(herdr_runtime.subprocess, "run", fake_run)
    r = herdr_runtime.run_herdr("agent", "list")
    assert r.returncode == 124

    def fake_missing(*a, **kw):
        raise FileNotFoundError("no herdr")

    monkeypatch.setattr(herdr_runtime.subprocess, "run", fake_missing)
    r2 = herdr_runtime.run_herdr("agent", "list")
    assert r2.returncode == 127


def test_probe_and_diagnostic():
    probe = herdr_runtime.probe_supported_version()
    assert "herdr_version" in probe and "identity_fields" in probe
    diag = herdr_runtime.herdr_capability_diagnostic()
    assert "missing_binary_case" in diag


def test_receipt_match_and_close_logic(monkeypatch, tmp_path):
    from bossmode.registry import Registry

    reg = Registry(tmp_path / "db.sqlite")
    # reserve
    r = herdr_runtime.reserve_herdr_worker(
        reg, herdr_session="sess", worker_name="w-01", agent_kind="pi"
    )
    assert r["state"] == "reserved"
    # bind live
    receipt = {
        "herdr_session": "sess",
        "worker_name": "w-01",
        "agent_kind": "pi",
        "pane_id": "w1:p1",
        "tab_id": "w1:t1",
        "workspace_id": "w1",
        "session_source": "herdr:pi",
        "session_agent": "pi",
        "session_ref_kind": "id",
        "session_value": "abc",
    }
    live = herdr_runtime.bind_herdr_worker_live(reg, r["id"], receipt)
    assert live["state"] == "live"
    # receipt mismatch via pane
    ok, _ = herdr_runtime._receipt_matches_live(
        receipt, {"pane_id": "w1:p2", "tab_id": "w1:t1", "workspace_id": "w1"}
    )
    assert ok is False
    # close requires receipt match: mock pane close
    live_full = {
        "pane_id": "w1:p1",
        "tab_id": "w1:t1",
        "workspace_id": "w1",
        "agent_session": {"source": "herdr:pi", "kind": "id", "value": "abc"},
    }

    def fake_run(*a, **kw):
        if "close" in a:
            return herdr_runtime.HerdrResult(0, "", "", "")
        return herdr_runtime.HerdrResult(0, '{"result":{"agents":[]}}', "", "")

    monkeypatch.setattr(herdr_runtime, "run_herdr", fake_run)
    res = herdr_runtime.close_owned_worker(reg, r["id"], live_full)
    assert res["closed"] is True
    # duplicate close on retired should be idempotent
    res2 = herdr_runtime.close_owned_worker(reg, r["id"], None)
    assert res2["state"] in ("retired", "orphaned") or res2["closed"] is True
    # wrong native session blocks
    bad_live = {
        "pane_id": "w1:p1",
        "tab_id": "w1:t1",
        "workspace_id": "w1",
        "agent_session": {"source": "herdr:pi", "kind": "id", "value": "WRONG"},
    }
    res3 = herdr_runtime.close_owned_worker(reg, r["id"], bad_live)
    assert res3["closed"] is False
    # detach external
    det = herdr_runtime.detach_external_worker(live_full)
    assert det["detached"] is True
    # name reuse after retired: new reserve with same canonical bumps generation
    r2 = herdr_runtime.reserve_herdr_worker(
        reg, herdr_session="sess", worker_name="w-01", agent_kind="pi"
    )
    assert r2["generation"] > r["generation"] or r2["id"] != r["id"]
    # Also cover reconcile_after_crash returns none when no live
    monkeypatch.setattr(
        herdr_runtime,
        "run_herdr",
        lambda *a, **kw: herdr_runtime.HerdrResult(0, '{"result":{"agent":null}}', "", ""),
    )
    assert herdr_runtime.reconcile_after_crash(reg, "sess", "w-01") is None


def test_pane_movement_and_server_restart(tmp_path):
    """Pane movement changes pane_id; restart may clear session."""
    receipt = {
        "pane_id": "w1:p1",
        "tab_id": "w1:t1",
        "workspace_id": "w1",
        "session_source": "herdr:pi",
        "session_agent": "pi",
        "session_ref_kind": "id",
        "session_value": "abc",
    }
    live_moved = {
        "pane_id": "w1:p2",
        "tab_id": "w1:t1",
        "workspace_id": "w1",
        "agent_session": {"source": "herdr:pi", "kind": "id", "value": "abc"},
    }
    ok, _ = herdr_runtime._receipt_matches_live(receipt, live_moved)
    assert ok is False
    live_restart = {
        "pane_id": "w1:p1",
        "tab_id": "w1:t1",
        "workspace_id": "w1",
    }  # no agent_session after restart
    ok2, _ = herdr_runtime._receipt_matches_live(receipt, live_restart)
    assert ok2 is True  # missing session is not a mismatch by this check
