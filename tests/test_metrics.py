"""Tests for per-session mining metrics + insights aggregation (Phase 2)."""
import json

from core import codex, insights, metrics


def _write(path, records):
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


# ---------- pricing / cost ----------

def test_cost_usd_opus():
    toks = {"input": 1_000_000, "output": 1_000_000, "cache_read": 0, "cache_creation": 0}
    # opus: 15 in + 75 out per 1M
    assert metrics.cost_usd(toks, "claude-opus-4-8") == 90.0


def test_cost_usd_unknown_model_is_none():
    assert metrics.cost_usd({"input": 1_000_000}, "gpt-5.5") is None


# ---------- claude metrics ----------

def test_claude_metrics_tokens_tools_files_errors(tmp_path):
    f = tmp_path / "c.jsonl"
    _write(f, [
        {"type": "user", "timestamp": "2026-06-20T10:00:00Z",
         "message": {"content": [{"type": "text", "text": "hi"}]}},
        {"type": "assistant", "timestamp": "2026-06-20T10:01:00Z",
         "message": {"model": "claude-opus-4-8", "stop_reason": "tool_use",
                     "usage": {"input_tokens": 100, "output_tokens": 50,
                               "cache_read_input_tokens": 900, "cache_creation_input_tokens": 0},
                     "content": [
                         {"type": "tool_use", "name": "Edit", "input": {"file_path": "/x/a.py"}},
                         {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
                     ]}},
        {"type": "assistant", "timestamp": "2026-06-20T10:05:00Z",
         "message": {"model": "claude-opus-4-8", "stop_reason": "max_tokens",
                     "usage": {"input_tokens": 10, "output_tokens": 5},
                     "content": [{"type": "tool_use", "name": "Edit", "input": {"file_path": "/x/a.py"}}]}},
    ])
    m = metrics.claude_metrics(f)
    assert m["tokens"]["input"] == 110 and m["tokens"]["output"] == 55
    assert m["tokens"]["cache_read"] == 900
    assert m["tools"] == {"Edit": 2, "Bash": 1}
    assert m["files"] == ["/x/a.py"]          # deduped
    assert m["errors"] == 1                     # one max_tokens
    assert m["turns"] == 3
    assert m["duration_sec"] == 300             # 10:00 -> 10:05
    assert m["model"] == "claude-opus-4-8"
    assert m["cost_usd"] is not None            # opus is priced


def test_claude_metrics_tool_result_error(tmp_path):
    f = tmp_path / "c.jsonl"
    _write(f, [
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "is_error": True, "content": "boom"}]}},
    ])
    assert metrics.claude_metrics(f)["errors"] == 1


# ---------- codex metrics (via activity extractor) ----------

def test_codex_metrics_from_token_count(tmp_path):
    f = tmp_path / "rollout.jsonl"
    _write(f, [
        {"type": "session_meta", "timestamp": "2026-06-20T00:00:00Z",
         "payload": {"id": "x", "cwd": "/w"}},
        {"type": "turn_context", "payload": {"model": "gpt-5.5"}},
        {"type": "response_item", "payload": {"type": "function_call", "name": "exec_command",
         "arguments": "{\"cmd\": \"ls\"}"}},
        {"type": "response_item", "payload": {"type": "message", "role": "user",
         "content": [{"type": "input_text", "text": "do it"}]}},
        {"type": "response_item", "payload": {"type": "message", "role": "assistant",
         "content": [{"type": "output_text", "text": "ok"}]}},
        {"type": "event_msg", "timestamp": "2026-06-20T00:10:00Z",
         "payload": {"type": "token_count", "info": {
             "total_token_usage": {"input_tokens": 5000, "cached_input_tokens": 4000,
                                   "output_tokens": 200, "reasoning_output_tokens": 50,
                                   "total_tokens": 5250},
             "model_context_window": 258400},
             "rate_limits": {"primary": {"used_percent": 12.5}}}},
    ])
    m = codex.extract_codex_session_activity(f)["metrics"]
    assert m["tokens"]["input"] == 5000 and m["tokens"]["total"] == 5250
    assert m["tokens"]["cache_read"] == 4000 and m["tokens"]["reasoning"] == 50
    assert m["context_pct"] == 12.5 and m["context_window"] == 258400
    assert m["tools"] == {"exec_command": 1}
    assert m["turns"] == 2                       # one user + one assistant message
    assert m["duration_sec"] == 600              # 00:00 -> 00:10
    assert m["cost_usd"] is None                 # codex: no $
    assert m["model"] == "gpt-5.5"


# ---------- insights aggregation ----------

def _sess(platform, model, tokens, cost, dur, turns, tools, files, proj, ts):
    return {
        "session_id": f"{platform}-{model}-{tokens}", "platform": platform,
        "project_name": proj, "model": model, "first_ts": ts, "last_ts": ts,
        "metrics": {
            "tokens": {"total": tokens}, "cost_usd": cost, "duration_sec": dur,
            "turns": turns, "tools": tools, "files": files, "model": model,
            "context_pct": None,
        },
    }


def test_build_insights_aggregates():
    sessions = [
        _sess("claude", "claude-opus-4-8", 1000, 1.5, 100, 10, {"Edit": 3}, ["/a.py"], "projA", "2026-06-20T09:00:00Z"),
        _sess("claude", "claude-opus-4-8", 2000, 2.5, 200, 20, {"Bash": 5}, ["/a.py", "/b.py"], "projA", "2026-06-20T14:00:00Z"),
        _sess("codex", "gpt-5.5", 5000, 0, 300, 30, {"exec_command": 9}, [], "projB", "2026-06-21T10:00:00Z"),
    ]
    ins = insights.build_insights(sessions)
    t = ins["totals"]
    assert t["sessions"] == 3 and t["sessions_claude"] == 2 and t["sessions_codex"] == 1
    assert t["tokens_total"] == 8000 and t["tokens_codex"] == 5000
    assert t["cost_usd"] == 4.0
    # model rollup
    opus = next(r for r in ins["by_model"] if r["model"] == "claude-opus-4-8")
    assert opus["sessions"] == 2 and opus["tokens"] == 3000
    # tool histogram merges across sessions
    tools = {x["name"]: x["count"] for x in ins["tools"]}
    assert tools == {"exec_command": 9, "Bash": 5, "Edit": 3}
    # hot files: /a.py touched in 2 sessions
    hot = {x["name"]: x["count"] for x in ins["hot_files"]}
    assert hot["/a.py"] == 2 and hot["/b.py"] == 1
    # leaderboards present
    assert ins["leaderboards"]["cost"][0]["cost_usd"] == 2.5
    # heatmap buckets (UTC hours 9, 14, 10)
    assert ins["heatmap"]["hour"][9] == 1 and ins["heatmap"]["hour"][14] == 1


def test_build_insights_empty():
    ins = insights.build_insights([])
    assert ins["totals"]["sessions"] == 0
    assert ins["by_model"] == [] and ins["tools"] == []
