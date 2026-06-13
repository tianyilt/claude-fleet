"""Claude Fleet — FastAPI app: dashboard backend + SSE."""
from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

from core import actions, codex, history, memory, patrol, perms, plans, search, sessions, skills, terminal, transcripts

HERE = Path(__file__).parent
STATIC_DIR = HERE / "static"


# ---------- shared in-memory state ----------

class State:
    def __init__(self) -> None:
        self.last_snapshot: dict = {"windows": [], "counts": {}, "ts": 0}
        self.last_signature: tuple = ()
        self.subscribers: set[asyncio.Queue] = set()

    def diff_signature(self, snap: dict) -> tuple:
        # Tuple of (pid, status, waiting_for, updated_at) lets us tell whether
        # anything dashboard-visible has changed.
        return tuple(
            (w["pid"], w["status"], w["waiting_for"], w["updated_at"])
            for w in snap["windows"]
        )


state = State()


def _enriched_snapshot() -> dict:
    snap = sessions.snapshot()
    perm_by_tty = perms.pending_by_tty()
    for w in snap["windows"]:
        tty = w.get("tty")
        if tty and tty in perm_by_tty:
            ev = perm_by_tty[tty]
            w["permission_msg"] = ev.msg
            w["permission_ts"] = ev.raw_ts
        else:
            w["permission_msg"] = None
            w["permission_ts"] = None
        tp = w.get("transcript_path")
        if not w.get("name") and tp:
            from core.history import _extract_first_user_text
            first = _extract_first_user_text(Path(tp))
            if first:
                w["first_input"] = first[:100]
        if tp:
            w["current_task"] = transcripts.current_task_hint(tp)
        else:
            w["current_task"] = None
        tri = patrol.classify(w)
        w["triage"] = tri["triage"]
        w["triage_reason"] = tri["reason"]
        w["triage_suggestion"] = tri["suggestion"]
        if tp:
            w["skills_used"] = transcripts.extract_skills_used(tp)
            w["memory_ops"] = transcripts.extract_memory_ops(tp)
            w["background_tasks"] = transcripts.extract_background_tasks(tp)
        else:
            w["skills_used"] = []
            w["memory_ops"] = []
            w["background_tasks"] = []
    # Sort by triage priority (most urgent first), then by idle time.
    snap["windows"].sort(key=lambda w: (
        patrol.TRIAGE_PRIORITY.get(w.get("triage", ""), 99),
        -w.get("updated_at", 0),
    ))
    return snap


async def _watcher() -> None:
    """Poll sessions every 2s; broadcast deltas to SSE subscribers."""
    while True:
        try:
            snap = _enriched_snapshot()
            sig = state.diff_signature(snap)
            state.last_snapshot = snap
            if sig != state.last_signature:
                state.last_signature = sig
                payload = json.dumps(snap)
                dead: list[asyncio.Queue] = []
                for q in list(state.subscribers):
                    try:
                        q.put_nowait(payload)
                    except asyncio.QueueFull:
                        dead.append(q)
                for q in dead:
                    state.subscribers.discard(q)
        except Exception as e:
            print(f"[watcher] error: {e}")
        await asyncio.sleep(2)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(_watcher())
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(title="Claude Fleet", lifespan=lifespan)


# ---------- routes ----------

@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    html = (STATIC_DIR / "index.html").read_text()
    return HTMLResponse(html)


@app.get("/api/windows")
def api_windows() -> dict:
    if not state.last_snapshot["windows"]:
        state.last_snapshot = _enriched_snapshot()
    return state.last_snapshot


@app.get("/api/windows/{pid}/timeline")
def api_timeline(pid: int, limit: int = 2000) -> dict:
    w = sessions.find_window(pid)
    if not w:
        raise HTTPException(404, "window not found")
    tp = w.transcript_path or ""
    events = transcripts.timeline(tp, limit=limit) if tp else []
    return {
        "pid": pid,
        "session_id": w.session_id,
        "project_name": w.project_name,
        "events": events,
        "skills_used": transcripts.extract_skills_used(tp) if tp else [],
        "memory_ops": transcripts.extract_memory_ops(tp) if tp else [],
        "plan_history": transcripts.extract_plan_history(tp) if tp else [],
    }


@app.get("/api/windows/{pid}/plan")
def api_plan(pid: int) -> dict:
    w = sessions.find_window(pid)
    if not w:
        raise HTTPException(404, "window not found")
    plan = plans.plan_for_session(w.name, w.cwd, w.transcript_path)
    return {"pid": pid, "plan": plan}


@app.get("/api/search")
def api_search(q: str, limit: int = 60) -> dict:
    if not q.strip():
        return {"hits": [], "q": q}
    return {"hits": search.search(q, limit=limit), "q": q}


@app.get("/api/plans")
def api_plans() -> dict:
    return {"plans": plans.list_plans()}


@app.get("/api/plans/{name}")
def api_plan_by_name(name: str) -> dict:
    p = plans.read_plan_by_name(name)
    if not p:
        raise HTTPException(404, "plan not found")
    return p


@app.post("/api/windows/{pid}/focus")
def api_focus(pid: int) -> dict:
    w = sessions.find_window(pid)
    if not w:
        raise HTTPException(404, "window not found")
    if not w.tty:
        return {"ok": False, "error": "no tty available for this pid"}
    return terminal.focus(w.tty)


@app.post("/api/windows/{pid}/fork")
def api_fork(pid: int) -> dict:
    return actions.fork_session(pid)


@app.post("/api/windows/{pid}/close")
def api_close(pid: int) -> dict:
    return actions.close_session(pid)


@app.post("/api/windows/{pid}/review")
def api_review(pid: int) -> dict:
    return actions.review_session_start(pid)


@app.get("/api/windows/{pid}/review")
def api_review_result(pid: int) -> dict:
    return actions.review_session_result(pid)


@app.get("/api/history")
def api_history(q: str = "", page: int = 1, limit: int = 30) -> dict:
    return history.list_sessions(q=q or None, page=page, limit=limit)


@app.get("/api/history/{session_id}/timeline")
def api_history_timeline(session_id: str, limit: int = 2000) -> dict:
    # Claude Code transcripts
    from core.sessions import PROJECTS_DIR
    for proj_dir in PROJECTS_DIR.iterdir():
        if not proj_dir.is_dir():
            continue
        f = proj_dir / f"{session_id}.jsonl"
        if f.exists():
            fp = str(f)
            events = transcripts.timeline(fp, limit=limit)
            return {
                "session_id": session_id, "project_slug": proj_dir.name,
                "events": events, "platform": "claude",
                "skills_used": transcripts.extract_skills_used(fp),
                "memory_ops": transcripts.extract_memory_ops(fp),
                "plan_history": transcripts.extract_plan_history(fp),
            }
    # Codex transcripts
    from core.codex import CODEX_SESSIONS_DIR
    if CODEX_SESSIONS_DIR.exists():
        for f in CODEX_SESSIONS_DIR.rglob("*.jsonl"):
            if session_id in f.stem:
                events = codex.codex_timeline(str(f), limit=limit)
                return {"session_id": session_id, "project_slug": "codex", "events": events, "platform": "codex"}
    # OpenCode sessions (SQLite)
    try:
        from core.opencode import opencode_timeline
        events = opencode_timeline(session_id, limit=limit)
        if events:
            return {"session_id": session_id, "project_slug": "opencode", "events": events, "platform": "opencode"}
    except Exception:
        pass
    raise HTTPException(404, "transcript not found")


def _find_history_cwd(session_id: str) -> tuple[bool, str]:
    """Return (found, cwd) for a session_id from the history index."""
    data = history.list_sessions(limit=9999)
    for s in data["sessions"]:
        if s["session_id"] == session_id:
            return True, (s.get("project") or str(Path.home()))
    return False, ""


@app.post("/api/history/{session_id}/resume")
def api_history_resume(session_id: str) -> dict:
    import shlex
    # If the session is alive, focus it instead of opening a new window.
    for w in sessions.list_windows():
        if w.session_id == session_id and w.alive and w.tty:
            result = terminal.focus(w.tty)
            return {"ok": result.get("ok", False), "action": "focused",
                    "session_id": session_id, "pid": w.pid, "error": result.get("error"),
                    "fallback_cmd": result.get("fallback_cmd")}

    found, cwd = _find_history_cwd(session_id)
    if not found:
        return {"ok": False, "error": "session not found in index"}
    inner = f"cd {shlex.quote(cwd)} && claude --resume {shlex.quote(session_id)}"
    result = terminal.spawn_window(inner)
    return {"ok": result["ok"], "action": "resumed", "session_id": session_id,
            "cwd": cwd, "error": result.get("error"), "fallback_cmd": result.get("fallback_cmd")}


@app.post("/api/history/{session_id}/fork")
def api_history_fork(session_id: str) -> dict:
    import shlex
    found, cwd = _find_history_cwd(session_id)
    if not found:
        return {"ok": False, "error": "session not found in index"}
    inner = f"cd {shlex.quote(cwd)} && claude --resume {shlex.quote(session_id)} --fork-session"
    result = terminal.spawn_window(inner)
    return {"ok": result["ok"], "action": "forked", "session_id": session_id,
            "cwd": cwd, "error": result.get("error"), "fallback_cmd": result.get("fallback_cmd")}


@app.get("/api/skills/{name}/sessions")
def api_skill_sessions(name: str) -> dict:
    """Reverse lookup: which sessions touched this skill, with per-session counts."""
    data = history.list_sessions(limit=9999)
    rows = []
    for s in data["sessions"]:
        bd = s.get("skill_breakdown", {}) or {}
        inv = (bd.get("per_skill_invokes") or {}).get(name, 0)
        rd = (bd.get("per_skill_reads") or {}).get(name, 0)
        wr = (bd.get("per_skill_writes") or {}).get(name, 0)
        bash = (bd.get("per_skill_bash_refs") or {}).get(name, 0)
        total = inv + rd + wr + bash
        if total == 0:
            continue
        rows.append({
            "session_id": s["session_id"],
            "project_name": s["project_name"],
            "platform": s.get("platform", "claude"),
            "title": s.get("first_input", "")[:120],
            "ts": s.get("last_ts") or s.get("first_ts") or "",
            "invoke": inv,
            "reads": rd,
            "writes": wr,
            "bash_refs": bash,
            "total": total,
        })
    rows.sort(key=lambda r: -r["total"])
    return {"name": name, "sessions": rows, "session_count": len(rows)}


@app.get("/api/memory/{name}/sessions")
def api_memory_sessions(name: str) -> dict:
    """Reverse lookup: which sessions read/wrote this memory."""
    data = history.list_sessions(limit=9999)
    rows = []
    for s in data["sessions"]:
        bd = s.get("memory_breakdown", {}) or {}
        rd = (bd.get("per_memory_reads") or {}).get(name, 0)
        wr = (bd.get("per_memory_writes") or {}).get(name, 0)
        ed = (bd.get("per_memory_edits") or {}).get(name, 0)
        total = rd + wr + ed
        if total == 0:
            continue
        rows.append({
            "session_id": s["session_id"],
            "project_name": s["project_name"],
            "platform": s.get("platform", "claude"),
            "title": s.get("first_input", "")[:120],
            "ts": s.get("last_ts") or s.get("first_ts") or "",
            "reads": rd,
            "writes": wr,
            "edits": ed,
            "total": total,
        })
    rows.sort(key=lambda r: -r["total"])
    return {"name": name, "sessions": rows, "session_count": len(rows)}


@app.get("/api/memory/{name}")
def api_memory_detail(name: str) -> dict:
    from core.sessions import PROJECTS_DIR
    for proj_dir in PROJECTS_DIR.iterdir():
        mem_dir = proj_dir / "memory"
        if not mem_dir.is_dir():
            continue
        f = mem_dir / f"{name}.md"
        if f.exists():
            text = f.read_text(errors="replace")
            fm = memory._parse_frontmatter(text) if hasattr(memory, '_parse_frontmatter') else {}
            body_start = text.find("\n---", 3)
            body = text[body_start + 4:].strip() if body_start > 0 else text
            return {
                "name": fm.get("name", name),
                "description": fm.get("description", ""),
                "type": fm.get("type", "unknown"),
                "content": body,
                "path": str(f),
            }
    raise HTTPException(404, "memory not found")


@app.get("/api/skills")
def api_skills() -> dict:
    data = history.list_sessions(limit=9999)
    session_count: dict[str, int] = {}
    invoke_count: dict[str, int] = {}
    reads_count: dict[str, int] = {}
    writes_count: dict[str, int] = {}
    bash_refs_count: dict[str, int] = {}
    for s in data["sessions"]:
        for sk in s.get("skills_used", []):
            session_count[sk] = session_count.get(sk, 0) + 1
        # Use the per-session breakdown that history index already produced
        # (covers Claude + OpenCode + Codex uniformly).
        bd = s.get("skill_breakdown") or {}
        for sk, cnt in (bd.get("per_skill_invokes") or {}).items():
            invoke_count[sk] = invoke_count.get(sk, 0) + cnt
        for sk, cnt in (bd.get("per_skill_reads") or {}).items():
            reads_count[sk] = reads_count.get(sk, 0) + cnt
        for sk, cnt in (bd.get("per_skill_writes") or {}).items():
            writes_count[sk] = writes_count.get(sk, 0) + cnt
        for sk, cnt in (bd.get("per_skill_bash_refs") or {}).items():
            bash_refs_count[sk] = bash_refs_count.get(sk, 0) + cnt
    all_skills = skills.list_all_skills()
    for s in all_skills:
        name = s["name"]
        inv = invoke_count.get(name, 0)
        rd = reads_count.get(name, 0)
        wr = writes_count.get(name, 0)
        brefs = bash_refs_count.get(name, 0)
        s["session_count"] = session_count.get(name, 0)
        s["invoke_count"] = inv
        s["reads"] = rd
        s["writes"] = wr
        s["bash_refs"] = brefs
        s["total_activity"] = inv + rd + wr + brefs
    all_skills.sort(key=lambda s: (-s["total_activity"], -s["invoke_count"], s["name"]))
    return {"skills": all_skills}


@app.get("/api/memory")
def api_memory(project: str | None = None) -> dict:
    data = history.list_sessions(limit=9999)
    read_count: dict[str, int] = {}
    write_count: dict[str, int] = {}
    for s in data["sessions"]:
        for m in s.get("memory_ops", []):
            name = m["name"]
            if m["operation"] == "read":
                read_count[name] = read_count.get(name, 0) + 1
            else:
                write_count[name] = write_count.get(name, 0) + 1
    result = memory.list_memories(project_slug=project)
    for group_mems in result.get("groups", {}).values():
        for m in group_mems:
            stem = m.get("file_stem", m["name"])
            m["read_sessions"] = read_count.get(stem, 0)
            m["write_sessions"] = write_count.get(stem, 0)
    return result


@app.get("/api/perms")
def api_perms() -> dict:
    return perms.snapshot()


@app.get("/api/events")
async def api_events(request: Request) -> EventSourceResponse:
    queue: asyncio.Queue = asyncio.Queue(maxsize=32)
    state.subscribers.add(queue)

    async def event_gen():
        # Send the current snapshot once immediately.
        snap = state.last_snapshot or _enriched_snapshot()
        yield {"event": "snapshot", "data": json.dumps(snap)}
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=20.0)
                    yield {"event": "snapshot", "data": payload}
                except asyncio.TimeoutError:
                    yield {"event": "heartbeat", "data": str(int(time.time()))}
        finally:
            state.subscribers.discard(queue)

    return EventSourceResponse(event_gen())
