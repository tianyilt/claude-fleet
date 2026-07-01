"""Timeline events carry a full `search_text` so in-session search matches what
History full-text search matches (plan docs, long tool output, thinking) — while
the displayed `text` stays a compact preview. Regression for: search a session in
History but can't find the word inside its timeline."""
import json

from core import codex, transcripts


def _write(path, records):
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


NEEDLE = "89/89"


# ---------- Claude ----------

def test_tool_result_full_body_searchable_preview_stays_short(tmp_path):
    # needle sits ~300 chars into an 8 KB tool_result → past the 200-char preview
    body = "head " + "x" * 300 + f" ActionBench {NEEDLE} tail " + "y" * 8000
    f = tmp_path / "t.jsonl"
    _write(f, [
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "content": body}]}},
    ])
    ev = [e for e in transcripts.timeline(f, limit=100) if e["kind"] == "tool_result"][0]
    assert NEEDLE in ev["search_text"]           # searchable
    assert NEEDLE not in ev["text"]              # not in the compact preview
    assert len(ev["text"]) <= 200                # display stays lean


def test_tool_use_result_file_content_searchable(tmp_path):
    # the out-of-band echo Claude puts under toolUseResult.file.content
    f = tmp_path / "t.jsonl"
    _write(f, [
        {"type": "user",
         "message": {"content": [{"type": "tool_result", "content": "(trimmed)"}]},
         "toolUseResult": {"file": {"content": "line1\nplan row " + NEEDLE + " here\n"}}},
    ])
    ev = [e for e in transcripts.timeline(f, limit=100) if e["kind"] == "tool_result"][0]
    assert NEEDLE in ev["search_text"]


def test_write_content_searchable_but_preview_capped(tmp_path):
    content = "L1\n" * 30 + f"L50\t- delivery {NEEDLE}\n" + "tail\n" * 100
    f = tmp_path / "t.jsonl"
    _write(f, [
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Write",
             "input": {"file_path": "/tmp/plan.md", "content": content}}]}},
    ])
    ev = [e for e in transcripts.timeline(f, limit=100) if e["kind"] == "tool_use"][0]
    assert NEEDLE in ev["search_text"]           # full input searchable
    assert len(ev["extra"]) <= 6                 # compact chip preview unchanged
    for v in ev["extra"].values():
        assert not (isinstance(v, str) and len(v) > 200)


def test_thinking_event_emitted_and_searchable(tmp_path):
    f = tmp_path / "t.jsonl"
    _write(f, [
        {"type": "assistant", "message": {"content": [
            {"type": "thinking", "thinking": "let me consider " + NEEDLE + " carefully"}]}},
    ])
    evs = transcripts.timeline(f, limit=100)
    think = [e for e in evs if e["kind"] == "thinking"]
    assert think and NEEDLE in think[0]["search_text"]


def test_search_text_capped(tmp_path):
    huge = "z" * (transcripts.SEARCH_CAP + 50_000)
    f = tmp_path / "t.jsonl"
    _write(f, [
        {"type": "assistant", "message": {"content": [{"type": "text", "text": huge}]}},
    ])
    ev = transcripts.timeline(f, limit=100)[0]
    assert len(ev["search_text"]) == transcripts.SEARCH_CAP


def test_total_budget_flags_truncation(tmp_path, monkeypatch):
    monkeypatch.setattr(transcripts, "TOTAL_SEARCH_CAP", 100)   # tiny budget
    f = tmp_path / "t.jsonl"
    _write(f, [
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "a" * 500}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "b" * 500}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "c" * 500}]}},
    ])
    evs = transcripts.timeline(f, limit=100)
    # newest keeps its search_text; older ones get dropped once budget is spent
    assert any(e.get("search_text") for e in evs)
    assert any(not e.get("search_text") for e in evs)
    assert evs[0].get("search_truncated") is True


def test_early_events_not_dropped_for_small_file(tmp_path):
    # 300 short user lines; full-read keeps the earliest event searchable
    recs = [{"type": "user", "message": {"content": [{"type": "text", "text": f"msg {i}"}]}}
            for i in range(300)]
    recs[0]["message"]["content"][0]["text"] = "FIRST " + NEEDLE
    f = tmp_path / "t.jsonl"
    _write(f, recs)
    evs = transcripts.timeline(f, limit=2000)
    assert any(NEEDLE in (e.get("search_text") or "") for e in evs)


# ---------- Codex ----------

def test_codex_tool_output_full_searchable(tmp_path):
    out = "start " + "q" * 5000 + f" {NEEDLE} " + "r" * 5000   # needle past 4000 preview
    f = tmp_path / "r.jsonl"
    _write(f, [
        {"type": "session_meta", "payload": {"id": "x", "cwd": "/tmp"}},
        {"timestamp": "t", "type": "response_item",
         "payload": {"type": "function_call_output", "output": out}},
    ])
    ev = [e for e in codex.codex_timeline(f, limit=100) if e["kind"] == "tool_result"][0]
    assert NEEDLE in ev["search_text"]
    assert NEEDLE not in ev["text"]              # 4000-char preview doesn't reach it
