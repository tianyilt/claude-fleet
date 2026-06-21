"""Per-session mining metrics: tokens, cost, duration, tool/file usage, errors.

Both Claude and Codex transcripts carry rich usage data that the dashboard never
surfaced. This module extracts a platform-neutral `metrics` dict so History rows
and the Insights page can aggregate cost, velocity, tool histograms, hot files,
and error signals.

The metrics shape is shared across platforms (missing fields are None):
    tokens: {input, output, cache_read, cache_creation, reasoning, total}
    cache_hit: float|None      cache_read / (input + cache_creation + cache_read)
    context_pct: float|None    codex rate-limit / context-window usage; None for claude
    duration_sec: int|None     last_ts - first_ts
    turns: int                 user+assistant messages
    tools: {name: count}       tool / function-call histogram
    files: [path, ...]         files touched (claude Edit/Write/Read; [] for codex)
    errors: int                error/limit signals
    cost_usd: float|None       claude only, via PRICING; None for codex (subscription)
    model: str
"""
from __future__ import annotations

import datetime
from pathlib import Path
from typing import Optional


# $ per 1M tokens. Editable; only used for Claude $ estimates (Codex is a
# subscription, so we show tokens/context% instead of dollars). Cache-read is the
# 5m-cache discount; cache_creation ("write") is the surcharge for creating it.
PRICING: dict[str, dict[str, float]] = {
    # base-model prefix -> rates. Matched by `model.startswith(prefix)`.
    "claude-opus-4":   {"input": 15.0, "output": 75.0, "cache_read": 1.5,  "cache_write": 18.75},
    "claude-sonnet-4": {"input": 3.0,  "output": 15.0, "cache_read": 0.30, "cache_write": 3.75},
    "claude-haiku-4":  {"input": 1.0,  "output": 5.0,  "cache_read": 0.10, "cache_write": 1.25},
    "claude-3-5-haiku": {"input": 0.80, "output": 4.0, "cache_read": 0.08, "cache_write": 1.0},
}


def _rates(model: str) -> Optional[dict[str, float]]:
    for prefix, r in PRICING.items():
        if model and model.startswith(prefix):
            return r
    return None


def cost_usd(tokens: dict, model: str) -> Optional[float]:
    """Estimate Claude session cost from a tokens dict. None if model unpriced."""
    r = _rates(model or "")
    if not r:
        return None
    return round(
        (tokens.get("input", 0) * r["input"]
         + tokens.get("output", 0) * r["output"]
         + tokens.get("cache_read", 0) * r["cache_read"]
         + tokens.get("cache_creation", 0) * r["cache_write"]) / 1_000_000.0,
        4,
    )


def _empty_tokens() -> dict:
    return {"input": 0, "output": 0, "cache_read": 0,
            "cache_creation": 0, "reasoning": 0, "total": 0}


def _parse_ts(ts: str) -> Optional[datetime.datetime]:
    if not ts:
        return None
    try:
        return datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _duration(first: Optional[str], last: Optional[str]) -> Optional[int]:
    a, b = _parse_ts(first or ""), _parse_ts(last or "")
    if a and b:
        return max(0, int((b - a).total_seconds()))
    return None


def claude_metrics(path: str | Path) -> dict:
    """Extract metrics from a Claude transcript (~/.claude/projects/.../*.jsonl)."""
    from .transcripts import _iter_lines

    tokens = _empty_tokens()
    tools: dict[str, int] = {}
    files: set[str] = set()
    turns = 0
    errors = 0
    model = ""
    first_ts = last_ts = ""

    for d in _iter_lines(Path(path)):
        ts = d.get("timestamp", "")
        if ts:
            if not first_ts:
                first_ts = ts
            last_ts = ts
        typ = d.get("type")
        if typ in ("user", "assistant"):
            turns += 1
        msg = d.get("message") or {}
        if typ == "assistant":
            if msg.get("model"):
                model = msg["model"]
            u = msg.get("usage") or {}
            tokens["input"] += u.get("input_tokens", 0) or 0
            tokens["output"] += u.get("output_tokens", 0) or 0
            tokens["cache_read"] += u.get("cache_read_input_tokens", 0) or 0
            tokens["cache_creation"] += u.get("cache_creation_input_tokens", 0) or 0
            if msg.get("stop_reason") == "max_tokens":
                errors += 1
            for b in (msg.get("content") or []):
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_use":
                    name = b.get("name", "?")
                    tools[name] = tools.get(name, 0) + 1
                    inp = b.get("input") or {}
                    fp = inp.get("file_path") or inp.get("notebook_path")
                    if fp:
                        files.add(fp)
        # tool_result error markers live on user records
        if typ == "user":
            for b in (msg.get("content") or []):
                if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("is_error"):
                    errors += 1

    tokens["total"] = (tokens["input"] + tokens["output"]
                       + tokens["cache_read"] + tokens["cache_creation"])
    seen_input = tokens["input"] + tokens["cache_creation"] + tokens["cache_read"]
    cache_hit = round(tokens["cache_read"] / seen_input, 3) if seen_input else None
    return {
        "tokens": tokens,
        "cache_hit": cache_hit,
        "context_pct": None,
        "duration_sec": _duration(first_ts, last_ts),
        "turns": turns,
        "tools": tools,
        "files": sorted(files),
        "errors": errors,
        "cost_usd": cost_usd(tokens, model),
        "model": model,
    }
