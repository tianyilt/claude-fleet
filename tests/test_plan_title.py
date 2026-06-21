"""History titles derived from a session's plan-mode plan (transcripts.plan_title)."""
import json

from core import transcripts


def _write(path, records):
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


def _plan_write(content, fp="/Users/x/.claude/plans/my-plan.md"):
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Write", "input": {"file_path": fp, "content": content}}]}}


def test_plan_title_from_h1(tmp_path):
    f = tmp_path / "t.jsonl"
    _write(f, [
        {"type": "user", "message": {"content": [{"type": "text", "text": "vague ask"}]}},
        _plan_write("# Fix the polling bottleneck\n\n## Context\n..."),
    ])
    assert transcripts.plan_title(f) == "Fix the polling bottleneck"


def test_plan_title_uses_latest_plan(tmp_path):
    f = tmp_path / "t.jsonl"
    _write(f, [
        _plan_write("# First plan"),
        _plan_write("# Revised plan"),
    ])
    assert transcripts.plan_title(f) == "Revised plan"


def test_plan_title_none_without_plan(tmp_path):
    f = tmp_path / "t.jsonl"
    _write(f, [
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Write",
             "input": {"file_path": "/Users/x/code/app.py", "content": "# not a plan"}}]}},
    ])
    assert transcripts.plan_title(f) is None


def test_plan_title_fallback_first_line(tmp_path):
    f = tmp_path / "t.jsonl"
    _write(f, [_plan_write("No heading here\nsecond line")])
    assert transcripts.plan_title(f) == "No heading here"
