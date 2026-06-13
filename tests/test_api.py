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


# ---------- resume ----------

def test_resume_alive_focuses(monkeypatch):
    monkeypatch.setattr(app_module.sessions, "list_windows", lambda: [_win("sid1")])
    monkeypatch.setattr(app_module.terminal, "focus", lambda tty: {"ok": True})
    r = client.post("/api/history/sid1/resume").json()
    assert r["ok"] is True and r["action"] == "focused"


def test_resume_dead_spawns(monkeypatch):
    monkeypatch.setattr(app_module.sessions, "list_windows", lambda: [])
    monkeypatch.setattr(app_module.history, "list_sessions",
                        lambda **k: {"sessions": [{"session_id": "sid2", "project": "/p"}]})
    monkeypatch.setattr(app_module.terminal, "spawn_window", lambda inner: {"ok": True, "error": None})
    r = client.post("/api/history/sid2/resume").json()
    assert r["ok"] is True and r["action"] == "resumed" and r["cwd"] == "/p"


def test_resume_not_in_index(monkeypatch):
    monkeypatch.setattr(app_module.sessions, "list_windows", lambda: [])
    monkeypatch.setattr(app_module.history, "list_sessions", lambda **k: {"sessions": []})
    r = client.post("/api/history/nope/resume").json()
    assert r["ok"] is False and "not found" in r["error"]


def test_resume_non_mac_returns_fallback(monkeypatch):
    monkeypatch.setattr(app_module.sessions, "list_windows", lambda: [])
    monkeypatch.setattr(app_module.history, "list_sessions",
                        lambda **k: {"sessions": [{"session_id": "sid3", "project": "/p"}]})
    monkeypatch.setattr(app_module.terminal, "spawn_window",
                        lambda inner: {"ok": False, "unsupported": True,
                                       "fallback_cmd": inner, "error": "only on macOS"})
    r = client.post("/api/history/sid3/resume").json()
    assert r["ok"] is False
    assert r["fallback_cmd"].startswith("cd /p &&")


# ---------- fork ----------

def test_fork_found(monkeypatch):
    monkeypatch.setattr(app_module.history, "list_sessions",
                        lambda **k: {"sessions": [{"session_id": "sid4", "project": "/q"}]})
    monkeypatch.setattr(app_module.terminal, "spawn_window", lambda inner: {"ok": True, "error": None})
    r = client.post("/api/history/sid4/fork").json()
    assert r["ok"] is True and r["action"] == "forked"


def test_fork_not_in_index(monkeypatch):
    monkeypatch.setattr(app_module.history, "list_sessions", lambda **k: {"sessions": []})
    r = client.post("/api/history/nope/fork").json()
    assert r["ok"] is False and "not found" in r["error"]


def test_fork_non_mac_fallback(monkeypatch):
    monkeypatch.setattr(app_module.history, "list_sessions",
                        lambda **k: {"sessions": [{"session_id": "sid5", "project": "/q"}]})
    monkeypatch.setattr(app_module.terminal, "spawn_window",
                        lambda inner: {"ok": False, "fallback_cmd": inner, "error": "x"})
    r = client.post("/api/history/sid5/fork").json()
    assert r["fallback_cmd"].endswith("--fork-session")


# ---------- removed feishu export ----------

def test_export_endpoint_removed():
    assert client.post("/api/windows/123/export").status_code == 404
