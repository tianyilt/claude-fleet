"""Tests for `claude-fleet handoff` — safe takeover of a live session by
another frontend (Orca pane). Busy sessions are refused, SIGTERM is waited on,
and the resume command is only handed out once the old process is gone."""
import os

import pytest

from core import actions, cli, sessions


class _W:
    def __init__(self, pid, session_id="abc123def456", cwd="/tmp/proj",
                 status="idle", alive=True, name="my-task"):
        self.pid = pid
        self.session_id = session_id
        self.cwd = cwd
        self.status = status
        self.alive = alive
        self.name = name
        self.project_name = "proj"


@pytest.fixture
def no_clipboard(monkeypatch):
    monkeypatch.setattr(actions, "_copy_clipboard", lambda text: False)


# ---------- actions.handoff_session ----------

def test_unknown_pid(monkeypatch, no_clipboard):
    monkeypatch.setattr(actions, "find_window", lambda pid: None)
    r = actions.handoff_session(999)
    assert r["ok"] is False and "no window" in r["error"]


def test_busy_refused_without_force(monkeypatch, no_clipboard):
    monkeypatch.setattr(actions, "find_window", lambda pid: _W(pid, status="busy"))
    killed = []
    monkeypatch.setattr(actions.os, "kill", lambda *a: killed.append(a))
    r = actions.handoff_session(123)
    assert r["ok"] is False and "busy" in r["error"]
    assert not killed                       # never signalled


def test_idle_handoff_sigterms_and_returns_resume(monkeypatch, no_clipboard):
    monkeypatch.setattr(actions, "find_window", lambda pid: _W(pid, status="idle"))
    killed = []
    monkeypatch.setattr(actions.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    monkeypatch.setattr(actions, "_pid_alive", lambda pid: False)
    r = actions.handoff_session(123)
    assert r["ok"] is True
    assert killed and killed[0][0] == 123
    assert "claude --resume abc123def456" in r["resume_command"]
    assert r["resume_command"].startswith("cd ")


def test_busy_with_force_proceeds(monkeypatch, no_clipboard):
    monkeypatch.setattr(actions, "find_window", lambda pid: _W(pid, status="busy"))
    monkeypatch.setattr(actions.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(actions, "_pid_alive", lambda pid: False)
    r = actions.handoff_session(123, force=True)
    assert r["ok"] is True


def test_survivor_process_blocks_handoff(monkeypatch, no_clipboard):
    monkeypatch.setattr(actions, "find_window", lambda pid: _W(pid, status="idle"))
    monkeypatch.setattr(actions.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(actions, "_pid_alive", lambda pid: True)   # refuses to die
    r = actions.handoff_session(123, wait_seconds=0.3)
    assert r["ok"] is False and "still running" in r["error"]
    assert "resume_command" not in r        # no fork-risk command handed out


def test_dead_window_hands_out_resume_without_kill(monkeypatch, no_clipboard):
    monkeypatch.setattr(actions, "find_window", lambda pid: _W(pid, alive=False))
    def boom(*a):
        raise AssertionError("kill must not be called for a dead window")
    monkeypatch.setattr(actions.os, "kill", boom)
    r = actions.handoff_session(123)
    assert r["ok"] is True and "claude --resume" in r["resume_command"]


# ---------- CLI resolution ----------

def _run_cli(argv):
    return cli.main(argv)


def test_cli_lists_live_sessions_when_no_arg(monkeypatch, capsys):
    monkeypatch.setattr(sessions, "list_windows",
                        lambda include_dead=False: [_W(1), _W(2, session_id="zzz999")])
    rc = _run_cli(["handoff"])
    out = capsys.readouterr().out
    assert rc == 0 and "abc123def456"[:12] in out and "zzz999"[:6] in out


def test_cli_no_live_match(monkeypatch, capsys):
    monkeypatch.setattr(sessions, "list_windows", lambda include_dead=False: [_W(1)])
    rc = _run_cli(["handoff", "nomatch"])
    err = capsys.readouterr().err
    assert rc == 2 and "no LIVE local session" in err


def test_cli_ambiguous_prefix(monkeypatch, capsys):
    monkeypatch.setattr(sessions, "list_windows",
                        lambda include_dead=False: [_W(1, session_id="abc111"),
                                                    _W(2, session_id="abc222")])
    rc = _run_cli(["handoff", "abc"])
    err = capsys.readouterr().err
    assert rc == 2 and "ambiguous" in err


def test_cli_pid_match_calls_handoff(monkeypatch, capsys):
    monkeypatch.setattr(sessions, "list_windows", lambda include_dead=False: [_W(4242)])
    seen = {}
    def fake_handoff(pid, force=False):
        seen["pid"], seen["force"] = pid, force
        return {"ok": True, "pid": pid, "session_id": "abc123def456",
                "name": "my-task", "resume_command": "cd /tmp/proj && claude --resume abc123def456",
                "copied": False}
    monkeypatch.setattr(actions, "handoff_session", fake_handoff)
    rc = _run_cli(["handoff", "4242"])
    out = capsys.readouterr().out
    assert rc == 0 and seen["pid"] == 4242 and seen["force"] is False
    assert "claude --resume" in out and "Orca" in out


def test_cli_force_flag(monkeypatch, capsys):
    monkeypatch.setattr(sessions, "list_windows", lambda include_dead=False: [_W(4242)])
    seen = {}
    def fake_handoff(pid, force=False):
        seen["force"] = force
        return {"ok": True, "pid": pid, "session_id": "s", "name": "",
                "resume_command": "cmd", "copied": False}
    monkeypatch.setattr(actions, "handoff_session", fake_handoff)
    rc = _run_cli(["handoff", "4242", "--force"])
    assert rc == 0 and seen["force"] is True


@pytest.mark.skipif(os.name == "nt", reason="SIGTERM/os.kill semantics are Unix-only")
def test_real_process_handoff(monkeypatch, no_clipboard):
    """End-to-end against a real throwaway process: SIGTERM lands, waiter sees it die."""
    import subprocess
    import threading
    proc = subprocess.Popen(["sleep", "60"])
    # A real claude process is reaped by init; here WE are the parent, so reap
    # concurrently or the zombie keeps os.kill(pid, 0) succeeding forever.
    threading.Thread(target=proc.wait, daemon=True).start()
    try:
        monkeypatch.setattr(actions, "find_window",
                            lambda pid: _W(pid, status="idle"))
        r = actions.handoff_session(proc.pid, wait_seconds=5.0)
        assert r["ok"] is True
        assert proc.wait(timeout=5) is not None
    finally:
        if proc.poll() is None:
            proc.kill()
