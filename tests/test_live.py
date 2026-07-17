"""Tests for `claude-fleet live` — the in-pane live board with one-key handoff."""
import pytest

from core import actions, cli, live, sessions


class _W:
    def __init__(self, pid, session_id="abc123def456", status="idle",
                 name="my-task", updated_at=None):
        import time
        self.pid = pid
        self.session_id = session_id
        self.cwd = "/tmp/proj"
        self.status = status
        self.name = name
        self.project_name = "proj"
        self.updated_at = updated_at if updated_at is not None else int(time.time() * 1000)


# ---------- render ----------

def test_render_lists_sessions_with_keys():
    out = live.render([_W(1), _W(2, session_id="zzz999", status="busy")], color=False)
    assert "2 running session(s)" in out
    assert "  1   abc123def456" in out
    assert "  2   zzz999" in out
    assert "busy" in out and "idle" in out
    assert "my-task" in out


def test_render_empty():
    out = live.render([], color=False)
    assert "0 running session(s)" in out and "no live local sessions" in out


def test_render_noninteractive_hides_keys():
    out = live.render([_W(1)], interactive=False, color=False)
    assert "press a session's KEY" not in out
    assert "      abc123def456" in out      # blank key column


def test_render_shows_message():
    out = live.render([], message="hello-there", color=False)
    assert "hello-there" in out


def test_fmt_idle():
    assert live._fmt_idle(45) == "45s"
    assert live._fmt_idle(120) == "2m"
    assert live._fmt_idle(3900) == "1h05m"


# ---------- handoff dispatch ----------

def test_handoff_by_index_dispatches(monkeypatch):
    seen = {}
    def fake(pid, force=False):
        seen["pid"], seen["force"] = pid, force
        return {"ok": True, "session_id": "abc123def456", "name": "my-task",
                "resume_command": "cd /tmp/proj && claude --resume abc123def456",
                "copied": True}
    monkeypatch.setattr(actions, "handoff_session", fake)
    msg = live._handoff_by_index([_W(4242)], 0, force=False, color=False)
    assert seen["pid"] == 4242 and seen["force"] is False
    assert "claude --resume" in msg and "clipboard" in msg


def test_handoff_by_index_error_surfaces(monkeypatch):
    monkeypatch.setattr(actions, "handoff_session",
                        lambda pid, force=False: {"ok": False, "error": "session is busy"})
    msg = live._handoff_by_index([_W(1)], 0, force=False, color=False)
    assert "busy" in msg


def test_handoff_by_index_out_of_range():
    assert "no session" in live._handoff_by_index([_W(1)], 5, force=False)


# ---------- run / cli ----------

def test_run_once_prints_snapshot(monkeypatch, capsys):
    monkeypatch.setattr(sessions, "list_windows",
                        lambda include_dead=False: [_W(1), _W(2, session_id="zzz999")])
    rc = cli.main(["live", "--once"])
    out = capsys.readouterr().out
    assert rc == 0 and "abc123def456" in out and "zzz999" in out


def test_run_once_empty(monkeypatch, capsys):
    monkeypatch.setattr(sessions, "list_windows", lambda include_dead=False: [])
    rc = cli.main(["live", "--once"])
    assert rc == 0 and "no live local sessions" in capsys.readouterr().out
