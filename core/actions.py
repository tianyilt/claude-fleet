"""Side-effectful actions: focus, fork, export, close, review."""
from __future__ import annotations

import os
import signal
import shlex
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Optional

from .sessions import CLAUDE_HOME, find_window
from .transcripts import timeline, extract_plan_history, extract_skills_used, extract_memory_ops

# Focus shim resolution: a user override at ~/.claude/focus-tty.sh wins; otherwise
# the bundled cross-setup default (Terminal.app / iTerm2 / tmux) shipped with the repo.
_USER_FOCUS_SCRIPT = CLAUDE_HOME / "focus-tty.sh"
_BUNDLED_FOCUS_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "focus-tty.sh"


def _resolve_focus_script() -> Optional[Path]:
    if _USER_FOCUS_SCRIPT.exists():
        return _USER_FOCUS_SCRIPT
    if _BUNDLED_FOCUS_SCRIPT.exists():
        return _BUNDLED_FOCUS_SCRIPT
    return None


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


def _configured_terminal_command(command: str, cwd: str) -> Optional[list[str]]:
    """Allow users to override terminal launch without changing code.

    Example:
      CLAUDE_FLEET_TERMINAL_CMD='tmux new-window -c {cwd} {cmd}'
    """
    template = os.environ.get("CLAUDE_FLEET_TERMINAL_CMD", "").strip()
    if not template:
        return None
    rendered = template.format(
        cmd=shlex.quote(command),
        cwd=shlex.quote(cwd),
        raw_cmd=command,
        raw_cwd=cwd,
    )
    return ["/bin/sh", "-lc", rendered]


def _user_shell() -> str:
    shell = os.environ.get("SHELL", "")
    if shell and Path(shell).exists():
        return shell
    return "/bin/sh"


def _shell_args(command: str) -> list[str]:
    return [_user_shell(), "-lc", command]


def _macos_terminal_command(command: str, cwd: str) -> Optional[list[str]]:
    if sys.platform != "darwin" or not shutil.which("open"):
        return None

    script = Path(tempfile.gettempdir()) / f"claude-fleet-{uuid.uuid4().hex}.command"
    script.write_text(f"#!/bin/bash\nexec {shlex.join(_shell_args(command))}\n", encoding="utf-8")
    script.chmod(0o700)
    return ["open", "-a", "Terminal", str(script)]


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


def launch_session(platform: str, session_id: str, cwd: str, fork: bool = False) -> dict:
    """Launch an interactive CLI session in a real terminal when possible."""
    cwd = cwd or str(Path.home())
    try:
        command = session_cli_command(platform, session_id, cwd, fork=fork)
    except ValueError as e:
        return {"ok": False, "error": str(e)}

    term_cmd = _terminal_command(command, cwd)
    if not term_cmd:
        return {
            "ok": False,
            "error": "no supported terminal launcher found; run the command manually",
            "command": command,
            "platform": platform,
        }

    try:
        subprocess.Popen(
            term_cmd,
            cwd=cwd if Path(cwd).is_dir() else None,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as e:
        return {"ok": False, "error": str(e), "command": command, "platform": platform}

    return {
        "ok": True,
        "action": "forked" if fork else "resumed",
        "session_id": session_id,
        "cwd": cwd,
        "platform": platform,
        "command": command,
    }


def focus_terminal(tty: str) -> dict:
    """Activate the terminal tab that owns `tty`.

    Prefers a user override at ~/.claude/focus-tty.sh; falls back to the bundled
    scripts/focus-tty.sh, which handles plain Terminal.app / iTerm2 tabs and tmux
    panes on macOS out of the box.
    """
    if not tty:
        return {"ok": False, "error": "no tty"}
    script = _resolve_focus_script()
    if script is None:
        return {
            "ok": False,
            "error": f"no focus-tty.sh found (looked at {_USER_FOCUS_SCRIPT} and {_BUNDLED_FOCUS_SCRIPT})",
        }
    # Direct exec respects the script's own shebang (matches the original behavior
    # and any user override). If the +x bit was lost on an odd checkout, retry via
    # bash (covers bash/POSIX scripts; a non-bash override should keep its +x).
    # The whole thing is shielded so focus_terminal NEVER raises — a TimeoutExpired
    # (e.g. a blocking macOS Automation prompt) or a missing `bash` must return the
    # structured error, not bubble up as a 500 in the request handler.
    try:
        try:
            proc = subprocess.run(
                [str(script), tty],
                capture_output=True, text=True, timeout=10,
            )
        except PermissionError:
            proc = subprocess.run(
                ["bash", str(script), tty],
                capture_output=True, text=True, timeout=10,
            )
    except subprocess.TimeoutExpired:
        # stable contract: the child (e.g. a blocking Automation prompt) was killed
        return {"ok": False, "error": "focus timed out after 10s", "code": None}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    # `code` lets the UI distinguish the script's exit codes (3 detached / 4 no-tab
    # / 5 permission-denied / 6 unsupported) instead of a generic failure.
    return {
        "ok": proc.returncode == 0,
        "code": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }


def fork_session(pid: int) -> dict:
    """Open a terminal and fork the live Claude session."""
    w = find_window(pid)
    if not w:
        return {"ok": False, "error": f"no window pid={pid}"}
    return launch_session("claude", w.session_id, w.cwd, fork=True)


def _render_session_markdown(pid: int, limit: int = 80) -> Optional[tuple[str, str]]:
    w = find_window(pid)
    if not w or not w.transcript_path:
        return None
    events = timeline(w.transcript_path, limit=limit)
    title = w.name or w.project_name or f"session-{w.session_id[:8]}"
    plan_hist = extract_plan_history(w.transcript_path)
    skills = extract_skills_used(w.transcript_path)
    mem_ops = extract_memory_ops(w.transcript_path)

    lines: list[str] = [
        f"# {title}",
        "",
        f"- project: `{w.cwd}`",
        f"- session: `{w.session_id}`",
        f"- pid: {w.pid} · status: {w.status} · version: {w.version}",
        f"- transcript: `{w.transcript_path}`",
    ]
    if skills:
        lines.append(f"- skills: {', '.join(skills)}")
    if mem_ops:
        ops_str = ", ".join(f"{'↓' if m['operation']=='read' else '↑'}{m['name']}" for m in mem_ops)
        lines.append(f"- memory: {ops_str}")
    lines.append("")

    if plan_hist:
        lines.append("## Plan 历史")
        lines.append("")
        for ph in plan_hist:
            ts = (ph.get("ts") or "")[:19]
            lines.append(f"### {ph['version_label']} — {ts} ({ph['plan_file']})")
            lines.append("")
            if ph["operation"] == "write" and ph.get("content"):
                lines.append("```")
                lines.append(ph["content"][:5000])
                lines.append("```")
            elif ph["operation"] == "edit" and ph.get("diff"):
                lines.append("```diff")
                lines.append(f"- {ph['diff']['old'][:1000]}")
                lines.append(f"+ {ph['diff']['new'][:1000]}")
                lines.append("```")
            lines.append("")

    lines.append("## 时间线")
    lines.append("")
    for ev in events:
        ts = (ev.get("ts") or "")[:19]
        kind = ev["kind"]
        if kind == "user_text":
            lines.append(f"### 👤 user `{ts}`")
            lines.append("")
            lines.append(ev["text"])
            lines.append("")
        elif kind == "assistant_text":
            lines.append(f"### 🤖 assistant `{ts}`")
            lines.append("")
            lines.append(ev["text"])
            lines.append("")
        elif kind == "tool_use":
            extras = ", ".join(f"{k}={v!r}" for k, v in ev.get("extra", {}).items())
            lines.append(f"- 🔧 `{ev['tool']}({extras})` `{ts}`")
        elif kind == "tool_result":
            snippet = (ev.get("text") or "").replace("\n", " ")[:120]
            lines.append(f"  - ↳ result: `{snippet}…`")
    return title, "\n".join(lines)


_EXPORT_MD = Path("/tmp/fleet-export.md")


def export_to_feishu(pid: int) -> dict:
    """Render session markdown and create a Feishu doc via lark-fnlp."""
    rendered = _render_session_markdown(pid)
    if not rendered:
        return {"ok": False, "error": "no session"}
    title, md = rendered

    _EXPORT_MD.write_text(md, encoding="utf-8")

    quoted_title = shlex.quote(title)
    cmd = (
        f"source ~/.zshrc 2>/dev/null; "
        f"cd /tmp && lark-fnlp docs +create "
        f"--title {quoted_title} "
        f"--markdown @./fleet-export.md "
        f"--as bot"
    )
    try:
        proc = subprocess.run(
            ["zsh", "-c", cmd],
            capture_output=True, text=True, timeout=60,
        )
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        _EXPORT_MD.unlink(missing_ok=True)

    doc_url = None
    if proc.returncode == 0:
        import json as _json
        try:
            result = _json.loads(proc.stdout)
            doc_url = result.get("data", {}).get("doc_url")
        except Exception:
            pass

    return {
        "ok": proc.returncode == 0,
        "title": title,
        "doc_url": doc_url,
        "stdout": proc.stdout.strip()[-2000:],
        "stderr": proc.stderr.strip()[-2000:],
        "rc": proc.returncode,
    }


def close_session(pid: int) -> dict:
    """Gracefully terminate a Claude Code session by PID."""
    w = find_window(pid)
    if not w:
        return {"ok": False, "error": f"no window pid={pid}"}
    if not w.alive:
        return {"ok": True, "already_dead": True}
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return {"ok": True, "already_dead": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "pid": pid, "name": w.name or w.project_name}


_review_results: dict[int, dict] = {}


def _build_review_summary(transcript_path: str, limit: int = 40) -> str:
    """Extract last N turns as compact text for review prompt."""
    from .transcripts import timeline
    events = timeline(transcript_path, limit=limit)
    lines: list[str] = []
    for ev in events:
        kind = ev.get("kind", "")
        ts = (ev.get("ts") or "")[:19]
        if kind == "user_text":
            lines.append(f"[USER {ts}] {ev.get('text','')[:500]}")
        elif kind == "assistant_text":
            lines.append(f"[ASSISTANT {ts}] {ev.get('text','')[:500]}")
        elif kind == "tool_use":
            extra = ", ".join(f"{k}={v!r}" for k, v in list(ev.get("extra", {}).items())[:2])
            lines.append(f"[TOOL {ts}] {ev.get('tool','')}({extra})")
        elif kind == "tool_result":
            lines.append(f"[RESULT] {ev.get('text','')[:200]}")
    return "\n".join(lines)


def review_session_start(pid: int) -> dict:
    """Start a background `claude -p` review (non-interactive, no new window)."""
    w = find_window(pid)
    if not w:
        return {"ok": False, "error": f"no window pid={pid}"}
    if pid in _review_results and _review_results[pid].get("status") == "running":
        return {"ok": True, "status": "already_running"}

    name = w.name or w.project_name or "session"
    transcript = w.transcript_path or ""
    if not transcript:
        return {"ok": False, "error": "no transcript to review"}

    summary = _build_review_summary(transcript, limit=40)

    prompt = (
        f"请 review 以下 Claude Code session 的工作成果。\n"
        f"Session: {name}\n"
        f"CWD: {w.cwd}\n\n"
        f"## 最近对话记录\n\n{summary}\n\n"
        f"请检查：\n"
        f"1. 任务是否完成\n"
        f"2. 有无低级错误或遗漏\n"
        f"3. 有无安全问题\n"
        f"4. 给出结论：PASS（可以关闭） / FAIL（需要继续或修复） / PARTIAL（部分完成）\n"
        f"用中文回答，200字以内。"
    )

    prompt_file = Path(f"/tmp/fleet-review-{pid}.txt")
    prompt_file.write_text(prompt, encoding="utf-8")

    _review_results[pid] = {"status": "running", "name": name}

    import threading

    def _run():
        try:
            cmd = f'cat {shlex.quote(str(prompt_file))} | claude -p --output-format text'
            proc = subprocess.run(
                ["zsh", "-c", f"source ~/.zshrc 2>/dev/null; cd {shlex.quote(w.cwd)} && {cmd}"],
                capture_output=True, text=True, timeout=120,
            )
            _review_results[pid] = {
                "status": "done",
                "name": name,
                "verdict": proc.stdout.strip()[-3000:],
                "rc": proc.returncode,
                "error": proc.stderr.strip()[-500:] if proc.returncode != 0 else "",
            }
        except Exception as e:
            _review_results[pid] = {"status": "error", "name": name, "error": str(e)}
        finally:
            prompt_file.unlink(missing_ok=True)

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "status": "started", "name": name}


def review_session_result(pid: int) -> dict:
    """Get the result of a background review."""
    return _review_results.get(pid, {"status": "not_found"})


def close_session(pid: int) -> dict:
    """Send SIGTERM to a Claude Code session for graceful shutdown."""
    import signal
    w = find_window(pid)
    if not w:
        return {"ok": False, "error": f"no window pid={pid}"}
    if not w.alive:
        return {"ok": True, "message": "already dead"}
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return {"ok": True, "message": "already dead"}
    except PermissionError:
        return {"ok": False, "error": "permission denied"}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "pid": pid, "message": f"SIGTERM sent to {pid}"}


def review_session(pid: int) -> dict:
    """Open a terminal and resume the session for manual review."""
    w = find_window(pid)
    if not w:
        return {"ok": False, "error": f"no window pid={pid}"}
    return launch_session("claude", w.session_id, w.cwd, fork=False)
