"""Side-effectful actions: fork, close, handoff, review.

Terminal window control (open/focus) lives in core/terminal.py and is dispatched
per-platform there; this module bundles the session lookup around it.
"""
from __future__ import annotations

import os
import shutil
import signal
import shlex
import subprocess
import threading
import time

from . import terminal
from .sessions import find_window, _pid_alive
from .transcripts import timeline


def focus_terminal(tty: str) -> dict:
    """Activate the terminal tab that owns `tty` (macOS only; degrades elsewhere)."""
    return terminal.focus(tty)


def fork_session(pid: int) -> dict:
    """Open a terminal and fork the live session (new ID, inherits history)."""
    w = find_window(pid)
    if not w:
        return {"ok": False, "error": f"no window pid={pid}"}
    return terminal.launch_session("claude", w.session_id, w.cwd, fork=True)


def fork_session_at_node(session_id: str, target_uuid: str, cwd: str) -> dict:
    """Fork a Claude session truncated at a timeline node (issue #3).

    Writes a new transcript ending at `target_uuid`, then resumes it — so the new
    session continues from that point with the prior history but none of the later
    turns.
    """
    from .transcripts import fork_transcript_at
    try:
        new_sid, _ = fork_transcript_at(session_id, target_uuid)
    except (FileNotFoundError, ValueError) as e:
        return {"ok": False, "error": str(e)}
    # The new transcript already exists on disk, so resume it directly (no --fork-session).
    result = terminal.launch_session("claude", new_sid, cwd, fork=False)
    result["new_session_id"] = new_sid
    result["forked_from"] = session_id
    return result


def close_session(pid: int) -> dict:
    """Send SIGTERM to a Claude Code session for graceful shutdown."""
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


def _copy_clipboard(text: str) -> bool:
    if not terminal.IS_MAC or not shutil.which("pbcopy"):
        return False
    try:
        subprocess.run(["pbcopy"], input=text, text=True, timeout=3, check=True)
        return True
    except Exception:
        return False


def handoff_session(pid: int, force: bool = False, wait_seconds: float = 8.0,
                    copy: bool = True) -> dict:
    """Gracefully stop a live Claude session and hand out its resume command, so
    another frontend (an Orca pane, a different terminal) can pick it up.

    Claude flushes the transcript continuously, so exiting an idle session loses
    nothing — but a mid-generation kill drops the in-flight turn, hence the busy
    gate. Refuses to hand off while the old process is still alive: two processes
    resuming one session id fork the transcript.
    """
    w = find_window(pid)
    if not w:
        return {"ok": False, "error": f"no window pid={pid}"}
    resume_command = terminal.session_cli_command("claude", w.session_id, w.cwd)

    if w.alive:
        if w.status == "busy" and not force:
            return {"ok": False,
                    "error": f"session {w.session_id[:8]} is busy (mid-generation); "
                             f"retry when idle, or pass --force to kill anyway"}
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except PermissionError:
            return {"ok": False, "error": "permission denied"}
        deadline = time.monotonic() + wait_seconds
        while _pid_alive(pid):
            if time.monotonic() >= deadline:
                return {"ok": False, "pid": pid,
                        "error": f"pid {pid} still running {wait_seconds:.0f}s after SIGTERM — "
                                 f"not handing off (resuming now would fork the transcript)"}
            time.sleep(0.2)

    copied = _copy_clipboard(resume_command) if copy else False
    return {"ok": True, "pid": pid, "session_id": w.session_id,
            "name": w.name or w.project_name,
            "resume_command": resume_command, "copied": copied}


# ---------- background review (non-interactive `claude -p`) ----------

_review_results: dict[int, dict] = {}


def _build_review_summary(transcript_path: str, limit: int = 40) -> str:
    """Extract last N turns as compact text for review prompt."""
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

    _review_results[pid] = {"status": "running", "name": name}

    def _run():
        try:
            if terminal.IS_MAC:
                # source ~/.zshrc so claude + PATH/aliases resolve like the user's shell
                shell_cmd = (
                    f"source ~/.zshrc 2>/dev/null; cd {shlex.quote(w.cwd)} && "
                    f"claude -p --output-format text"
                )
                proc = subprocess.run(
                    ["zsh", "-c", shell_cmd], input=prompt,
                    capture_output=True, text=True, timeout=120,
                )
            else:
                proc = subprocess.run(
                    ["claude", "-p", "--output-format", "text"], input=prompt,
                    cwd=w.cwd, capture_output=True, text=True, timeout=120,
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

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "status": "started", "name": name}


def review_session_result(pid: int) -> dict:
    """Get the result of a background review."""
    return _review_results.get(pid, {"status": "not_found"})


def review_session(pid: int) -> dict:
    """Open a terminal and resume the session for manual review."""
    w = find_window(pid)
    if not w:
        return {"ok": False, "error": f"no window pid={pid}"}
    return terminal.launch_session("claude", w.session_id, w.cwd, fork=False)
