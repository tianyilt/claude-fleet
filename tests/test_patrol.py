"""Tests for the triage classifier (core/patrol.py).

Focus: the background-task heuristic must not pin a *finished* session to
`working`. A session whose last turn ended cleanly (stop_reason == end_turn) but
whose text merely mentions monitoring/background used to be classified `working`
forever via the keyword fallback; it should now be `completed`. The reliable
queue-operation signal (a real pending background task) is preserved.
"""
import json

from core import patrol


# ---------- helpers: build synthetic transcript lines ----------
def _asst(stop, blocks):
    return json.dumps({"type": "assistant", "message": {"stop_reason": stop, "content": blocks}})


def _text(t):
    return {"type": "text", "text": t}


def _tool(name):
    return {"type": "tool_use", "name": name}


def _queue():
    return json.dumps({"type": "queue-operation", "operation": "add"})


def _info(**over):
    base = {"stop_reason": "end_turn", "last_text": "done", "last_block_type": "text",
            "last_tool": "", "has_pending_background": False}
    base.update(over)
    return base


# ---------- _compute_last_assistant_info: background detection ----------
def test_end_turn_with_bg_keyword_not_pending():
    """The regression: a finished turn that merely mentions monitor/后台/等待 is
    NOT a pending background task."""
    lines = [_asst("end_turn", [_text("全部完成，我在后台 monitor 传输，等待通知")])]
    info = patrol._compute_last_assistant_info(lines)
    assert info["stop_reason"] == "end_turn"
    assert info["has_pending_background"] is False


def test_midtool_with_bg_keyword_is_pending():
    """A turn that did NOT cleanly end (still on a tool_use) may use the keyword
    hint to flag background work."""
    lines = [_asst("tool_use", [_text("我在后台 monitor 传输"), _tool("Bash")])]
    info = patrol._compute_last_assistant_info(lines)
    assert info["stop_reason"] == "tool_use"
    assert info["has_pending_background"] is True


def test_queue_op_after_end_turn_is_pending():
    """The reliable signal: a queue-operation after the last end_turn means a real
    background task is still tracked — keep flagging it."""
    lines = [_asst("end_turn", [_text("done")]), _queue()]
    info = patrol._compute_last_assistant_info(lines)
    assert info["has_pending_background"] is True


def test_end_turn_no_keyword_not_pending():
    lines = [_asst("end_turn", [_text("交付完成，端口表已发飞书")])]
    info = patrol._compute_last_assistant_info(lines)
    assert info["has_pending_background"] is False


# ---------- classify: triage from (status, idle, info) ----------
def test_classify_completed_when_done_and_idle(monkeypatch):
    monkeypatch.setattr(patrol, "_last_assistant_info", lambda tp: _info())
    w = {"status": "idle", "idle_seconds": 120, "transcript_path": "/x", "name": "t"}
    assert patrol.classify(w)["triage"] == "completed"


def test_classify_working_when_bg_and_recent(monkeypatch):
    monkeypatch.setattr(patrol, "_last_assistant_info",
                        lambda tp: _info(last_text="后台 monitor", has_pending_background=True))
    w = {"status": "idle", "idle_seconds": 60, "transcript_path": "/x", "name": "t"}
    assert patrol.classify(w)["triage"] == "working"


def test_classify_bg_not_pinned_after_1h(monkeypatch):
    """Even a real-but-stale background hint must not pin `working` past the
    closeable threshold — it falls through to the end_turn/closeable logic."""
    monkeypatch.setattr(patrol, "_last_assistant_info",
                        lambda tp: _info(last_text="后台 monitor", has_pending_background=True))
    w = {"status": "idle", "idle_seconds": 4000, "transcript_path": "/x", "name": "t"}
    tri = patrol.classify(w)["triage"]
    assert tri != "working"
    assert tri == "closeable"   # end_turn + idle >= CLOSEABLE_THRESHOLD


def test_classify_end_to_end_completed_despite_bg_prose(monkeypatch):
    """Integration through _compute: a real transcript tail ending on end_turn with
    background prose classifies `completed`, not `working`."""
    lines = [_asst("end_turn", [_text("迁移完成，我用 Monitor 盯过后台传输了，等待你确认")])]
    monkeypatch.setattr(patrol, "_last_assistant_info",
                        lambda tp: patrol._compute_last_assistant_info(lines))
    w = {"status": "idle", "idle_seconds": 90, "transcript_path": "/x", "name": "t"}
    assert patrol.classify(w)["triage"] == "completed"
