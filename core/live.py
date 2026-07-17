"""`claude-fleet live` — a terminal live-board of running sessions, built to sit
inside an Orca pane (or any terminal, or tmux).

Why it exists: Orca's AI Vault scans transcript files, so running sessions are
mixed into hundreds of historical ones with no "live" marker. This board is the
missing view: it polls the same pid registry the dashboard uses and shows ONLY
live sessions, refreshed in place. Press a session's key to hand it off (the
safe stop-then-resume flow from `handoff`); the resume command lands on the
clipboard ready to paste into a new pane. Orca mirrors panes to its mobile app
and forwards keystrokes, so the board doubles as a phone-side control surface.

Interactive keys need a Unix tty; without one (pipes, Windows) it degrades to a
read-only auto-refreshing board, and `--once` prints a single snapshot.
"""
from __future__ import annotations

import os
import sys
import time

from . import actions, sessions

_KEYS = "123456789abcdefghijklmnopqrstuvwxyz"
_CSI_HOME_CLEAR = "\x1b[H\x1b[2J"     # cursor home + clear screen
_BOLD, _DIM, _RESET = "\x1b[1m", "\x1b[2m", "\x1b[0m"
_RED, _GREEN, _YELLOW = "\x1b[31m", "\x1b[32m", "\x1b[33m"

_STATUS_COLOR = {"busy": _RED, "idle": _GREEN, "waiting": _YELLOW, "shell": _GREEN}


def _fmt_idle(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def _idle_seconds(w) -> int:
    return max(0, int(time.time() - w.updated_at / 1000))


def render(windows: list, message: str = "", interactive: bool = True,
           color: bool = True) -> str:
    """The full board as one string (tested without a tty)."""
    def c(code: str, text: str) -> str:
        return f"{code}{text}{_RESET}" if color else text

    lines = [c(_BOLD, f"claude-fleet live — {len(windows)} running session(s)")
             + c(_DIM, "   (handoff = graceful stop + resume command on clipboard)")]
    if interactive:
        lines.append(c(_DIM, "press a session's KEY to hand it off · f = allow busy handoff · q = quit"))
    lines.append("")
    lines.append(c(_DIM, " KEY  SESSION       PID     STATUS   IDLE   NAME"))
    for i, w in enumerate(windows):
        key = _KEYS[i] if interactive and i < len(_KEYS) else " "
        status = c(_STATUS_COLOR.get(w.status, ""), f"{w.status:<8}")
        name = (w.name or w.project_name or "")[:58]
        lines.append(f"  {key}   {w.session_id[:12]}  {w.pid:<7} {status} "
                     f"{_fmt_idle(_idle_seconds(w)):<6} {name}")
    if not windows:
        lines.append(c(_DIM, "  (no live local sessions)"))
    if message:
        lines.append("")
        lines.append(message)
    return "\n".join(lines) + "\n"


def _handoff_by_index(windows: list, index: int, force: bool, color: bool = True) -> str:
    """Run the handoff for board row `index`; returns the message to display."""
    if index >= len(windows):
        return "no session on that key"
    w = windows[index]
    result = actions.handoff_session(w.pid, force=force)
    if not result.get("ok"):
        prefix = f"{_RED}✗{_RESET} " if color else "x "
        return prefix + result.get("error", "handoff failed")
    lines = [(f"{_GREEN}✓{_RESET} " if color else "ok ")
             + f"handed off {result['session_id'][:12]} ({result.get('name') or ''})",
             f"  resume: {result['resume_command']}"]
    lines.append("  copied to clipboard — paste into a new Orca pane (or AI Vault → resume)"
                 if result.get("copied") else
                 "  paste into a new Orca pane (or AI Vault → resume)")
    return "\n".join(lines)


def _read_key_unix(timeout: float) -> str:
    import select
    r, _, _ = select.select([sys.stdin], [], [], timeout)
    if not r:
        return ""
    return sys.stdin.read(1)


def _run_interactive(interval: float) -> int:
    import termios
    import tty
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    message, force = "", False
    try:
        tty.setcbreak(fd)
        while True:
            windows = sessions.list_windows(include_dead=False)
            status = message
            if force:
                status = (status + "\n" if status else "") + \
                    f"{_YELLOW}force mode ON — busy sessions may be killed (f to turn off){_RESET}"
            sys.stdout.write(_CSI_HOME_CLEAR + render(windows, status))
            sys.stdout.flush()
            # poll keys until the next refresh tick
            deadline = time.monotonic() + interval
            while (remaining := deadline - time.monotonic()) > 0:
                ch = _read_key_unix(min(remaining, 0.25))
                if not ch:
                    continue
                if ch in ("q", "\x03"):
                    return 0
                if ch == "f":
                    force = not force
                    break
                if ch in _KEYS:
                    message = _handoff_by_index(windows, _KEYS.index(ch), force)
                    force = False
                    break
    except KeyboardInterrupt:
        return 0
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        sys.stdout.write("\n")


def _run_watch_only(interval: float) -> int:
    """No usable tty (pipe, Windows): read-only board, Ctrl-C to exit."""
    try:
        while True:
            windows = sessions.list_windows(include_dead=False)
            out = render(windows, interactive=False, color=sys.stdout.isatty())
            if sys.stdout.isatty():
                sys.stdout.write(_CSI_HOME_CLEAR + out)
            else:
                sys.stdout.write(out + "---\n")
            sys.stdout.flush()
            time.sleep(interval)
    except KeyboardInterrupt:
        return 0


def run(interval: float = 2.0, once: bool = False) -> int:
    if once:
        windows = sessions.list_windows(include_dead=False)
        sys.stdout.write(render(windows, interactive=False,
                                color=sys.stdout.isatty()))
        return 0
    if os.name != "nt" and sys.stdin.isatty():
        return _run_interactive(interval)
    return _run_watch_only(interval)
