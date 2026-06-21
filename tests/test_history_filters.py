"""Tests for History faceted filters + metric ranking (multi-select platform/skill,
sort by tokens/cost/duration/turns)."""
from core import history


def _mk(sid, platform, tokens=0, cost=0.0, dur=0, turns=0, skills=None):
    return history.HistorySession(
        session_id=sid, project="/p", project_name="p", first_input="",
        input_count=0, first_ts="", last_ts="", transcript_path=None,
        transcript_size=0, transcript_mtime=0, is_alive=False, platform=platform,
        skills_used=skills or [],
        metrics={"tokens": {"total": tokens}, "cost_usd": cost,
                 "duration_sec": dur, "turns": turns},
    )


def _seed(monkeypatch, sessions):
    monkeypatch.setattr(history, "_cache", sessions)
    monkeypatch.setattr(history, "_cache_ts", 9e18)   # never rebuild


def test_sort_by_tokens(monkeypatch):
    _seed(monkeypatch, [
        _mk("a", "claude", tokens=100),
        _mk("b", "codex", tokens=900),
        _mk("c", "claude", tokens=500),
    ])
    out = history.list_sessions(limit=10, sort="tokens")
    assert [s["session_id"] for s in out["sessions"]] == ["b", "c", "a"]


def test_sort_by_cost(monkeypatch):
    _seed(monkeypatch, [_mk("a", "claude", cost=1.0), _mk("b", "claude", cost=9.0)])
    out = history.list_sessions(limit=10, sort="cost")
    assert out["sessions"][0]["session_id"] == "b"


def test_filter_platforms_multi(monkeypatch):
    _seed(monkeypatch, [
        _mk("a", "claude"), _mk("b", "codex"), _mk("c", "opencode"),
    ])
    out = history.list_sessions(limit=10, platforms=["codex", "opencode"])
    assert {s["session_id"] for s in out["sessions"]} == {"b", "c"}
    assert out["total"] == 2


def test_filter_skills_any_match(monkeypatch):
    _seed(monkeypatch, [
        _mk("a", "claude", skills=["feishu-notify", "qzcli"]),
        _mk("b", "claude", skills=["paper-write"]),
        _mk("c", "codex", skills=["feishu-notify"]),
    ])
    out = history.list_sessions(limit=10, skills=["feishu-notify"])
    assert {s["session_id"] for s in out["sessions"]} == {"a", "c"}


def test_filters_combine_platform_and_skill(monkeypatch):
    _seed(monkeypatch, [
        _mk("a", "claude", skills=["feishu-notify"]),
        _mk("c", "codex", skills=["feishu-notify"]),
    ])
    out = history.list_sessions(limit=10, platforms=["codex"], skills=["feishu-notify"])
    assert [s["session_id"] for s in out["sessions"]] == ["c"]


def test_no_sort_keeps_recency_order(monkeypatch):
    seeded = [_mk("a", "claude", tokens=1), _mk("b", "claude", tokens=999)]
    _seed(monkeypatch, seeded)
    out = history.list_sessions(limit=10, sort="recency")
    assert [s["session_id"] for s in out["sessions"]] == ["a", "b"]   # cache order preserved
