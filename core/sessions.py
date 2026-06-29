"""Read ~/.claude/sessions/*.json and enrich each with TTY + project metadata."""
from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

def _home_base() -> Path:
    """Base dir the dashboard reads from. Override with CLAUDE_FLEET_HOME to
    point at a fixture/demo tree (used for screenshots, demos, and tests)."""
    env = os.environ.get("CLAUDE_FLEET_HOME")
    return Path(env).expanduser() if env else Path.home()


HOME_BASE = _home_base()
CLAUDE_HOME = HOME_BASE / ".claude"
SESSIONS_DIR = CLAUDE_HOME / "sessions"
PROJECTS_DIR = CLAUDE_HOME / "projects"


def _cwd_to_project_slug(cwd: str) -> str:
    """Mirror Claude Code's project-dir naming: / _ . all become -"""
    return cwd.replace("/", "-").replace("_", "-").replace(".", "-")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return False
    return True


def _pid_tty(pid: int) -> Optional[str]:
    try:
        out = subprocess.check_output(
            ["ps", "-o", "tty=", "-p", str(pid)],
            stderr=subprocess.DEVNULL,
            timeout=2,
        ).decode().strip()
    except Exception:
        return None
    if not out or out == "??":
        return None
    return f"/dev/{out}"


_TTY_CACHE: dict[int, Optional[str]] = {}


def get_tty(pid: int) -> Optional[str]:
    if pid not in _TTY_CACHE:
        _TTY_CACHE[pid] = _pid_tty(pid)
    return _TTY_CACHE[pid]


def _prune_tty_cache(live_pids: set[int]) -> None:
    for pid in list(_TTY_CACHE.keys()):
        if pid not in live_pids:
            _TTY_CACHE.pop(pid, None)


@dataclass
class Window:
    pid: int
    session_id: str
    cwd: str
    project_name: str
    project_slug: str
    name: Optional[str]
    status: str           # busy | idle | waiting
    waiting_for: Optional[str]
    started_at: int       # ms
    updated_at: int       # ms
    version: str
    tty: Optional[str]
    transcript_path: Optional[str]
    alive: bool
    source: str = "local"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["idle_seconds"] = max(0, int(time.time() - self.updated_at / 1000))
        return d


def _load_session_file(path: Path) -> Optional[dict]:
    try:
        with path.open() as f:
            data = json.load(f)
    except Exception:
        return None
    if not isinstance(data, dict) or "pid" not in data:
        return None
    return data


def list_windows(include_dead: bool = False) -> list[Window]:
    if not SESSIONS_DIR.exists():
        return []

    windows: list[Window] = []
    live_pids: set[int] = set()

    for f in SESSIONS_DIR.glob("*.json"):
        # Skip the legacy `session-{ts}.json` files (no pid).
        if f.name.startswith("session-"):
            continue
        data = _load_session_file(f)
        if not data:
            continue

        pid = int(data["pid"])
        alive = _pid_alive(pid)
        if alive:
            live_pids.add(pid)
        if not alive and not include_dead:
            continue

        session_id = data.get("sessionId", "")
        cwd = data.get("cwd", "")
        slug = _cwd_to_project_slug(cwd)
        transcript = PROJECTS_DIR / slug / f"{session_id}.jsonl"

        windows.append(
            Window(
                pid=pid,
                session_id=session_id,
                cwd=cwd,
                project_name=os.path.basename(cwd) or cwd,
                project_slug=slug,
                name=data.get("name"),
                status=data.get("status", "unknown"),
                waiting_for=data.get("waitingFor"),
                started_at=int(data.get("startedAt", 0)),
                updated_at=int(data.get("updatedAt", 0)),
                version=str(data.get("version", "")),
                tty=get_tty(pid) if alive else None,
                transcript_path=str(transcript) if transcript.exists() else None,
                alive=alive,
            )
        )

    _prune_tty_cache(live_pids)

    # Newest activity first.
    windows.sort(key=lambda w: (-w.updated_at, w.pid))
    return windows


def find_window(pid: int) -> Optional[Window]:
    for w in list_windows(include_dead=True):
        if w.pid == pid:
            return w
    return None


def snapshot() -> dict:
    """Top-level state for the dashboard."""
    wins = list_windows()
    waiting = [w for w in wins if w.status == "waiting"]
    busy = [w for w in wins if w.status == "busy"]
    return {
        "windows": [w.to_dict() for w in wins],
        "counts": {
            "total": len(wins),
            "busy": len(busy),
            "waiting": len(waiting),
            "idle": len(wins) - len(busy) - len(waiting),
        },
        "ts": int(time.time() * 1000),
    }
