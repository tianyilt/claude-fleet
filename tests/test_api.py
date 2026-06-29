"""API tests with all terminal/iTerm interactions mocked (no real windows)."""
import types

import pytest
from fastapi.testclient import TestClient

import app as app_module

client = TestClient(app_module.app)


def _win(sid, **kw):
    base = dict(session_id=sid, alive=True, tty="/dev/ttys000", pid=42)
    base.update(kw)
    return types.SimpleNamespace(**base)


def _sessions(*items):
    return lambda **k: {"sessions": list(items)}


# ---------- resume ----------

def test_resume_alive_focuses(monkeypatch, tmp_path):
    monkeypatch.setattr(app_module.history, "list_sessions",
                        _sessions({"session_id": "sid1", "project": str(tmp_path), "platform": "claude"}))
    monkeypatch.setattr(app_module.sessions, "list_windows", lambda: [_win("sid1")])
    monkeypatch.setattr(app_module.terminal, "focus", lambda tty: {"ok": True})
    r = client.post("/api/history/sid1/resume").json()
    assert r["ok"] is True and r["action"] == "focused"


def test_resume_dead_launches(monkeypatch, tmp_path):
    monkeypatch.setattr(app_module.sessions, "list_windows", lambda: [])
    monkeypatch.setattr(app_module.history, "list_sessions",
                        _sessions({"session_id": "sid2", "project": str(tmp_path), "platform": "claude"}))
    monkeypatch.setattr(app_module.terminal, "launch_session",
                        lambda platform, sid, cwd, fork=False: {"ok": True, "action": "resumed",
                                                                "cwd": cwd, "platform": platform})
    r = client.post("/api/history/sid2/resume").json()
    assert r["ok"] is True and r["action"] == "resumed" and r["cwd"] == str(tmp_path)


def test_resume_codex_session(monkeypatch, tmp_path):
    monkeypatch.setattr(app_module.sessions, "list_windows", lambda: [])
    monkeypatch.setattr(app_module.history, "list_sessions",
                        _sessions({"session_id": "cx1", "project": str(tmp_path), "platform": "codex"}))
    seen = {}
    monkeypatch.setattr(app_module.terminal, "launch_session",
                        lambda platform, sid, cwd, fork=False: seen.update(platform=platform) or {"ok": True})
    client.post("/api/history/cx1/resume")
    assert seen["platform"] == "codex"


def test_resume_not_in_index(monkeypatch):
    monkeypatch.setattr(app_module.history, "list_sessions", _sessions())
    r = client.post("/api/history/nope/resume").json()
    assert r["ok"] is False and "not found" in r["error"]


def test_resume_no_launcher_returns_command(monkeypatch, tmp_path):
    monkeypatch.setattr(app_module.sessions, "list_windows", lambda: [])
    monkeypatch.setattr(app_module.history, "list_sessions",
                        _sessions({"session_id": "sid3", "project": str(tmp_path), "platform": "claude"}))
    monkeypatch.setattr(app_module.terminal, "launch_session",
                        lambda platform, sid, cwd, fork=False: {
                            "ok": False, "command": f"cd {cwd} && claude --resume {sid}", "error": "no launcher"})
    r = client.post("/api/history/sid3/resume").json()
    assert r["ok"] is False and r["command"].startswith(f"cd {tmp_path} &&")


# ---------- fork ----------

def test_fork_found(monkeypatch, tmp_path):
    monkeypatch.setattr(app_module.history, "list_sessions",
                        _sessions({"session_id": "sid4", "project": str(tmp_path), "platform": "claude"}))
    monkeypatch.setattr(app_module.terminal, "launch_session",
                        lambda platform, sid, cwd, fork=False: {"ok": True, "action": "forked"})
    r = client.post("/api/history/sid4/fork").json()
    assert r["ok"] is True and r["action"] == "forked"


def test_fork_not_in_index(monkeypatch):
    monkeypatch.setattr(app_module.history, "list_sessions", _sessions())
    r = client.post("/api/history/nope/fork").json()
    assert r["ok"] is False and "not found" in r["error"]


def test_fork_no_launcher_returns_command(monkeypatch, tmp_path):
    monkeypatch.setattr(app_module.history, "list_sessions",
                        _sessions({"session_id": "sid5", "project": str(tmp_path), "platform": "claude"}))
    monkeypatch.setattr(app_module.terminal, "launch_session",
                        lambda platform, sid, cwd, fork=False: {
                            "ok": False, "command": f"cd {cwd} && claude --resume {sid} --fork-session"})
    r = client.post("/api/history/sid5/fork").json()
    assert r["command"].endswith("--fork-session")


# ---------- fork-at-node (#3) ----------

def test_fork_at_node_requires_uuid():
    assert client.post("/api/history/sid/fork-at-node").json()["ok"] is False


def test_fork_at_node_calls_action(monkeypatch):
    monkeypatch.setattr(app_module.history, "list_sessions",
                        _sessions({"session_id": "s6", "project": "/p", "platform": "claude"}))
    seen = {}
    monkeypatch.setattr(app_module.actions, "fork_session_at_node",
                        lambda sid, uuid, cwd: seen.update(sid=sid, uuid=uuid, cwd=cwd) or {"ok": True})
    r = client.post("/api/history/s6/fork-at-node?uuid=u9").json()
    assert r["ok"] is True and seen == {"sid": "s6", "uuid": "u9", "cwd": "/p"}


def test_fork_at_node_rejects_non_claude(monkeypatch):
    monkeypatch.setattr(app_module.history, "list_sessions",
                        _sessions({"session_id": "cx", "project": "/p", "platform": "codex"}))
    r = client.post("/api/history/cx/fork-at-node?uuid=u1").json()
    assert r["ok"] is False and "Claude" in r["error"]


# ---------- removed feishu export ----------

def test_export_endpoint_removed():
    assert client.post("/api/windows/123/export").status_code == 404


def test_diff_signature_tolerates_none_idle():
    # A neutral codex card has idle_seconds=None; diff_signature must not crash
    # (a None // 30 here froze the whole watcher and broke all live updates).
    snap = {"windows": [
        {"pid": 1, "status": "running", "waiting_for": None, "updated_at": 0,
         "triage": "idle", "permission_msg": None, "idle_seconds": None},
        {"pid": 2, "status": "busy", "waiting_for": None, "updated_at": 5,
         "triage": "working", "permission_msg": None, "idle_seconds": 12},
    ]}
    sig = app_module.state.diff_signature(snap)   # must not raise
    assert len(sig) == 2
