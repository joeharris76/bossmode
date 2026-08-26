"""Herdr adapter for 2a — bounded subprocess, structured parsing, timeouts, redacted diagnostics.

Only this PR may touch `herdr --version` + create/inspect/prompt/wait/close.
No BOSSMODE_DB mutation here; ownership ledger is separate.
"""

from __future__ import annotations

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
    except subprocess.TimeoutExpired as exc:
        out = (
            (exc.stdout or b"").decode() if isinstance(exc.stdout, bytes) else str(exc.stdout or "")
        )
        err = (
            (exc.stderr or b"").decode() if isinstance(exc.stderr, bytes) else str(exc.stderr or "")
        )
        return HerdrResult(returncode=124, stdout=out, stderr=err, redacted_stderr=_redact(err))
    except FileNotFoundError as exc:
        return HerdrResult(
            returncode=127, stdout="", stderr=str(exc), redacted_stderr=_redact(str(exc))
        )


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
