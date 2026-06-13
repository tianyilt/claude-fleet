"""Platform-dispatched terminal control.

The headline actions (resume / fork / focus a session in a real terminal) are
inherently OS-specific. On macOS we drive iTerm2 via `osascript`. On every other
platform we don't fail loudly — we return a `fallback_cmd` the UI can show for the
user to paste into their own terminal. Spawning a native window on Windows/Linux
is a future enhancement; the function signatures stay the same when it lands.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Optional

from .sessions import CLAUDE_HOME

IS_MAC = sys.platform == "darwin"

# Focus shim resolution: a user override at ~/.claude/focus-tty.sh wins; otherwise
# the bundled cross-setup default (Terminal.app / iTerm2 / tmux) shipped in scripts/.
# (Bundled shim + override resolution contributed by @wanshuiyin, PR #1.)
_USER_FOCUS_SCRIPT = CLAUDE_HOME / "focus-tty.sh"
_BUNDLED_FOCUS_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "focus-tty.sh"


def _resolve_focus_script() -> Optional[Path]:
    if _USER_FOCUS_SCRIPT.exists():
        return _USER_FOCUS_SCRIPT
    if _BUNDLED_FOCUS_SCRIPT.exists():
        return _BUNDLED_FOCUS_SCRIPT
    return None

# When Claude Fleet is started by launchd (login item / auto-start) or as an
# orphaned background process instead of from a terminal / signed .app, its
# osascript calls have no responsible app with Automation (Apple Events)
# permission, so iTerm2/Terminal control fails with -1728 ("Can't get
# application") or -2741 (terminology can't be resolved → bogus syntax error).
_AUTOMATION_ERRNOS = ("-1728", "-2741", "-1743")

_UNSUPPORTED_MSG = (
    "Opening/focusing a terminal window is only supported on macOS (iTerm2). "
    "Copy the command below and run it in your own terminal."
)

_ITERM_SPAWN = '''tell application "iTerm2"
    activate
    set newWin to (create window with default profile)
    tell current session of newWin
        write text {cmd}
    end tell
end tell'''

_ITERM_SPAWN_THEN = '''tell application "iTerm2"
    activate
    set newWin to (create window with default profile)
    tell current session of newWin
        write text {cmd}
        delay {delay}
        write text {then_cmd}
    end tell
end tell'''


def automation_hint(stderr: str) -> Optional[str]:
    """Turn a raw osascript stderr into a user-facing error, or None if clean."""
    s = (stderr or "").strip()
    if not s:
        return None
    if any(code in s for code in _AUTOMATION_ERRNOS) or "Not authorized" in s:
        return ("macOS blocked controlling iTerm2/Terminal. Claude Fleet is likely "
                "running from launchd (auto-start) or an orphaned process without "
                "Automation permission. Launch it via the Claude Fleet.app (or "
                "./run.sh from a terminal) and approve the permission dialog, or "
                "grant it under System Settings → Privacy & Security → Automation.")
    return s


def _osa_quote(s: str) -> str:
    """Quote a shell command string as an AppleScript string literal."""
    return '"' + s.replace('\\', '\\\\').replace('"', '\\"') + '"'


def spawn_window(inner_cmd: str, then_cmd: Optional[str] = None, then_delay: int = 3) -> dict:
    """Open a new terminal window running `inner_cmd` (optionally then `then_cmd`).

    macOS → new iTerm2 window. Other platforms → graceful degradation with a
    `fallback_cmd` for the user to run manually.
    """
    if not IS_MAC:
        return {"ok": False, "unsupported": True,
                "fallback_cmd": inner_cmd, "error": _UNSUPPORTED_MSG}
    if then_cmd is not None:
        script = _ITERM_SPAWN_THEN.format(
            cmd=_osa_quote(inner_cmd), then_cmd=_osa_quote(then_cmd), delay=then_delay)
    else:
        script = _ITERM_SPAWN.format(cmd=_osa_quote(inner_cmd))
    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=15,
        )
    except Exception as e:
        return {"ok": False, "error": str(e), "fallback_cmd": inner_cmd}
    hint = automation_hint(proc.stderr)
    res = {"ok": proc.returncode == 0, "error": hint}
    if not res["ok"]:
        res["fallback_cmd"] = inner_cmd
    return res


def focus(tty: str) -> dict:
    """Activate the terminal tab that owns `tty`. macOS only.

    Runs the resolved focus shim and interprets its exit codes:
    0 focused · 2 usage · 3 tmux session detached · 4 no matching tab ·
    5 Automation permission denied · 6 unsupported platform.
    """
    if not IS_MAC:
        return {"ok": False, "unsupported": True, "error": _UNSUPPORTED_MSG}
    if not tty:
        return {"ok": False, "error": "no tty"}
    script = _resolve_focus_script()
    if script is None:
        return {"ok": False,
                "error": f"no focus-tty.sh found (looked at {_USER_FOCUS_SCRIPT} "
                         f"and {_BUNDLED_FOCUS_SCRIPT})"}
    # Direct exec respects the shim's shebang; if the +x bit was lost, retry via
    # bash. Shield everything so focus NEVER raises — a blocking Automation prompt
    # (TimeoutExpired) must return a structured error, not a 500.
    try:
        try:
            proc = subprocess.run([str(script), tty], capture_output=True, text=True, timeout=10)
        except PermissionError:
            proc = subprocess.run(["bash", str(script), tty], capture_output=True, text=True, timeout=10)
    except subprocess.TimeoutExpired:
        return {"ok": False, "code": None, "error": "focus timed out after 10s"}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    code = proc.returncode
    if code == 0:
        return {"ok": True, "code": 0}
    if code == 5:  # Automation permission denied
        return {"ok": False, "code": 5, "error": automation_hint("-1743")}
    if code == 6:  # not macOS / no osascript
        return {"ok": False, "code": 6, "unsupported": True, "error": _UNSUPPORTED_MSG}
    # 2 usage · 3 tmux detached · 4 no matching tab · other
    return {"ok": False, "code": code,
            "error": (proc.stderr.strip() or "window not found for that session")}
