"""Tests for Focus / Resume / Fork robustness (headless windows, bad pids,
deleted project dirs). Regression cover for the 'focus failed' bug."""
import app


class _W:
    def __init__(self, pid, tty, session_id="s", alive=True):
        self.pid = pid
        self.tty = tty
        self.session_id = session_id
        self.alive = alive


# ---------- api_focus ----------

def test_focus_headless_returns_json_not_404(monkeypatch):
    # window exists but has no tty (headless IDE/SDK process)
    monkeypatch.setattr(app.sessions, "find_window", lambda pid: _W(pid, None))
    r = app.api_focus(123)
    assert r["ok"] is False
    assert "headless" in r["error"]          # actionable, not generic


def test_focus_unknown_pid_returns_json_not_404(monkeypatch):
    monkeypatch.setattr(app.sessions, "find_window", lambda pid: None)
    monkeypatch.setattr(app.codex, "list_codex_windows", lambda: [])
    app.state.last_snapshot = {"windows": []}
    r = app.api_focus(999999)
    assert r["ok"] is False and "not found" in r["error"]   # no HTTPException


def test_focus_codex_live_window_uses_its_tty(monkeypatch):
    monkeypatch.setattr(app.sessions, "find_window", lambda pid: None)
    monkeypatch.setattr(app.codex, "list_codex_windows",
                        lambda: [{"pid": 4242, "tty": "/dev/ttys9", "platform": "codex"}])
    called = {}
    def fake_focus(tty):
        called["tty"] = tty
        return {"ok": True}
    monkeypatch.setattr(app.terminal, "focus", fake_focus)
    r = app.api_focus(4242)
    assert r["ok"] is True and called["tty"] == "/dev/ttys9"


def test_focus_tty_window_calls_terminal_focus(monkeypatch):
    monkeypatch.setattr(app.sessions, "find_window", lambda pid: _W(pid, "/dev/ttys1"))
    monkeypatch.setattr(app.terminal, "focus", lambda tty: {"ok": True, "tty": tty})
    r = app.api_focus(123)
    assert r["ok"] is True


# ---------- resume / fork project-dir guard ----------

def test_resume_missing_project_dir(monkeypatch):
    monkeypatch.setattr(app, "_find_history_session",
                        lambda sid, source="": {"platform": "claude", "project": "/no/such/dir/xyz"})
    r = app.api_history_resume("sid")
    assert r["ok"] is False and "not found" in r["error"]


def test_fork_missing_project_dir(monkeypatch):
    monkeypatch.setattr(app, "_find_history_session",
                        lambda sid, source="": {"platform": "claude", "project": "/no/such/dir/xyz"})
    r = app.api_history_fork("sid")
    assert r["ok"] is False and "not found" in r["error"]


def test_resume_valid_dir_proceeds(monkeypatch, tmp_path):
    monkeypatch.setattr(app, "_find_history_session",
                        lambda sid, source="": {"platform": "codex", "project": str(tmp_path)})
    monkeypatch.setattr(app.terminal, "launch_session",
                        lambda *a, **k: {"ok": True, "action": "resumed"})
    r = app.api_history_resume("sid")
    assert r["ok"] is True


def test_resume_remote_uses_ssh(monkeypatch):
    monkeypatch.setattr(app, "_find_history_session",
                        lambda sid, source="": {"platform": "codex", "project": "/remote/dir",
                                                "source": "2224"})
    monkeypatch.setattr(app.remote, "ssh_for", lambda s: "ssh -p 2224 root@localhost")
    seen = {}
    monkeypatch.setattr(app.terminal, "launch_session",
                        lambda *a, **k: seen.update(k) or {"ok": True, "action": "resumed"})
    r = app.api_history_resume("sid")
    assert r["ok"] is True and seen.get("ssh") == "ssh -p 2224 root@localhost"
