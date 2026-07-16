"""Triage classifier: inspect each session's transcript to determine its state."""
from __future__ import annotations

import json
import re
import time
from collections import deque
from pathlib import Path
from typing import Optional

IDLE_THRESHOLD = 300     # 5 min
CLOSEABLE_THRESHOLD = 3600  # 1 hour

TRIAGE_PRIORITY = {
    "waiting_perm": 0,
    "stalled": 1,
    "completed": 2,
    "working": 3,
    "closeable": 4,
}


_BG_KEYWORDS = re.compile(r"等待|等.*通知|后台|background|polling|monitor|run_in_background", re.IGNORECASE)


# Cache by (mtime, size): this is the only whole-file read in classify(), and the
# 2s poll calls classify() on every live window. An idle window re-uses the cached
# result and does zero I/O; only an actively-written transcript is re-read.
_last_info_cache: dict[str, tuple[int, int, Optional[dict]]] = {}


def _last_assistant_info(transcript_path: str) -> Optional[dict]:
    """Extract stop_reason, last content block type, and background task status."""
    p = Path(transcript_path)
    try:
        st = p.stat()
    except OSError:
        return None
    mtime, size = int(st.st_mtime * 1000), st.st_size
    cached = _last_info_cache.get(transcript_path)
    if cached and cached[0] == mtime and cached[1] == size:
        return cached[2]

    # Only the last 40 lines matter — keep a bounded window instead of reading the
    # whole (possibly tens-of-MB) transcript into memory.
    try:
        with p.open() as f:
            lines: list[str] = list(deque(f, maxlen=40))
    except Exception:
        return None

    info = _compute_last_assistant_info(lines)
    _last_info_cache[transcript_path] = (mtime, size, info)
    return info


def _compute_last_assistant_info(lines: list[str]) -> Optional[dict]:

    # Check for active background tasks: only queue-operations AFTER the
    # last assistant end_turn count. If the session moved on past the bg
    # task phase, stale queue-ops don't indicate active work.
    has_pending_background = False
    last_end_turn_idx = -1
    tail = lines[-30:]
    for i, raw in enumerate(tail):
        try:
            d = json.loads(raw)
        except Exception:
            continue
        t = d.get("type", "")
        if t == "assistant" and (d.get("message") or {}).get("stop_reason") == "end_turn":
            last_end_turn_idx = i
            has_pending_background = False
        elif t == "queue-operation" and i > last_end_turn_idx:
            has_pending_background = True

    # Find the last assistant message for stop_reason etc.
    stop_reason = ""
    last_block_type = ""
    last_text = ""
    last_tool = ""
    for raw in reversed(lines[-40:]):
        try:
            d = json.loads(raw)
        except Exception:
            continue
        if d.get("type") != "assistant":
            continue
        msg = d.get("message") or {}
        content = msg.get("content") or []
        stop_reason = msg.get("stop_reason", "")
        if isinstance(content, list) and content:
            last_block = content[-1]
            last_block_type = last_block.get("type", "")
            if last_block_type == "text":
                last_text = last_block.get("text", "")
            elif last_block_type == "tool_use":
                last_tool = last_block.get("name", "")
            for c in reversed(content):
                if c.get("type") == "text" and c.get("text", "").strip():
                    last_text = c["text"].strip()
                    break
        break

    # Keyword fallback: only when the turn has NOT cleanly ended. A finished turn
    # (stop_reason == end_turn) that merely *mentions* monitoring/background is done,
    # not working — genuine post-turn background work is caught by the queue-operation
    # signal above (which resets on end_turn). Without this guard, any session whose
    # last message says "monitor / 后台 / 等待通知" is pinned to `working` forever.
    if (not has_pending_background and last_text and stop_reason != "end_turn"
            and _BG_KEYWORDS.search(last_text)):
        has_pending_background = True

    return {
        "stop_reason": stop_reason,
        "last_block_type": last_block_type,
        "last_text": last_text[:200],
        "last_tool": last_tool,
        "has_pending_background": has_pending_background,
    }


def prune_last_info_cache(live_paths) -> None:
    """Keep the last-assistant cache bounded to currently-live transcripts."""
    keep = {str(p) for p in live_paths if p}
    for tp in list(_last_info_cache.keys()):
        if tp not in keep:
            _last_info_cache.pop(tp, None)


def classify(window_dict: dict) -> dict:
    """Classify a window dict (from sessions.snapshot) into a triage state.

    Returns {triage, reason, suggestion}.
    """
    status = window_dict.get("status", "unknown")
    idle = window_dict.get("idle_seconds", 0)
    name = window_dict.get("name") or window_dict.get("project_name") or ""
    transcript = window_dict.get("transcript_path")

    if status == "waiting":
        return {
            "triage": "waiting_perm",
            "reason": window_dict.get("waiting_for") or "等待授权",
            "suggestion": "去终端批准",
        }

    if status == "busy" and idle < IDLE_THRESHOLD:
        return {
            "triage": "working",
            "reason": "正在工作",
            "suggestion": "",
        }

    if status == "shell":
        return {
            "triage": "working",
            "reason": "shell 进程运行中",
            "suggestion": "",
        }

    if not transcript:
        return {
            "triage": "closeable",
            "reason": "无 transcript 记录",
            "suggestion": "可以关闭",
        }

    info = _last_assistant_info(transcript)
    if not info:
        return {
            "triage": "closeable",
            "reason": "transcript 为空",
            "suggestion": "可以关闭",
        }

    stop = info["stop_reason"]
    idle_str = _format_idle(idle)

    # A background signal only means "working" while the session is recently active.
    # Past the closeable threshold (1h idle) fall through to the normal end_turn/
    # completed/closeable logic so a stale background hint never pins `working` forever.
    if info.get("has_pending_background") and idle < CLOSEABLE_THRESHOLD:
        summary = info["last_text"].split("\n")[0][:80] if info["last_text"] else ""
        return {
            "triage": "working",
            "reason": f"有后台任务在执行。{summary}",
            "suggestion": "",
        }

    if stop == "end_turn":
        summary = info["last_text"].split("\n")[0][:80] if info["last_text"] else ""
        if idle >= CLOSEABLE_THRESHOLD:
            return {
                "triage": "closeable",
                "reason": f"已完成，空闲 {idle_str}。{summary}",
                "suggestion": "可以关闭",
            }
        return {
            "triage": "completed",
            "reason": f"已完成，空闲 {idle_str}。{summary}",
            "suggestion": "建议 review",
        }

    if stop == "tool_use":
        tool = info["last_tool"]
        if status == "busy":
            return {
                "triage": "working",
                "reason": f"正在执行 {tool}" if tool else "正在工作",
                "suggestion": "",
            }
        return {
            "triage": "stalled",
            "reason": f"停在 {tool}，空闲 {idle_str}" if tool else f"中途停止，空闲 {idle_str}",
            "suggestion": "需要用户介入",
        }

    # Fallback
    if idle >= CLOSEABLE_THRESHOLD:
        return {
            "triage": "closeable",
            "reason": f"空闲 {idle_str}",
            "suggestion": "可以关闭",
        }
    return {
        "triage": "completed" if idle >= IDLE_THRESHOLD else "working",
        "reason": f"空闲 {idle_str}",
        "suggestion": "",
    }


def _format_idle(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    h = seconds // 3600
    m = (seconds % 3600) // 60
    return f"{h}h{m}m" if m else f"{h}h"
