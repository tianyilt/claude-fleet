"""Tests for timestamp normalization and the per-file enrichment caches."""
import json

from core import history, codex


def test_norm_ts_epoch_millis():
    out = history._norm_ts(1781358414597)
    assert isinstance(out, str)
    assert out.startswith("2026-")


def test_norm_ts_epoch_seconds():
    out = history._norm_ts(1781358414)
    assert isinstance(out, str)
    assert out.startswith("2026-")


def test_norm_ts_iso_passthrough():
    assert history._norm_ts("2026-06-13T13:46:54Z") == "2026-06-13T13:46:54Z"


def test_norm_ts_empty_and_none():
    assert history._norm_ts("") == ""
    assert history._norm_ts(None) == ""


def test_norm_ts_garbage_does_not_raise():
    # absurd value must not throw, just yield ""
    assert history._norm_ts(10**30) == ""


def test_load_history_jsonl_numeric_timestamp_becomes_string(tmp_path, monkeypatch):
    """Regression: history.jsonl now stores int epoch ms; first_ts must be str."""
    hist = tmp_path / "history.jsonl"
    hist.write_text(json.dumps({
        "sessionId": "abc", "display": "hello", "timestamp": 1781358414597,
        "project": "/tmp/proj",
    }) + "\n")
    monkeypatch.setattr(history, "HISTORY_JSONL", hist)
    out = history._load_history_jsonl()
    assert isinstance(out["abc"]["first_ts"], str)
    assert out["abc"]["first_ts"].startswith("2026-")


def test_enrich_transcript_caches_by_mtime(monkeypatch):
    calls = {"n": 0}

    def fake_model(path):
        calls["n"] += 1
        return "stub-model"

    # stub every parse helper so no real file is read; count one of them
    monkeypatch.setattr(history, "_extract_model", fake_model)
    monkeypatch.setattr(history, "_extract_first_user_text", lambda p: "")
    monkeypatch.setattr(history, "_extract_skills_from_transcript", lambda p: [])
    monkeypatch.setattr("core.transcripts.extract_memory_ops", lambda tp: [])
    monkeypatch.setattr("core.transcripts.count_skill_activity", lambda tp: {})
    monkeypatch.setattr("core.transcripts.count_memory_activity", lambda tp: {})

    history._enrich_cache.clear()
    history._enrich_transcript("/fake.jsonl", 100, 5)
    history._enrich_transcript("/fake.jsonl", 100, 5)  # same mtime/size -> cache hit
    assert calls["n"] == 1
    history._enrich_transcript("/fake.jsonl", 200, 5)  # changed mtime -> re-parse
    assert calls["n"] == 2


def test_codex_cache_by_mtime(tmp_path, monkeypatch):
    sess = tmp_path / "sess1.jsonl"
    sess.write_text(json.dumps({
        "type": "session_meta",
        "payload": {"id": "sess1", "cwd": "/tmp/x", "timestamp": "2026-01-01T00:00:00Z"},
    }) + "\n")
    monkeypatch.setattr(codex, "CODEX_SESSIONS_DIR", tmp_path)

    calls = {"n": 0}

    def fake_activity(path):
        calls["n"] += 1
        return {"skills_used": [], "memory_ops": [], "model": "",
                "skill_breakdown": {}}

    monkeypatch.setattr(codex, "extract_codex_session_activity", fake_activity)
    monkeypatch.setattr(codex, "_extract_first_user_input", lambda p: "")
    codex._codex_cache.clear()

    a = codex.list_codex_sessions()
    b = codex.list_codex_sessions()  # unchanged file -> cache hit
    assert len(a) == 1 and len(b) == 1
    assert calls["n"] == 1
