"""Tail /tmp/claude-focus.log for live permission events."""
from __future__ import annotations

import os
import re
import tempfile
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

# focus-tty.sh and the notify hook write to the literal /tmp/claude-focus.log on
# POSIX; only divert to the platform temp dir where /tmp doesn't exist (Windows).
FOCUS_LOG = (Path("/tmp/claude-focus.log") if os.name == "posix"
             else Path(tempfile.gettempdir()) / "claude-focus.log")

# Sample line (from existing notify.sh):
# 2026-01-01 12:00:00 CST notify: project=my-project tty=/dev/ttys000 msg=Bash 需要授权
_LINE_RE = re.compile(
    r"notify:\s+project=(?P<project>\S+)\s+tty=(?P<tty>\S*)\s+msg=(?P<msg>.+?)\s*$"
)


@dataclass
class PermEvent:
    project: str
    tty: Optional[str]
    msg: str
    raw_ts: str
    epoch: float  # seconds; approximate (parsed from file mtime if no good ts)


def _parse_line(line: str, fallback_ts: float) -> Optional[PermEvent]:
    m = _LINE_RE.search(line)
    if not m:
        return None
    raw_ts = line[: m.start()].strip()
    tty = m.group("tty") or None
    return PermEvent(
        project=m.group("project"),
        tty=tty,
        msg=m.group("msg").strip(),
        raw_ts=raw_ts,
        epoch=fallback_ts,
    )


def recent_events(limit: int = 50) -> list[PermEvent]:
    if not FOCUS_LOG.exists():
        return []
    mtime = FOCUS_LOG.stat().st_mtime
    out: list[PermEvent] = []
    try:
        text = FOCUS_LOG.read_text(errors="replace")
    except Exception:
        return []
    for line in text.splitlines()[-limit * 2 :]:
        ev = _parse_line(line, mtime)
        if ev:
            out.append(ev)
    return out[-limit:]


def pending_by_tty() -> dict[str, PermEvent]:
    """For each TTY, the most recent 'needs authorization' event seen.

    The session-level `status == "waiting"` is the source of truth for whether
    a window is *currently* blocked; this map just enriches the dashboard with
    the human-readable reason (the exact text the user saw in the toast).
    """
    out: dict[str, PermEvent] = {}
    for ev in recent_events(limit=200):
        if not ev.tty:
            continue
        out[ev.tty] = ev
    return out


def snapshot() -> dict:
    return {
        "log_exists": FOCUS_LOG.exists(),
        "events": [asdict(e) for e in recent_events(limit=30)],
        "ts": int(time.time() * 1000),
    }
