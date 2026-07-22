"""Native desktop notifications when a session needs you.

Fires a macOS notification (osascript) when a session transitions INTO
`waiting_perm` (needs your approval) or `completed` (finished a turn). Runs from
the backend watcher, so it reaches you whether or not a browser tab is open —
unlike an in-page Notification. No-op off macOS or when FLEET_NOTIFY=0.

Only the *edge* into a state fires — re-notifying every 2s snapshot would be
noise. The first snapshot after startup only seeds state (so you don't get
blasted for every already-waiting/already-done session on boot).
"""
from __future__ import annotations

import os
import subprocess
import sys

# triage state -> short zh label shown in the notification title
_NOTIFY_STATES = {"waiting_perm": "需要授权", "completed": "已完成"}

_prev: dict[str, str] = {}   # session_id -> last-seen triage
_seeded = False              # first snapshot seeds without notifying


def _reset() -> None:
    """Test hook: clear accumulated state."""
    global _seeded
    _prev.clear()
    _seeded = False


def _enabled() -> bool:
    return sys.platform == "darwin" and os.environ.get("FLEET_NOTIFY", "1") != "0"


def _osascript(title: str, subtitle: str, body: str) -> None:
    def esc(s: str) -> str:
        return (str(s) or "").replace("\\", "\\\\").replace('"', '\\"')[:180]
    script = (f'display notification "{esc(body)}" '
              f'with title "{esc(title)}" subtitle "{esc(subtitle)}"')
    try:
        subprocess.Popen(["osascript", "-e", script],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def notify_transitions(windows: list[dict]) -> None:
    """Call once per snapshot. Fires on edges into waiting_perm / completed."""
    global _seeded
    enabled = _enabled()
    seen = set()
    for w in windows:
        sid = w.get("session_id") or str(w.get("pid"))
        seen.add(sid)
        cur = w.get("triage", "")
        old = _prev.get(sid)
        _prev[sid] = cur
        if not enabled or not _seeded:
            continue   # disabled → just track; first pass → seed, don't blast
        if cur in _NOTIFY_STATES and cur != old:
            label = _NOTIFY_STATES[cur]
            name = w.get("name") or w.get("project_name") or (w.get("session_id") or "")[:8]
            _osascript(f"Fleet · {label}", str(name), w.get("triage_reason", ""))
    # Forget dead sessions so a reused pid / re-run re-notifies cleanly.
    for sid in list(_prev):
        if sid not in seen:
            _prev.pop(sid, None)
    _seeded = True
