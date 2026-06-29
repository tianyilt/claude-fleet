"""Platform-dispatched terminal control.

Two headline actions, both OS-specific:

- launch (resume / fork a saved session in a real terminal window): cross-platform
  via `open -a` (macOS, no Apple Events / no Automation permission needed), native
  terminal emulators (Linux), or a `CLAUDE_FLEET_TERMINAL_CMD` override. Supports
  both Claude and Codex sessions. When no launcher is available it returns the
  `command` for the UI to surface for manual paste.
  (Cross-platform launcher + Codex support contributed by @ppolariss, PR #5.)
- focus (raise the tab owning a tty): macOS-only, via the bundled focus-tty.sh
  shim (@wanshuiyin, PR #1).
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import uuid
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
    "Couldn't open a terminal window automatically here. "
    "Copy the command below and run it in your own terminal."
)


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


# ---------------------------------------------------------------------------
# Launch a saved session in a real terminal window (PR #5, @ppolariss).
#
# macOS uses `open -a <Terminal app> <script>.command` via LaunchServices — this
# does NOT send Apple Events, so it works even from a launchd/orphaned server with
# no Automation permission (unlike osascript window-creation). Linux uses the first
# available terminal emulator. CLAUDE_FLEET_TERMINAL_CMD overrides everything.
# ---------------------------------------------------------------------------

def session_cli_command(platform: str, session_id: str, cwd: str, fork: bool = False) -> str:
    """Build the interactive CLI command for resuming/forking a saved session."""
    platform = (platform or "claude").lower()
    cwd = cwd or str(Path.home())
    if platform == "claude":
        args = ["claude", "--resume", session_id]
        if fork:
            args.append("--fork-session")
    elif platform == "codex":
        args = ["codex", "fork" if fork else "resume", session_id]
    else:
        raise ValueError(f"{platform} sessions cannot be resumed from this dashboard yet")
    return f"cd {shlex.quote(cwd)} && {shlex.join(args)}"


def remote_session_command(ssh: str, platform: str, session_id: str,
                           cwd: str, fork: bool = False) -> str:
    """Resume/fork a session on a remote host: open a LOCAL terminal that SSHes in
    with a tty (-t) and runs the resume command there. `ssh` is the full prefix
    (e.g. 'ssh -p 2222 user@host')."""
    inner = session_cli_command(platform, session_id, cwd, fork=fork)
    return f"{ssh} -t {shlex.quote(inner)}"


def _configured_terminal_command(command: str, cwd: str) -> Optional[list[str]]:
    """User override, e.g. CLAUDE_FLEET_TERMINAL_CMD='tmux new-window -c {cwd} {cmd}'."""
    template = os.environ.get("CLAUDE_FLEET_TERMINAL_CMD", "").strip()
    if not template:
        return None
    rendered = template.format(
        cmd=shlex.quote(command), cwd=shlex.quote(cwd), raw_cmd=command, raw_cwd=cwd)
    return ["/bin/sh", "-lc", rendered]


def _user_shell() -> str:
    shell = os.environ.get("SHELL", "")
    if shell and Path(shell).exists():
        return shell
    return "/bin/sh"


def _shell_args(command: str) -> list[str]:
    return [_user_shell(), "-lc", command]


def _keepalive_script(command: str) -> str:
    """A .command body that runs `command` WITHOUT exec and holds the window open
    if it exits too fast to read.

    The old body was `exec <shell> -lc <cmd>`: exec replaced the shell, so when
    the CLI died the terminal had nothing left and closed instantly. A failed
    resume (`claude --resume <id>` → "No conversation found", which exits 0) thus
    flash-closed before the error could be read. Without exec, and with a sub-3s
    guard, any quick exit leaves the message on screen behind a paused prompt.
    Genuine interactive sessions run far longer than 3s, so they close normally.
    """
    inner = shlex.join(_shell_args(command))
    return (
        "#!/bin/bash\n"
        "start=$SECONDS\n"
        f"{inner}\n"
        "code=$?\n"
        'if [ $(($SECONDS - start)) -lt 3 ]; then\n'
        '  echo\n'
        '  echo "[claude-fleet] command exited ($code) immediately — see above."\n'
        '  echo "Press Enter to close this window."\n'
        '  read -r\n'
        "fi\n"
    )


def _macos_terminal_command(command: str, cwd: str) -> Optional[list[str]]:
    if not IS_MAC or not shutil.which("open"):
        return None
    # Write a throwaway .command script and open it via LaunchServices in the
    # user's DEFAULT terminal (whatever handles .command — Terminal.app by
    # default, or iTerm2 / Warp if they set it). No `-a` so we don't force a
    # particular app, and no osascript → no Automation prompt. To pin a specific
    # terminal, set CLAUDE_FLEET_TERMINAL_CMD or change the .command default app.
    script = Path(tempfile.gettempdir()) / f"claude-fleet-{uuid.uuid4().hex}.command"
    script.write_text(_keepalive_script(command), encoding="utf-8")
    script.chmod(0o700)
    return ["open", str(script)]


def _linux_terminal_command(command: str, cwd: str) -> Optional[list[str]]:
    if not sys.platform.startswith("linux"):
        return None
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return None
    launchers = [
        ("x-terminal-emulator", ["x-terminal-emulator", "-e", *_shell_args(command)]),
        ("gnome-terminal", ["gnome-terminal", "--working-directory", cwd, "--", *_shell_args(command)]),
        ("konsole", ["konsole", "--workdir", cwd, "-e", *_shell_args(command)]),
        ("xfce4-terminal", ["xfce4-terminal", "--working-directory", cwd, "-e", shlex.join(_shell_args(command))]),
        ("alacritty", ["alacritty", "--working-directory", cwd, "-e", *_shell_args(command)]),
        ("kitty", ["kitty", "--directory", cwd, *_shell_args(command)]),
        ("wezterm", ["wezterm", "start", "--cwd", cwd, "--", *_shell_args(command)]),
        ("xterm", ["xterm", "-e", *_shell_args(command)]),
    ]
    for exe, argv in launchers:
        if shutil.which(exe):
            return argv
    return None


def _terminal_command(command: str, cwd: str) -> Optional[list[str]]:
    return (
        _configured_terminal_command(command, cwd)
        or _linux_terminal_command(command, cwd)
        or _macos_terminal_command(command, cwd)
    )


def launch_session(platform: str, session_id: str, cwd: str, fork: bool = False,
                   ssh: Optional[str] = None) -> dict:
    """Launch an interactive CLI session in a real terminal when possible.

    Returns {ok: True, action, command, ...} on success, or {ok: False, command,
    error} when there's no launcher (the UI offers `command` for manual paste).
    When `ssh` is given, the session lives on a remote host: open a local terminal
    that SSHes in and resumes there.
    """
    cwd = cwd or str(Path.home())
    try:
        command = (remote_session_command(ssh, platform, session_id, cwd, fork=fork)
                   if ssh else session_cli_command(platform, session_id, cwd, fork=fork))
    except ValueError as e:
        return {"ok": False, "error": str(e)}

    term_cmd = _terminal_command(command, cwd)
    if not term_cmd:
        return {"ok": False, "error": _UNSUPPORTED_MSG,
                "command": command, "platform": platform}
    try:
        subprocess.Popen(
            term_cmd,
            cwd=cwd if Path(cwd).is_dir() else None,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as e:
        return {"ok": False, "error": str(e), "command": command, "platform": platform}
    return {"ok": True, "action": "forked" if fork else "resumed",
            "session_id": session_id, "cwd": cwd, "platform": platform, "command": command}


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
