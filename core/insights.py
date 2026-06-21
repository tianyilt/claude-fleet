"""Cross-session aggregation for the Insights dashboard.

Rolls up the per-session `metrics` (see core/metrics.py) attached to every history
session into totals, time series, histograms, heatmaps and leaderboards. Pure
aggregation over data already extracted+cached upstream — no transcript re-reads.
"""
from __future__ import annotations

import datetime
from collections import Counter, defaultdict
from typing import Optional


def _parse_ts(ts: str) -> Optional[datetime.datetime]:
    if not ts:
        return None
    try:
        return datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _top(counter: dict, n: int) -> list[dict]:
    return [{"name": k, "count": v}
            for k, v in sorted(counter.items(), key=lambda x: -x[1])[:n]]


def build_insights(sessions: list[dict]) -> dict:
    """Aggregate a list of history-session dicts (each with a `metrics` block)."""
    n_claude = n_codex = 0
    tok_claude = tok_codex = 0
    total_cost = 0.0
    total_duration = 0

    by_model: dict[str, dict] = defaultdict(lambda: {"sessions": 0, "tokens": 0, "cost": 0.0})
    by_project: dict[str, dict] = defaultdict(lambda: {"sessions": 0, "tokens": 0, "cost": 0.0})
    by_day: dict[str, dict] = defaultdict(lambda: {"sessions": 0, "tokens": 0, "cost": 0.0})
    tools: Counter = Counter()
    files: Counter = Counter()
    heat_hour: Counter = Counter()   # 0..23
    heat_dow: Counter = Counter()    # 0=Mon..6=Sun

    rows: list[dict] = []  # flat rows for leaderboards

    for s in sessions:
        m = s.get("metrics") or {}
        plat = s.get("platform", "claude")
        toks = (m.get("tokens") or {}).get("total", 0) or 0
        cost = m.get("cost_usd") or 0.0
        dur = m.get("duration_sec") or 0
        model = (m.get("model") or s.get("model") or "unknown") or "unknown"
        proj = s.get("project_name") or "—"

        if plat == "codex":
            n_codex += 1
            tok_codex += toks
        else:
            n_claude += 1
            tok_claude += toks
        total_cost += cost
        total_duration += dur

        by_model[model]["sessions"] += 1
        by_model[model]["tokens"] += toks
        by_model[model]["cost"] += cost
        by_project[proj]["sessions"] += 1
        by_project[proj]["tokens"] += toks
        by_project[proj]["cost"] += cost

        dt = _parse_ts(s.get("first_ts") or s.get("last_ts") or "")
        if dt:
            day = dt.date().isoformat()
            by_day[day]["sessions"] += 1
            by_day[day]["tokens"] += toks
            by_day[day]["cost"] += cost
            heat_hour[dt.hour] += 1
            heat_dow[dt.weekday()] += 1

        for name, cnt in (m.get("tools") or {}).items():
            tools[name] += cnt
        for fp in (m.get("files") or []):
            files[fp] += 1

        rows.append({
            "session_id": s.get("session_id", ""),
            "project_name": proj,
            "platform": plat,
            "model": model,
            "tokens": toks,
            "cost_usd": round(cost, 2),
            "duration_sec": dur,
            "turns": m.get("turns", 0) or 0,
        })

    def _leaders(key: str, n: int = 10) -> list[dict]:
        return sorted([r for r in rows if r.get(key)], key=lambda r: -r[key])[:n]

    def _model_rows() -> list[dict]:
        out = [{"model": k, **v} for k, v in by_model.items()]
        for r in out:
            r["cost"] = round(r["cost"], 2)
        return sorted(out, key=lambda r: -r["tokens"])

    def _project_rows() -> list[dict]:
        out = [{"project": k, **v} for k, v in by_project.items()]
        for r in out:
            r["cost"] = round(r["cost"], 2)
        return sorted(out, key=lambda r: -r["tokens"])[:15]

    def _day_rows() -> list[dict]:
        out = [{"day": k, **v} for k, v in by_day.items()]
        for r in out:
            r["cost"] = round(r["cost"], 2)
        return sorted(out, key=lambda r: r["day"])

    return {
        "totals": {
            "sessions": n_claude + n_codex,
            "sessions_claude": n_claude,
            "sessions_codex": n_codex,
            "tokens_claude": tok_claude,
            "tokens_codex": tok_codex,
            "tokens_total": tok_claude + tok_codex,
            "cost_usd": round(total_cost, 2),
            "duration_sec": total_duration,
        },
        "by_model": _model_rows(),
        "by_project": _project_rows(),
        "by_day": _day_rows(),
        "tools": _top(tools, 20),
        "hot_files": _top(files, 25),
        "heatmap": {
            "hour": [heat_hour.get(h, 0) for h in range(24)],
            "dow": [heat_dow.get(d, 0) for d in range(7)],
        },
        "leaderboards": {
            "cost": _leaders("cost_usd"),
            "duration": _leaders("duration_sec"),
            "turns": _leaders("turns"),
            "tokens": _leaders("tokens"),
        },
    }
