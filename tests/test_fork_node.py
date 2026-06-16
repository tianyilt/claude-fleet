"""Tests for fork-at-node: event uuid propagation + transcript truncation (#3)."""
import json

import pytest

from core import transcripts


def test_normalize_propagates_uuid():
    line = {"type": "user", "uuid": "u-42", "sessionId": "s",
            "message": {"role": "user", "content": "hi"}}
    evs = transcripts._normalize(line)
    assert evs and all(e.uuid == "u-42" for e in evs)


def _write_transcript(tmp_path):
    lines = [
        {"type": "user", "uuid": "u1", "sessionId": "old", "message": {"role": "user", "content": "a"}},
        {"type": "assistant", "uuid": "u2", "sessionId": "old", "message": {"role": "assistant", "content": "b"}},
        {"type": "user", "uuid": "u3", "sessionId": "old", "message": {"role": "user", "content": "c"}},
    ]
    f = tmp_path / "old.jsonl"
    f.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    return f


def test_fork_transcript_at_truncates_and_rewrites(tmp_path, monkeypatch):
    f = _write_transcript(tmp_path)
    monkeypatch.setattr(transcripts, "find_transcript_path", lambda sid: f)
    new_sid, new_path = transcripts.fork_transcript_at("old", "u2")
    out = [json.loads(l) for l in open(new_path) if l.strip()]
    assert [o["uuid"] for o in out] == ["u1", "u2"]          # truncated inclusive
    assert all(o["sessionId"] == new_sid for o in out)        # sessionId rewritten
    assert new_sid != "old"


def test_fork_transcript_at_missing_uuid_raises_and_cleans(tmp_path, monkeypatch):
    f = _write_transcript(tmp_path)
    monkeypatch.setattr(transcripts, "find_transcript_path", lambda sid: f)
    with pytest.raises(ValueError):
        transcripts.fork_transcript_at("old", "nope")
    # the half-written new file must be removed
    assert list(tmp_path.glob("*.jsonl")) == [f]


def test_fork_transcript_at_no_transcript_raises(monkeypatch):
    monkeypatch.setattr(transcripts, "find_transcript_path", lambda sid: None)
    with pytest.raises(FileNotFoundError):
        transcripts.fork_transcript_at("ghost", "u1")


def test_extract_plan_history_carries_source_uuid(tmp_path):
    """Plan versions must carry the source line uuid so the Plan panel can
    jump / fork at that node (the navigation feature)."""
    f = tmp_path / "s.jsonl"
    f.write_text(json.dumps({
        "type": "assistant", "uuid": "plan-uuid-1", "timestamp": "2026-06-16T00:00:00Z",
        "message": {"role": "assistant", "content": [
            {"type": "tool_use", "name": "Write",
             "input": {"file_path": "/home/x/.claude/plans/my-plan.md", "content": "# step 1"}},
        ]},
    }) + "\n")
    hist = transcripts.extract_plan_history(str(f))
    assert len(hist) == 1
    assert hist[0]["version_label"] == "v1"
    assert hist[0]["uuid"] == "plan-uuid-1"
