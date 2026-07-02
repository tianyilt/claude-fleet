"""Tail /tmp/claude-focus.log for live permission events.

The log mixes two very different notifications:
  - APPROVAL  ("X 需要授权", "needs your approval/permission") — the session is
    BLOCKED waiting for the user to approve a tool/plan. This is the red alert.
  - INPUT     ("Claude is waiting for your input") — the turn just finished; the
    session is done and idle, NOT blocked. This must NOT raise a red alert.

Each line carries a real (zh-locale) timestamp; we parse it so stale events can
be aged out instead of marking a tty "pending" forever.
"""
from __future__ import annotations

import datetime
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
# 2026年 6月24日 星期三 14时22分53秒 CST notify: project=my-project tty=/dev/ttys000 msg=Bash 需要授权
_LINE_RE = re.compile(
    r"notify:\s+project=(?P<project>\S+)\s+tty=(?P<tty>\S*)\s+msg=(?P<msg>.+?)\s*$"
)
# zh-locale timestamp prefix: 2026年 6月24日 星期三 14时22分53秒 CST
# NOTE: `date` space-pads single-digit days (%e) → "7月 1日" has a space after 月.
# The `\s*` after each unit is essential; without it single-digit-day timestamps
# (the 1st–9th of any month) fail to parse, fall back to "now", and make every
# pending session look permanently blocked. Regression: test_parse_zh_ts_single_digit.
_TS_RE = re.compile(
    r"(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日.*?(\d{1,2})时\s*(\d{1,2})分\s*(\d{1,2})秒")

# msg substrings that mean "blocked, needs the user to approve" (red alert).
_APPROVAL_HINTS = ("需要授权", "needs your approval", "needs your permission",
                   "approval for the plan", "needs your authorization")
# msg substrings that mean "done, waiting for the next prompt" (NOT blocked).
_INPUT_HINTS = ("waiting for your input",)


def _classify_kind(msg: str) -> str:
    m = msg.lower()
    if any(h.lower() in m for h in _APPROVAL_HINTS):
        return "approval"
    if any(h.lower() in m for h in _INPUT_HINTS):
        return "input"
    return "other"


def _parse_zh_ts(raw_ts: str) -> Optional[float]:
    """Parse '2026年 6月24日 ... 14时22分53秒 CST' → local epoch seconds."""
    m = _TS_RE.search(raw_ts or "")
    if not m:
        return None
    try:
        y, mo, d, hh, mm, ss = (int(x) for x in m.groups())
        return datetime.datetime(y, mo, d, hh, mm, ss).timestamp()
    except (ValueError, OverflowError):
        return None


@dataclass
class PermEvent:
    project: str
    tty: Optional[str]
    msg: str
    raw_ts: str
    epoch: float    # real event time (local epoch); falls back to file mtime
    kind: str       # approval | input | other


def _parse_line(line: str, fallback_ts: float) -> Optional[PermEvent]:
    m = _LINE_RE.search(line)
    if not m:
        return None
    raw_ts = line[: m.start()].strip()
    msg = m.group("msg").strip()
    return PermEvent(
        project=m.group("project"),
        tty=m.group("tty") or None,
        msg=msg,
        raw_ts=raw_ts,
        epoch=_parse_zh_ts(raw_ts) or fallback_ts,
        kind=_classify_kind(msg),
    )


def recent_events(limit: int = 50) -> list[PermEvent]:
    if not FOCUS_LOG.exists():
        return []
    mtime = FOCUS_LOG.stat().st_mtime
    out: list[PermEvent] = []
    try:
        text = FOCUS_LOG.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []
    for line in text.splitlines()[-limit * 2 :]:
        ev = _parse_line(line, mtime)
        if ev:
            out.append(ev)
    return out[-limit:]


def pending_by_tty(max_age_sec: float = 3600,
                   kinds: tuple = ("approval",)) -> dict[str, PermEvent]:
    """Per TTY, the most recent FRESH approval event — i.e. a session currently
    blocked waiting for the user.

    Only events of the given `kinds` (default: real approval prompts, NOT
    "waiting for your input") and within `max_age_sec` count, so a prompt from
    days ago no longer marks a tty pending forever. `max_age_sec` is generous (a
    session can sit blocked while you step away); the primary self-clear is the
    caller comparing the event time against the session's last activity, so an
    answered prompt drops off as soon as the session advances.
    """
    now = time.time()
    out: dict[str, PermEvent] = {}
    for ev in recent_events(limit=400):
        if not ev.tty or ev.kind not in kinds:
            continue
        if now - ev.epoch > max_age_sec:
            continue
        out[ev.tty] = ev          # later lines overwrite → most recent wins
    return out


def snapshot() -> dict:
    return {
        "log_exists": FOCUS_LOG.exists(),
        "events": [asdict(e) for e in recent_events(limit=30)],
        "ts": int(time.time() * 1000),
    }
