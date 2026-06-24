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


def _session_title(s: dict) -> str:
    return (s.get("plan_title")
            or (s.get("first_input") or "")[:70]
            or s.get("session_id", "")[:8])


def fork_forest(sessions: list[dict], top: int = 20) -> dict:
    """Reconstruct the fork lineage from the per-request ledgers.

    A fork copies its parent's transcript prefix, so the copied requestIds are
    "inherited". A session's parent is the owner (earliest holder) of the LAST
    request it inherited — which is exactly the fork point's predecessor, so this
    handles multi-level chains (A→B→C). Returns the family trees that actually
    have forks, ranked so the most-reused "mother" sessions surface first.
    """
    from . import metrics as _metrics

    ordered = sorted(sessions, key=lambda s: s.get("first_ts") or s.get("last_ts") or "")
    owner: dict[str, str] = {}        # requestId -> earliest session_id
    node: dict[str, dict] = {}

    for s in ordered:
        sid = s.get("session_id", "")
        if not sid or sid in node:
            continue
        m = s.get("metrics") or {}
        model = m.get("model") or "unknown"
        reqs = m.get("requests") or []
        owned_cost = 0.0
        owned_tokens = 0
        parent = None
        for rid, ti, to, tcr, tcc in reqs:
            if rid in owner:
                parent = owner[rid]          # last inherited wins → immediate parent
            else:
                owner[rid] = sid
                owned_cost += _metrics.request_cost(ti, to, tcr, tcc, model)
                owned_tokens += ti + to + tcr + tcc
        node[sid] = {
            "session_id": sid,
            "title": _session_title(s),
            "project": s.get("project_name"),
            "platform": s.get("platform", "claude"),
            "first_ts": s.get("first_ts", ""),
            "parent": parent if (parent and parent != sid) else None,
            "children": [],
            "owned_cost": round(owned_cost, 2),
            "owned_tokens": owned_tokens,
        }

    for sid, n in node.items():
        if n["parent"] and n["parent"] in node:
            node[n["parent"]]["children"].append(sid)

    def subtree(sid: str) -> tuple:
        n = node[sid]
        cnt = 0
        cost = n["owned_cost"]
        for c in n["children"]:
            ccnt, ccost = subtree(c)
            cnt += 1 + ccnt
            cost += ccost
        n["descendants"] = cnt
        n["subtree_cost"] = round(cost, 2)
        return cnt, cost

    for sid, n in node.items():
        if not n["parent"]:
            subtree(sid)

    def as_tree(sid: str) -> dict:
        n = node[sid]
        return {
            "session_id": sid, "title": n["title"], "project": n["project"],
            "platform": n["platform"], "first_ts": n["first_ts"],
            "owned_cost": n["owned_cost"], "descendants": n.get("descendants", 0),
            "subtree_cost": n.get("subtree_cost", n["owned_cost"]),
            "children": [as_tree(c) for c in sorted(
                n["children"], key=lambda c: node[c].get("first_ts") or "")],
        }

    # Family roots that actually branched, ranked by how much descends from them.
    roots = [n for n in node.values()
             if not n["parent"] and n.get("descendants", 0) > 0]
    roots.sort(key=lambda n: (-n.get("descendants", 0), -n.get("subtree_cost", 0)))
    return {
        "families": [as_tree(n["session_id"]) for n in roots[:top]],
        "family_count": len(roots),
        "forked_sessions": sum(1 for n in node.values() if n["parent"]),
    }


def build_insights(sessions: list[dict]) -> dict:
    """Aggregate a list of history-session dicts (each with a `metrics` block).

    Cost/token totals are DEDUPED by requestId across the whole set: forks copy the
    parent's transcript prefix, so the same API requests appear in several sessions.
    Each request is attributed to the EARLIEST session that holds it (oldest
    first_ts), so a long fork chain is billed once, not once per fork.
    """
    from . import metrics as _metrics

    n_claude = n_codex = 0
    tok_claude = tok_codex = 0
    total_cost = 0.0          # deduped
    total_cost_naive = 0.0    # sum of per-session standalone cost (for the savings note)
    tok_total_naive = 0
    total_duration = 0
    seen_requests: set = set()   # requestIds already attributed to an earlier session

    by_model: dict[str, dict] = defaultdict(lambda: {"sessions": 0, "tokens": 0, "cost": 0.0})
    by_project: dict[str, dict] = defaultdict(lambda: {"sessions": 0, "tokens": 0, "cost": 0.0})
    by_day: dict[str, dict] = defaultdict(lambda: {"sessions": 0, "tokens": 0, "cost": 0.0})
    tools: Counter = Counter()
    files: Counter = Counter()
    heat_hour: Counter = Counter()   # 0..23
    heat_dow: Counter = Counter()    # 0=Mon..6=Sun

    rows: list[dict] = []  # flat rows for leaderboards

    # Oldest first so the original session owns shared requests; forks keep only
    # the new work they added.
    sessions = sorted(sessions, key=lambda s: s.get("first_ts") or s.get("last_ts") or "")

    for s in sessions:
        m = s.get("metrics") or {}
        plat = s.get("platform", "claude")
        model = (m.get("model") or s.get("model") or "unknown") or "unknown"
        proj = s.get("project_name") or "—"
        dur = m.get("duration_sec") or 0

        naive_toks = (m.get("tokens") or {}).get("total", 0) or 0
        naive_cost = m.get("cost_usd") or 0.0
        tok_total_naive += naive_toks
        total_cost_naive += naive_cost

        # Deduped tokens/cost for this session = only requests not seen earlier.
        ledger = m.get("requests")
        if ledger is not None:
            toks = 0
            cost = 0.0
            for rid, ti, to, tcr, tcc in ledger:
                if rid in seen_requests:
                    continue
                seen_requests.add(rid)
                toks += ti + to + tcr + tcc
                cost += _metrics.request_cost(ti, to, tcr, tcc, model)
        else:
            # codex / no ledger: nothing to dedup
            toks = naive_toks
            cost = naive_cost

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
            "cost_usd_naive": round(total_cost_naive, 2),
            "tokens_total_naive": tok_total_naive,
            "dup_tokens": max(0, tok_total_naive - (tok_claude + tok_codex)),
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
