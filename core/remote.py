"""Registered remote servers — collect their Claude/Codex sessions over SSH.

A remote is `{name, ssh}` where `ssh` is the full command prefix (e.g.
"ssh -p 2222 user@host"). We pipe scripts/remote-collector.py to
`<ssh> python3 -`, parse its JSON, and cache it; the background poller in app.py
refreshes every ~25s (SSH is slow, so never on the 2s hot path). Cached windows
and history are merged into the board / History tagged with `source=<name>`.

Config lives at ~/.claude/fleet-remotes.json (under the user's home, NOT in the
repo — never committed). It holds SSH targets, so it stays local.
"""
from __future__ import annotations

import json
import shlex
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

from .sessions import CLAUDE_HOME

REMOTES_PATH = CLAUDE_HOME / "fleet-remotes.json"
_COLLECTOR = (Path(__file__).resolve().parents[1] / "scripts" / "remote-collector.py")

POLL_TIMEOUT = 20        # seconds per remote SSH collect
_lock = threading.Lock()
# name -> {ts, ok, error, home, windows: [...], history: [...]}
CACHE: dict[str, dict] = {}


# ---------- registry (persisted to ~/.claude/fleet-remotes.json) ----------

def load_remotes() -> list[dict]:
    try:
        data = json.loads(REMOTES_PATH.read_text(encoding="utf-8"))
        out = []
        for r in data.get("remotes", []):
            if r.get("name") and r.get("ssh"):
                out.append({"name": str(r["name"]), "ssh": str(r["ssh"])})
        return out
    except Exception:
        return []


def save_remotes(remotes: list[dict]) -> None:
    REMOTES_PATH.parent.mkdir(parents=True, exist_ok=True)
    REMOTES_PATH.write_text(json.dumps({"remotes": remotes}, indent=2), encoding="utf-8")


def add_remote(name: str, ssh: str) -> list[dict]:
    remotes = [r for r in load_remotes() if r["name"] != name]
    remotes.append({"name": name, "ssh": ssh})
    save_remotes(remotes)
    return remotes


def remove_remote(name: str) -> list[dict]:
    remotes = [r for r in load_remotes() if r["name"] != name]
    save_remotes(remotes)
    with _lock:
        CACHE.pop(name, None)
    return remotes


def ssh_for(source: str) -> Optional[str]:
    for r in load_remotes():
        if r["name"] == source:
            return r["ssh"]
    return None


def resume_path(source: str) -> str:
    """The remote's toolchain PATH (codex/claude live here), captured at collect
    time. Prepended to the resume command so a non-interactive SSH shell — which
    skips the user's profile and nvm setup — can still find the binary."""
    with _lock:
        return CACHE.get(source, {}).get("path", "")


# ---------- collect over SSH ----------

def collect(remote: dict) -> dict:
    """Run the collector on the remote, return parsed {windows, history, home}."""
    # utf-8 throughout: the collector source carries non-ASCII (title helpers), and
    # its JSON output can too — never let the platform's default codec (cp1252 on
    # Windows) decide, or it raises UnicodeDecodeError.
    src = _COLLECTOR.read_text(encoding="utf-8")
    args = shlex.split(remote["ssh"]) + [
        # BatchMode so a missing key fails fast instead of hanging on a prompt.
        "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "python3", "-",
    ]
    proc = subprocess.run(args, input=src, capture_output=True, text=True,
                          encoding="utf-8", timeout=POLL_TIMEOUT)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or "ssh failed").strip()[:300])
    return json.loads(proc.stdout)


def remote_timeline(source: str, transcript_path: str, platform: str,
                    limit: int = 2000) -> dict:
    """SSH-cat a remote transcript and parse it into timeline events locally.
    Returns {events, plan_history} (plan_history populated for codex)."""
    import os
    import tempfile
    from . import codex as _codex, transcripts as _tx
    ssh = ssh_for(source)
    if not ssh:
        raise RuntimeError(f"remote '{source}' not registered")
    args = shlex.split(ssh) + ["-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
                               "cat", shlex.quote(transcript_path)]
    proc = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                          timeout=POLL_TIMEOUT)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or "cat failed").strip()[:200])
    tmp = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8")
    try:
        tmp.write(proc.stdout)
        tmp.close()
        if platform == "codex":
            return {"events": _codex.codex_timeline(tmp.name, limit=limit),
                    "plan_history": _codex.codex_plan_history(tmp.name)}
        return {"events": _tx.timeline(tmp.name, limit=limit), "plan_history": []}
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


def poll_all() -> None:
    """Refresh CACHE for every registered remote (called by the slow poller)."""
    for r in load_remotes():
        name = r["name"]
        try:
            data = collect(r)
            entry = {"ts": _now_ms(), "ok": True, "error": "", "home": data.get("home", ""),
                     "path": data.get("path", ""),
                     "windows": data.get("windows", []), "history": data.get("history", [])}
        except Exception as e:
            with _lock:
                prev = CACHE.get(name, {})
            # keep last-good windows/history; just flag unreachable
            entry = {"ts": _now_ms(), "ok": False, "error": str(e)[:300],
                     "home": prev.get("home", ""), "path": prev.get("path", ""),
                     "windows": prev.get("windows", []), "history": prev.get("history", [])}
        with _lock:
            CACHE[name] = entry
    # drop caches for de-registered remotes
    names = {r["name"] for r in load_remotes()}
    with _lock:
        for stale in [n for n in CACHE if n not in names]:
            CACHE.pop(stale, None)


def status() -> list[dict]:
    """Per-remote status for the UI."""
    out = []
    with _lock:
        snap = dict(CACHE)
    for r in load_remotes():
        e = snap.get(r["name"], {})
        out.append({
            "name": r["name"], "ssh": r["ssh"],
            "ok": e.get("ok", False), "error": e.get("error", ""),
            "ts": e.get("ts", 0),
            "windows": len(e.get("windows", [])), "history": len(e.get("history", [])),
        })
    return out


# ---------- merged views (tagged source) ----------

def _now_ms() -> int:
    return int(time.time() * 1000)


def cached_windows() -> list[dict]:
    """Board-ready window dicts for all remotes (source-tagged, fully formed)."""
    now = _now_ms()
    out: list[dict] = []
    with _lock:
        items = [(n, e) for n, e in CACHE.items()]
    for name, e in items:
        for w in e.get("windows", []):
            cwd = w.get("cwd", "")
            updated = w.get("updated_at", 0) or 0
            idle = max(0, int((now - updated) / 1000)) if updated else None
            # Same honest rule as local codex: recent transcript write = generating.
            from .codex import CODEX_WORKING_IDLE_SEC
            triage = ("working" if (idle is not None and idle < CODEX_WORKING_IDLE_SEC)
                      else "idle")
            out.append({
                "pid": w.get("pid", 0), "session_id": w.get("session_id", ""),
                "cwd": cwd, "project_name": cwd.rsplit("/", 1)[-1] or name,
                "project_slug": "", "name": w.get("name") or w.get("first_input") or f"{w.get('platform')} @ {name}",
                "first_input": w.get("first_input", ""),
                "status": w.get("status", "running"), "waiting_for": w.get("waiting_for"),
                "started_at": updated, "updated_at": updated, "version": "",
                "tty": None,                 # remote tty can't be focused locally
                "transcript_path": w.get("transcript_path"), "alive": True,
                "platform": w.get("platform", "codex"), "model": w.get("model", ""),
                "idle_seconds": idle,
                "source": name,
                "triage": triage,
                "triage_reason": f"{name} · {'active' if triage=='working' else 'idle'}",
                "triage_suggestion": "",
                "current_task": None, "skills_used": [], "memory_ops": [],
                "background_tasks": [], "permission_msg": None, "permission_ts": None,
            })
    return out


def cached_history() -> list[dict]:
    """History rows for all remotes (source-tagged)."""
    out: list[dict] = []
    with _lock:
        items = [(n, e) for n, e in CACHE.items()]
    for name, e in items:
        for h in e.get("history", []):
            ts = h.get("first_ts", "")
            out.append({
                "session_id": h.get("session_id", ""),
                "project": h.get("project", ""),
                "project_name": h.get("project_name", "") or name,
                "first_input": h.get("first_input", ""),
                "input_count": 0,
                "first_ts": ts, "last_ts": ts,
                "transcript_path": h.get("transcript_path"),
                "transcript_size": 0,
                "transcript_mtime": h.get("transcript_mtime", 0),
                "is_alive": False,
                "platform": h.get("platform", "codex"),
                "model": h.get("model", ""),
                "skills_used": [], "memory_ops": [],
                "skill_breakdown": {}, "memory_breakdown": {},
                "metrics": {}, "plan_title": "",
                "source": name,
            })
    return out
