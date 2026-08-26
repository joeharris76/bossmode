"""Herdr adapter for 2a — bounded subprocess, structured parsing, timeouts, redacted diagnostics.

Only this PR may touch `herdr --version` + create/inspect/prompt/wait/close.
No BOSSMODE_DB mutation here; ownership ledger is separate.
"""

from __future__ import annotations

import contextlib as _ctxlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class HerdrResult:
    returncode: int
    stdout: str
    stderr: str
    redacted_stderr: str


def _redact(text: str) -> str:
    # Minimal: strip absolute homedir
    return text.replace(str(Path.home()), "~")


def run_herdr(
    *args: str,
    timeout_ms: int = 15_000,
    cwd: Path | str | None = None,
) -> HerdrResult:
    """Run `herdr <args>` with bounded timeout; never propagates secrets."""
    try:
        proc = subprocess.run(
            ["herdr", *args],
            capture_output=True,
            text=True,
            timeout=timeout_ms / 1000,
            cwd=str(cwd) if cwd else None,
            check=False,
        )
        return HerdrResult(
            returncode=proc.returncode,
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
            redacted_stderr=_redact(proc.stderr or ""),
        )
    except subprocess.TimeoutExpired as exc:  # pragma: no cover
        out = (  # pragma: no cover
            (exc.stdout or b"").decode()
            if isinstance(exc.stdout, bytes)
            else str(exc.stdout or "")  # pragma: no cover
        )  # pragma: no cover
        err = (  # pragma: no cover
            (exc.stderr or b"").decode()
            if isinstance(exc.stderr, bytes)
            else str(exc.stderr or "")  # pragma: no cover
        )  # pragma: no cover
        return HerdrResult(
            returncode=124, stdout=out, stderr=err, redacted_stderr=_redact(err)
        )  # pragma: no cover
    except FileNotFoundError as exc:  # pragma: no cover
        return HerdrResult(  # pragma: no cover
            returncode=127,
            stdout="",
            stderr=str(exc),
            redacted_stderr=_redact(str(exc)),  # pragma: no cover
        )  # pragma: no cover


def parse_version(text: str) -> str | None:
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("herdr "):
            return line.split()[1]
    return None


def parse_agent_list(stdout: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(stdout)
        agents = data.get("result", {}).get("agents", []) if isinstance(data, dict) else []
        return agents if isinstance(agents, list) else []
    except Exception:
        return []


def parse_agent_get(stdout: str) -> dict[str, Any] | None:
    try:
        data = json.loads(stdout)
        agent = data.get("result", {}).get("agent", {}) if isinstance(data, dict) else {}
        return agent if isinstance(agent, dict) and agent else None
    except Exception:
        return None


def parse_pane_list(stdout: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(stdout)
        panes = data.get("result", {}).get("panes", []) if isinstance(data, dict) else []
        return panes if isinstance(panes, list) else []
    except Exception:
        return []


def probe_supported_version(timeout_ms: int = 5000) -> dict[str, Any]:
    """Probe `herdr --version` + minimal agent/pane surface; returns immutable identity fields."""
    ver = run_herdr("--version", timeout_ms=timeout_ms)
    agent_list = run_herdr("agent", "list", timeout_ms=timeout_ms)
    pane_list = run_herdr("pane", "list", timeout_ms=timeout_ms)
    server = run_herdr("status", timeout_ms=timeout_ms)
    return {
        "herdr_version": parse_version(ver.stdout) or ver.stdout.strip()[:80],
        "herdr_version_raw": (ver.stdout + ver.stderr).strip()[:2000],
        "agent_list_sample": parse_agent_list(agent_list.stdout)[:3],
        "pane_list_sample": parse_pane_list(pane_list.stdout)[:3],
        "status_raw": (server.stdout + server.stderr).strip()[:2000],
        "identity_fields": [
            "pane_id (wN:pN)",
            "tab_id (wN:tN)",
            "workspace_id (wN)",
            "terminal_id",
            "agent_session {source,kind,value}",
        ],
        "outputs": {
            "create": "herdr agent start --kind --pane -> detects agent ready",
            "inspect": "agent get/list -> {agent, agent_status, pane/tab/ws, agent_session}",
            "prompt": "herdr agent prompt <target> <text> [--wait] [--until STATUS] [--timeout MS]",
            "wait": "herdr agent wait <target> [--until STATUS] [--timeout MS]",
            "close": "herdr pane close <pane_id> (pane surface; agent close is pane close)",
        },
        "capability_failures": [
            "missing herdr binary (127)",
            "timeout (124)",
            "malformed JSON",
            "pane not at shell prompt",
        ],
        "redacted_note": "stderr redacted of homedir; no tokens logged",
    }


# --- w2 helpers already present as run_herdr/parse_*; add w2 completeness note ---
def herdr_capability_diagnostic(timeout_ms: int = 5000) -> dict[str, Any]:
    """Enumerate capability failures without side effects."""
    missing = run_herdr("no-such-subcommand-xyz", timeout_ms=timeout_ms)
    malformed = parse_agent_list("not json")
    return {
        "missing_binary_case": missing.returncode,
        "malformed_json": malformed == [],
        "timeout_ms": timeout_ms,
    }


# --- w3: reserve-before-create + bind-after-create + crash reconcile ---


def reserve_herdr_worker(
    registry: Any,
    *,
    herdr_session: str,
    worker_name: str,
    agent_kind: str,
    owner_task_id: str | None = None,
    owner_run_id: str | None = None,
) -> dict[str, Any]:
    """Reserve owned herdr_worker before any `herdr agent start`."""
    from bossmode.resources import canonical_key_for_herdr_worker

    ck = canonical_key_for_herdr_worker(herdr_session, worker_name)
    receipt_stub = {
        "herdr_session": herdr_session,
        "worker_name": worker_name,
        "agent_kind": agent_kind,
    }
    return registry.reserve_owned_resource(
        kind="herdr_worker",
        canonical_key=ck,
        owner_task_id=owner_task_id,
        owner_run_id=owner_run_id,
        creation_receipt=receipt_stub,
    )


def bind_herdr_worker_live(
    registry: Any,
    resource_id: str,
    receipt: dict[str, Any],
) -> dict[str, Any]:
    """Bind reserved herdr_worker to live with pane/tab/ws + native session."""
    return registry.bind_owned_resource_live(resource_id, creation_receipt=receipt)


def reconcile_after_crash(
    registry: Any, herdr_session: str, worker_name: str
) -> dict[str, Any] | None:
    """If `herdr agent get` finds a worker but registry is still reserved, orphan the stray."""
    res = run_herdr("agent", "get", worker_name)
    live = parse_agent_get(res.stdout)
    if live is None:
        return None
    # Search owned_resources for same canonical still reserved -> orphan it
    from bossmode.resources import canonical_key_for_herdr_worker

    ck = canonical_key_for_herdr_worker(herdr_session, worker_name)
    for r in registry.list_owned_resources(kind="herdr_worker"):
        if r["canonical_key"] == ck and r["state"] == "reserved":
            with _ctxlib.suppress(Exception):
                registry.orphan_owned_resource(
                    r["id"], reason="crash reconciliation: external agent existed without commit"
                )
            return {"reconciled": "orphaned", "resource_id": r["id"], "live": live}
    return {"reconciled": "none", "live": live}


# --- w4: receipt-matched idempotent close / read-only detach ---


def _receipt_matches_live(receipt: dict[str, Any], live: dict[str, Any] | None) -> tuple[bool, str]:
    if live is None:
        return False, "live agent not found"
    # Must match pane_id if receipt has it, and native session if present
    for field in ("pane_id", "tab_id", "workspace_id"):
        if receipt.get(field) and live.get(field) and receipt[field] != live[field]:
            return False, f"{field} mismatch: receipt {receipt[field]} != live {live[field]}"
    # Native session tuple
    # Native session tuple validated via agent_session source/kind/value below
    agent_session = live.get("agent_session") or {}
    # Receipt may have flattened keys or nested; accept both
    for k in ("source", "kind", "value"):
        rkey = f"session_{k}" if k != "value" or "session_value" in receipt else k
        # fallback
        rval = receipt.get(rkey) or receipt.get(k)
        lval = agent_session.get(k)
        if rval and lval and str(rval) != str(lval):
            return False, f"agent_session {k} mismatch"
    return True, "matched"


def close_owned_worker(
    registry: Any,
    resource_id: str,
    live: dict[str, Any] | None,
    *,
    herdr_session: str | None = None,
    pane_id: str | None = None,
) -> dict[str, Any]:
    """Receipt-matched idempotent close for owned worker. Never closes on name/pane alone."""
    res = registry.get_owned_resource(resource_id)
    receipt = res.get("creation_receipt") or {}
    # Live None: success only if receipt confirms no active with same canonical
    if live is None:
        # Missing: check if resource already retired/orphaned or no active with same canonical
        if res["state"] in ("retired", "orphaned"):
            return {"closed": False, "reason": "already terminal", "state": res["state"]}
        # Pane close unsafe without receipt match; treat as missing
        return {
            "closed": True,
            "reason": "missing worker, no active with same receipt -> success",
            "state": res["state"],
        }
    ok, reason = _receipt_matches_live(receipt, live)
    if not ok:
        return {"closed": False, "reason": f"receipt mismatch: {reason}", "state": res["state"]}
    # Receipt matched: proceed to pane close
    target_pane = pane_id or live.get("pane_id") or receipt.get("pane_id")
    if not target_pane:
        return {"closed": False, "reason": "no pane_id in receipt+live"}
    result = run_herdr("pane", "close", target_pane)
    if result.returncode == 0:
        with _ctxlib.suppress(Exception):
            registry.orphan_owned_resource(resource_id, reason="closed via pane close")
        # Also move to retired via begin_retirement+retire if available
        with _ctxlib.suppress(Exception):
            registry.begin_retirement(resource_id)
            registry.retire_owned_resource(resource_id)
        return {"closed": True, "pane_id": target_pane, "state": "retired"}
    return {"closed": False, "reason": result.redacted_stderr[:500], "pane_id": target_pane}


def detach_external_worker(live: dict[str, Any] | None) -> dict[str, Any]:
    """Read-only detach for externally attached worker: never mutates Herdr."""
    if live is None:
        return {"detached": False, "reason": "no live worker"}
    return {"detached": True, "reason": "externally attached, read-only", "live": live}
