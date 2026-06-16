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
