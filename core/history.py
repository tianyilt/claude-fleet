"""Index all past sessions from history.jsonl + projects/**/*.jsonl + codex."""
from __future__ import annotations

import datetime
import json
import os
import subprocess
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Optional

from .sessions import CLAUDE_HOME, HOME_BASE, PROJECTS_DIR

HISTORY_JSONL = CLAUDE_HOME / "history.jsonl"


def _norm_ts(ts) -> str:
    """Normalize a timestamp to an ISO string.

    Newer Claude Code writes history.jsonl `timestamp` as an epoch number
    (seconds or milliseconds) instead of an ISO string; the frontend calls
    `.slice()` on it, so a number throws and breaks Alpine row rendering.
    """
    if isinstance(ts, str):
        return ts
    if isinstance(ts, (int, float)):
        secs = ts / 1000 if ts > 1e12 else ts
        try:
            return datetime.datetime.fromtimestamp(
                secs, tz=datetime.timezone.utc
            ).isoformat()
        except (ValueError, OverflowError, OSError):
            return ""
    return ""


@dataclass
class HistorySession:
    session_id: str
    project: str
    project_name: str
    first_input: str
    input_count: int
    first_ts: str
    last_ts: str
    transcript_path: Optional[str]
    transcript_size: int
    transcript_mtime: int
    is_alive: bool
    platform: str = "claude"
    model: str = ""
    skills_used: list = field(default_factory=list)
    memory_ops: list = field(default_factory=list)
    skill_breakdown: dict = field(default_factory=dict)
    memory_breakdown: dict = field(default_factory=dict)
    metrics: dict = field(default_factory=dict)
    plan_title: str = ""


_cache: list[HistorySession] = []
_cache_ts: float = 0
_CACHE_TTL = 30


def _load_history_jsonl() -> dict[str, dict]:
    out: dict[str, dict] = {}
    if not HISTORY_JSONL.exists():
        return out
    try:
        with HISTORY_JSONL.open(errors="replace") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                sid = d.get("sessionId", "")
                if not sid:
                    continue
                display = d.get("display", "")
                ts = _norm_ts(d.get("timestamp", ""))
                project = d.get("project", "")
                if sid not in out:
                    out[sid] = {
                        "first_input": display[:300],
                        "first_ts": ts,
                        "last_ts": ts,
                        "project": project,
                        "count": 0,
                    }
                out[sid]["count"] += 1
                out[sid]["last_ts"] = ts
    except Exception:
        pass
    return out


def _scan_transcripts() -> dict[str, dict]:
    out: dict[str, dict] = {}
    if not PROJECTS_DIR.exists():
        return out
    for proj_dir in PROJECTS_DIR.iterdir():
        if not proj_dir.is_dir():
            continue
        for f in proj_dir.glob("*.jsonl"):
            if f.name.endswith(".wakatime"):
                continue
            if "subagents" in f.parts:
                continue
            sid = f.stem
            try:
                st = f.stat()
            except Exception:
                continue
            out[sid] = {
                "path": str(f),
                "size": st.st_size,
                "mtime": int(st.st_mtime * 1000),
                "project_slug": proj_dir.name,
            }
    return out


def _find_alive_pids() -> set[str]:
    sessions_dir = CLAUDE_HOME / "sessions"
    alive: set[str] = set()
    if not sessions_dir.exists():
        return alive
    for f in sessions_dir.glob("*.json"):
        if f.name.startswith("session-"):
            continue
        try:
            d = json.loads(f.read_text())
            pid = d.get("pid")
            sid = d.get("sessionId", "")
            if pid and sid:
                try:
                    os.kill(int(pid), 0)
                    alive.add(sid)
                except (ProcessLookupError, PermissionError, OSError):
                    pass
        except Exception:
            pass
    return alive


def _extract_skills_from_transcript(path: Path) -> list[str]:
    """Extract unique skill names invoked via Skill tool_use."""
    skills: list[str] = []
    seen: set[str] = set()
    try:
        with path.open() as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("type") != "assistant":
                    continue
                content = (d.get("message") or {}).get("content", [])
                if not isinstance(content, list):
                    continue
                for c in content:
                    if isinstance(c, dict) and c.get("name") == "Skill":
                        skill_name = (c.get("input") or {}).get("skill", "")
                        if skill_name and skill_name not in seen:
                            seen.add(skill_name)
                            skills.append(skill_name)
    except Exception:
        pass
    return skills


# Per-transcript enrichment cache keyed by path -> (mtime, size, enrichment).
# Parsing a transcript (skills/memory/model) is the dominant cost of _build_index
# and the file is immutable once a session ends, so cache by (mtime, size) and
# only re-parse files that actually changed.
_enrich_cache: dict[str, tuple[int, int, dict]] = {}


def _enrich_transcript(tp: str, mtime: int, size: int) -> dict:
    cached = _enrich_cache.get(tp)
    if cached and cached[0] == mtime and cached[1] == size:
        return cached[2]
    from .transcripts import extract_memory_ops, count_skill_activity, count_memory_activity, plan_title
    from .metrics import claude_metrics
    path = Path(tp)
    sa = count_skill_activity(tp)
    enrichment = {
        "plan_title": plan_title(path),
        "first_input": _extract_first_user_text(path),
        "skills": _extract_skills_from_transcript(path),
        "mem_ops": extract_memory_ops(tp),
        "model": _extract_model(path),
        "skill_breakdown": {
            "per_skill_invokes": sa.get("per_skill_invokes", {}),
            "per_skill_reads": sa.get("per_skill_reads", {}),
            "per_skill_writes": sa.get("per_skill_writes", {}),
            "per_skill_bash_refs": sa.get("per_skill_bash_refs", {}),
        },
        "memory_breakdown": count_memory_activity(tp),
        "metrics": claude_metrics(tp),
    }
    _enrich_cache[tp] = (mtime, size, enrichment)
    return enrichment


def _build_index() -> list[HistorySession]:
    hist = _load_history_jsonl()
    transcripts = _scan_transcripts()
    alive = _find_alive_pids()

    all_sids = set(hist.keys()) | set(transcripts.keys())
    sessions: list[HistorySession] = []

    for sid in all_sids:
        h = hist.get(sid, {})
        t = transcripts.get(sid, {})

        project = h.get("project", "")
        tp = t.get("path")
        if not project and tp:
            # history.jsonl had no entry for this session — recover the real cwd
            # from the transcript so Resume/Fork open in the right directory
            # (otherwise we'd fall back to $HOME and `claude --resume` would fail
            # with "No conversation found", flash-closing the terminal).
            from .transcripts import transcript_cwd
            project = transcript_cwd(tp) or ""
        project_name = project.rsplit("/", 1)[-1] if project else (
            t.get("project_slug", "").replace("-", "/").split("/")[-1] or "unknown"
        )

        first_input = h.get("first_input", "")
        skills = []
        mem_ops = []
        model = ""
        skill_breakdown = {}
        memory_breakdown = {}
        metrics = {}
        plan_title = ""
        if tp:
            enr = _enrich_transcript(tp, t.get("mtime", 0), t.get("size", 0))
            if not first_input:
                first_input = enr["first_input"]
            skills = enr["skills"]
            mem_ops = enr["mem_ops"]
            model = enr["model"]
            skill_breakdown = enr["skill_breakdown"]
            memory_breakdown = enr["memory_breakdown"]
            metrics = enr.get("metrics", {})
            plan_title = enr.get("plan_title") or ""

        sessions.append(HistorySession(
            session_id=sid,
            project=project,
            project_name=project_name,
            first_input=first_input,
            input_count=h.get("count", 0),
            first_ts=h.get("first_ts", ""),
            last_ts=h.get("last_ts", ""),
            transcript_path=tp,
            transcript_size=t.get("size", 0),
            transcript_mtime=t.get("mtime", 0),
            is_alive=sid in alive,
            platform="claude",
            model=model,
            skills_used=skills,
            memory_ops=mem_ops,
            skill_breakdown=skill_breakdown,
            memory_breakdown=memory_breakdown,
            metrics=metrics,
            plan_title=plan_title,
        ))

    # Merge Codex sessions
    try:
        from .codex import list_codex_sessions
        for cs in list_codex_sessions():
            d = cs.to_history_dict()
            sessions.append(HistorySession(**d))
    except Exception:
        pass

    # Merge OpenCode sessions
    try:
        from .opencode import list_opencode_sessions
        for oc in list_opencode_sessions():
            sessions.append(HistorySession(**oc))
    except Exception:
        pass

    sessions.sort(key=lambda s: s.transcript_mtime or 0, reverse=True)
    return sessions


def _extract_first_user_text(path: Path) -> str:
    try:
        with path.open() as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("type") != "user":
                    continue
                msg = d.get("message", {})
                content = msg.get("content", [])
                if isinstance(content, str):
                    return content[:300]
                if isinstance(content, list):
                    for c in content:
                        if isinstance(c, dict) and c.get("type") == "text":
                            return (c.get("text") or "")[:300]
    except Exception:
        pass
    return ""


def _extract_model(path: Path) -> str:
    try:
        with path.open() as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("type") != "assistant":
                    continue
                return (d.get("message") or {}).get("model", "")
    except Exception:
        pass
    return ""


def _rg_search_sessions(query: str) -> dict[str, list[str]]:
    """Use ripgrep to find session IDs + match snippets.

    Returns {session_id: [snippet1, snippet2, ...]}.
    """
    import re as _re
    search_dirs: list[str] = []
    if PROJECTS_DIR.exists():
        search_dirs.append(str(PROJECTS_DIR))
    codex_dir = HOME_BASE / ".codex" / "sessions"
    if codex_dir.exists():
        search_dirs.append(str(codex_dir))
    if not search_dirs:
        return {}
    cmd = [
        "rg", "-i", "-S",
        "--max-count", "3",
        "-g", "*.jsonl",
        "-g", "!*.wakatime",
        "-g", "!*subagents*",
        "--no-heading",
        query,
    ] + search_dirs
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {}
    result: dict[str, list[str]] = {}
    ql = query.lower()
    for raw_line in proc.stdout.splitlines():
        # format: /path/to/sid.jsonl:jsonl_content
        colon = raw_line.find(".jsonl:")
        if colon < 0:
            continue
        fpath = raw_line[:colon + 6]
        content = raw_line[colon + 7:]
        sid = Path(fpath).stem
        # Extract a human-readable snippet around the match
        idx = content.lower().find(ql)
        if idx < 0:
            continue
        start = max(0, idx - 60)
        end = min(len(content), idx + len(query) + 60)
        snippet = content[start:end].replace("\n", " ").replace("\\n", " ").strip()
        if start > 0:
            snippet = "…" + snippet
        if end < len(content):
            snippet = snippet + "…"
        if sid not in result:
            result[sid] = []
        if len(result[sid]) < 3:
            result[sid].append(snippet)
    return result


_SORT_KEYS = {
    "tokens": lambda s: (s.metrics.get("tokens") or {}).get("total", 0) or 0,
    "cost": lambda s: s.metrics.get("cost_usd") or 0,
    "duration": lambda s: s.metrics.get("duration_sec") or 0,
    "turns": lambda s: s.metrics.get("turns") or 0,
}


def list_sessions(
    q: Optional[str] = None,
    page: int = 1,
    limit: int = 30,
    include_alive: bool = True,
    platform: Optional[str] = None,
    platforms: Optional[list] = None,
    skills: Optional[list] = None,
    sort: str = "recency",
) -> dict:
    global _cache, _cache_ts
    now = time.time()
    if now - _cache_ts > _CACHE_TTL or not _cache:
        _cache = _build_index()
        _cache_ts = now

    filtered = _cache
    if not include_alive:
        filtered = [s for s in filtered if not s.is_alive]
    if platform:
        filtered = [s for s in filtered if s.platform == platform]
    # Multi-select facets (Feishu-bitable style): keep sessions matching ANY of
    # the selected platforms, and ANY of the selected skills.
    if platforms:
        pset = {p for p in platforms if p}
        if pset:
            filtered = [s for s in filtered if s.platform in pset]
    if skills:
        skset = {sk for sk in skills if sk}
        if skset:
            filtered = [s for s in filtered if skset & set(s.skills_used or [])]
    rg_matches: dict[str, list[str]] = {}
    if q:
        ql = q.lower()
        meta_sids = {
            s.session_id for s in filtered
            if ql in s.first_input.lower()
            or ql in s.project_name.lower()
            or ql in s.session_id.lower()
            or ql in s.project.lower()
            or ql in (s.transcript_path or "").lower()
        }
        rg_matches = _rg_search_sessions(q)
        # Also search OpenCode SQLite
        try:
            from .opencode import search_opencode
            oc_matches = search_opencode(q)
            for sid, snips in oc_matches.items():
                if sid not in rg_matches:
                    rg_matches[sid] = snips
                else:
                    rg_matches[sid].extend(snips[:2])
        except Exception:
            pass
        all_sids = meta_sids | set(rg_matches.keys())
        filtered = [s for s in filtered if s.session_id in all_sids]

    # Rank by a numeric metric (desc); default keeps the recency order of _cache.
    keyfn = _SORT_KEYS.get(sort)
    if keyfn:
        filtered = sorted(filtered, key=keyfn, reverse=True)

    total = len(filtered)
    start = (page - 1) * limit
    page_items = filtered[start : start + limit]

    sessions_out = []
    for s in page_items:
        d = asdict(s)
        d["match_snippets"] = rg_matches.get(s.session_id, [])
        sessions_out.append(d)
    return {
        "total": total,
        "page": page,
        "limit": limit,
        "sessions": sessions_out,
    }
