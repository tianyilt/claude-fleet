"""Tests for the live-window enrichment caches that stop the 2s poll from
re-scanning unchanged transcripts (Tier-1 efficiency fixes)."""
import json

from core import patrol, transcripts


def _write(path, records):
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


def _claude_transcript(path):
    _write(path, [
        {"type": "user", "timestamp": "2026-06-20T10:00:00Z",
         "message": {"content": [{"type": "text", "text": "hi"}]}},
        {"type": "assistant", "timestamp": "2026-06-20T10:01:00Z",
         "message": {"model": "claude-opus-4-8", "stop_reason": "end_turn",
                     "content": [{"type": "text", "text": "done"}]}},
    ])


# ---------- Fix A: window_enrichment caches by (mtime, size) ----------

def test_window_enrichment_caches_unchanged_file(tmp_path, monkeypatch):
    f = tmp_path / "c.jsonl"
    _claude_transcript(f)
    transcripts._window_enrich_cache.clear()

    calls = {"n": 0}
    orig = transcripts._iter_lines
    def counting(p):
        calls["n"] += 1
        yield from orig(p)
    monkeypatch.setattr(transcripts, "_iter_lines", counting)

    first = transcripts.window_enrichment(f)
    after_first = calls["n"]
    assert after_first > 0                       # cold: reads the file

    calls["n"] = 0
    second = transcripts.window_enrichment(f)
    assert calls["n"] == 0                        # warm: zero re-reads
    assert second == first                        # same bundle


def test_window_enrichment_rescans_after_change(tmp_path):
    f = tmp_path / "c.jsonl"
    _claude_transcript(f)
    transcripts._window_enrich_cache.clear()
    b1 = transcripts.window_enrichment(f)
    # grow the file → new (mtime,size) → must recompute
    with f.open("a") as fh:
        fh.write(json.dumps({"type": "user", "message": {"content": [
            {"type": "text", "text": "more"}]}}) + "\n")
    b2 = transcripts.window_enrichment(f)
    assert isinstance(b1, dict) and isinstance(b2, dict)
    assert set(b2) == {"current_task", "skills_used", "memory_ops", "background_tasks"}


def test_window_enrichment_matches_individual_calls(tmp_path):
    f = tmp_path / "c.jsonl"
    _claude_transcript(f)
    transcripts._window_enrich_cache.clear()
    b = transcripts.window_enrichment(f)
    assert b["current_task"] == transcripts.current_task_hint(f)
    assert b["skills_used"] == transcripts.extract_skills_used(f)
    assert b["memory_ops"] == transcripts.extract_memory_ops(f)
    assert b["background_tasks"] == transcripts.extract_background_tasks(f)


def test_prune_window_enrich_cache(tmp_path):
    f = tmp_path / "c.jsonl"
    _claude_transcript(f)
    transcripts._window_enrich_cache.clear()
    transcripts.window_enrichment(f)
    assert str(f) in transcripts._window_enrich_cache
    transcripts.prune_window_enrich_cache([])    # nothing live → evict
    assert str(f) not in transcripts._window_enrich_cache


# ---------- Fix B: patrol._last_assistant_info caches + reads only the tail ----------

def test_last_assistant_info_caches(tmp_path, monkeypatch):
    f = tmp_path / "c.jsonl"
    _claude_transcript(f)
    patrol._last_info_cache.clear()

    opens = {"n": 0}
    real_open = type(f).open
    def counting_open(self, *a, **k):
        if str(self) == str(f):
            opens["n"] += 1
        return real_open(self, *a, **k)
    monkeypatch.setattr(type(f), "open", counting_open)

    patrol._last_assistant_info(str(f))
    assert opens["n"] == 1
    patrol._last_assistant_info(str(f))          # cached → no second open
    assert opens["n"] == 1


def test_last_assistant_info_reads_only_tail(tmp_path):
    # A long transcript: the function must still work reading only the last lines.
    f = tmp_path / "big.jsonl"
    recs = [{"type": "user", "message": {"content": [{"type": "text", "text": f"q{i}"}]}}
            for i in range(500)]
    recs.append({"type": "assistant", "timestamp": "2026-06-20T10:00:00Z",
                 "message": {"stop_reason": "end_turn",
                             "content": [{"type": "text", "text": "final"}]}})
    _write(f, recs)
    patrol._last_info_cache.clear()
    info = patrol._last_assistant_info(str(f))
    assert info and info["stop_reason"] == "end_turn"
    assert info["last_text"] == "final"
