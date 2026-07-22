"""Claude Fleet — FastAPI app: dashboard backend + SSE."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

from core import actions, codex, history, insights, memory, notify, patrol, perms, plans, remote, search, sessions, share, skills, terminal, transcripts

HERE = Path(__file__).parent
STATIC_DIR = HERE / "static"
SHARE_DIR = STATIC_DIR / "share"


# ---------- shared in-memory state ----------

class State:
    def __init__(self) -> None:
        self.last_snapshot: dict = {"windows": [], "counts": {}, "ts": 0}
        self.last_signature: tuple = ()
        self.subscribers: set[asyncio.Queue] = set()

    def diff_signature(self, snap: dict) -> tuple:
        # Tuple of (pid, status, waiting_for, updated_at, idle_bucket) lets us tell
        # whether anything dashboard-visible has changed. The 30s idle bucket makes
        # idle cards (esp. codex, whose updated_at = transcript mtime never moves
        # while idle) keep refreshing their "Xm ago" without per-second churn.
        return tuple(
            (w["pid"], w["status"], w["waiting_for"], w["updated_at"],
             w.get("triage"), bool(w.get("permission_msg")),
             (w.get("idle_seconds") or 0) // 30)
            for w in snap["windows"]
        )


state = State()


# session_id -> first user message (immutable per session; avoids a per-poll read).
_first_input_cache: dict[str, str] = {}

# Serializes snapshot building: the watcher runs it in a threadpool while
# api_windows / the SSE initial-send may call it on the request thread; the shared
# enrichment caches aren't safe under concurrent build, so only one runs at a time.
_snapshot_lock = threading.Lock()


def _enriched_snapshot() -> dict:
    with _snapshot_lock:
        return _build_enriched_snapshot()


def _build_enriched_snapshot() -> dict:
    snap = sessions.snapshot()
    # Fresh "needs approval" events only (not stale, not "waiting for input").
    perm_by_tty = perms.pending_by_tty()
    for w in snap["windows"]:
        w["platform"] = "claude"
        tty = w.get("tty")
        ev = perm_by_tty.get(tty) if tty else None
        # BLOCKED only if the session is genuinely parked at the prompt: no activity
        # AFTER the approval event. Measure activity by the TRANSCRIPT's mtime, not
        # session.json's `updatedAt` — while Claude is crunching, it keeps appending
        # to the transcript but `updatedAt` lags (often by many minutes), which made
        # a busy session look permanently stuck at an old permission prompt.
        blocked = False
        if ev:
            activity = (w.get("updated_at", 0) or 0) / 1000
            _tp = w.get("transcript_path")
            if _tp:
                try:
                    activity = max(activity, os.path.getmtime(_tp))
                except OSError:
                    pass
            # Approve → transcript advances past the prompt (activity > ev.epoch);
            # a real stall leaves activity at/before the prompt time.
            blocked = ev.epoch >= activity - 5
        if blocked:
            w["permission_msg"] = ev.msg
            w["permission_ts"] = ev.raw_ts
        else:
            w["permission_msg"] = None
            w["permission_ts"] = None
        tp = w.get("transcript_path")
        if not w.get("name") and tp:
            # First user message never changes for a session — cache it by id so
            # nameless windows don't re-read the transcript every 2s tick.
            sid = w.get("session_id", "")
            if sid not in _first_input_cache:
                from core.history import _extract_first_user_text
                _first_input_cache[sid] = (_extract_first_user_text(Path(tp)) or "")[:100]
            if _first_input_cache[sid]:
                w["first_input"] = _first_input_cache[sid]
        # One cached bundle (by transcript mtime) instead of four uncached
        # whole-file scans per window per 2s tick.
        if tp:
            enr = transcripts.window_enrichment(tp)
            w["current_task"] = enr["current_task"]
            w["skills_used"] = enr["skills_used"]
            w["memory_ops"] = enr["memory_ops"]
            w["background_tasks"] = enr["background_tasks"]
        else:
            w["current_task"] = None
            w["skills_used"] = []
            w["memory_ops"] = []
            w["background_tasks"] = []
        if blocked:
            # Claude Code's session.json never writes status=="waiting", so the
            # focus-log approval event is the real signal — surface it as the red
            # alert instead of mislabeling a blocked session "completed".
            w["triage"] = "waiting_perm"
            w["triage_reason"] = ev.msg
            w["triage_suggestion"] = "去终端批准"
        else:
            tri = patrol.classify(w)
            w["triage"] = tri["triage"]
            w["triage_reason"] = tri["reason"]
            w["triage_suggestion"] = tri["suggestion"]
    # Codex writes no pid registry, so running codex tasks are invisible to
    # sessions.list_windows(). Detect them separately and render as live cards.
    # Their transcripts are a different format, so skip the Claude-specific
    # enrichment (perms/triage/skills) and supply neutral defaults.
    try:
        for cw in codex.list_codex_windows():
            idle = cw.get("idle_seconds") or 0
            cw.setdefault("permission_msg", None)
            cw.setdefault("permission_ts", None)
            tp = cw.get("transcript_path")
            cw["current_task"] = codex.codex_current_task(tp) if tp else None
            # Honest state from transcript ACTIVITY, not the open fd: codex keeps its
            # rollout open the whole time it's alive, so "active" (lsof) was always
            # true → every codex card was permanently "working". Use mtime instead —
            # codex appends events as it streams, so a recently-written transcript is
            # genuinely generating; a stale one is an open terminal sitting idle.
            if idle < codex.CODEX_WORKING_IDLE_SEC:
                cw["triage"] = "working"
                cw["triage_reason"] = "codex · active (recent output)"
            else:
                cw["triage"] = "idle"
                cw["triage_reason"] = "codex · open (idle)"
            cw["triage_suggestion"] = ""
            cw.setdefault("skills_used", [])
            cw.setdefault("memory_ops", [])
            cw.setdefault("background_tasks", [])
            snap["windows"].append(cw)
    except Exception as e:
        print(f"[snapshot] codex windows error: {e}")
    # Registered remote servers (collected over SSH by the slow poller, cached).
    try:
        snap["windows"].extend(remote.cached_windows())
    except Exception as e:
        print(f"[snapshot] remote windows error: {e}")
    # The live board is for focusable terminal sessions. Headless/detached LOCAL
    # sessions (no controlling tty) can't be focused and just clutter the board;
    # they stay in History. Remote windows have no LOCAL tty but are legit (you
    # Resume them over SSH), so they're exempt from the tty filter.
    headless = sum(1 for w in snap["windows"]
                   if not w.get("tty") and w.get("source", "local") == "local")
    snap["windows"] = [w for w in snap["windows"]
                       if w.get("tty") or w.get("source", "local") != "local"]
    snap["counts"] = {
        "total": len(snap["windows"]),
        "busy": sum(1 for w in snap["windows"] if w.get("status") == "busy"),
        "waiting": sum(1 for w in snap["windows"] if w.get("status") == "waiting"),
        "idle": sum(1 for w in snap["windows"]
                    if w.get("status") not in ("busy", "waiting")),
        "headless": headless,
    }
    # Keep the per-window caches bounded to the transcripts that are actually live.
    live_tps = [w.get("transcript_path") for w in snap["windows"]]
    transcripts.prune_window_enrich_cache(live_tps)
    patrol.prune_last_info_cache(live_tps)
    # Sort by triage priority (most urgent first), then by idle time.
    snap["windows"].sort(key=lambda w: (
        patrol.TRIAGE_PRIORITY.get(w.get("triage", ""), 99),
        -w.get("updated_at", 0),
    ))
    return snap


async def _watcher() -> None:
    """Poll sessions every 2s; broadcast deltas to SSE subscribers.

    `_enriched_snapshot` is synchronous, disk-bound work; run it in a thread so a
    slow/cold poll never blocks the event loop (and the SSE/HTTP handlers on it).
    """
    loop = asyncio.get_event_loop()
    while True:
        try:
            snap = await loop.run_in_executor(None, _enriched_snapshot)
            sig = state.diff_signature(snap)
            state.last_snapshot = snap
            # Desktop notify on edges into waiting_perm / completed (macOS, opt-out
            # via FLEET_NOTIFY=0). Reaches you even with no browser tab open.
            try:
                notify.notify_transitions(snap["windows"])
            except Exception as e:
                print(f"[notify] error: {e}")
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


async def _remote_poller() -> None:
    """Refresh registered remotes over SSH on a slow cadence (SSH is too costly for
    the 2s watcher). Runs in a thread so blocking SSH never stalls the event loop."""
    loop = asyncio.get_event_loop()
    while True:
        try:
            if remote.load_remotes():
                await loop.run_in_executor(None, remote.poll_all)
        except Exception as e:
            print(f"[remote-poller] error: {e}")
        await asyncio.sleep(25)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(_watcher())
    rtask = asyncio.create_task(_remote_poller())
    try:
        yield
    finally:
        task.cancel()
        rtask.cancel()


app = FastAPI(title="Claude Fleet", lifespan=lifespan)


# ---------- routes ----------

@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    html = (STATIC_DIR / "index.html").read_text()
    # Never let the browser serve a stale dashboard — always fetch the latest JS,
    # so a refresh actually picks up new behavior (and the live-update logic).
    return HTMLResponse(html, headers={"Cache-Control": "no-store, must-revalidate"})


@app.get("/api/windows")
def api_windows() -> dict:
    if not state.last_snapshot["windows"]:
        state.last_snapshot = _enriched_snapshot()
    return state.last_snapshot


def _snapshot_window(pid: int) -> Optional[dict]:
    """Find a live window dict by pid in the last snapshot — covers codex windows,
    which aren't in sessions.find_window (Claude registry only)."""
    for cw in state.last_snapshot.get("windows", []):
        if cw.get("pid") == pid:
            return cw
    return None


@app.get("/api/windows/{pid}/timeline")
def api_timeline(pid: int, limit: int = 2000) -> dict:
    w = sessions.find_window(pid)
    if not w:
        # Codex live window — has no Claude registry entry; serve its codex timeline.
        cw = _snapshot_window(pid)
        if cw and cw.get("platform") == "codex":
            tp = cw.get("transcript_path") or ""
            return {
                "pid": pid,
                "session_id": cw.get("session_id", ""),
                "project_name": cw.get("project_name", ""),
                "platform": "codex",
                "events": codex.codex_timeline(tp, limit=limit) if tp else [],
                "skills_used": cw.get("skills_used", []),
                "memory_ops": [],
                "plan_history": [],
            }
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
    tty = w.tty if w else None
    if not w:
        # Codex live cards aren't in the Claude window list — look them up live
        # (not via the possibly-stale snapshot) and read their tty.
        try:
            for cw in codex.list_codex_windows():
                if cw.get("pid") == pid:
                    tty = cw.get("tty")
                    break
        except Exception:
            pass
        if tty is None and not any(
            cw.get("pid") == pid for cw in state.last_snapshot.get("windows", [])
        ):
            # Return structured JSON (not a 404) so the UI shows a real message.
            return {"ok": False, "error": "window not found — it may have exited"}
    if not tty:
        return {"ok": False, "error": (
            "this session has no terminal to focus — it's running headless "
            "(IDE/SDK). Use Timeline or Fork instead.")}
    return terminal.focus(tty)


@app.post("/api/windows/{pid}/fork")
def api_fork(pid: int) -> dict:
    cw = _snapshot_window(pid)
    if cw and cw.get("platform") == "codex":
        return terminal.launch_session(
            "codex", cw.get("session_id", ""), cw.get("cwd", ""), fork=True)
    return actions.fork_session(pid)


@app.post("/api/windows/{pid}/close")
def api_close(pid: int) -> dict:
    cw = _snapshot_window(pid)
    if cw and cw.get("platform") == "codex":
        return {"ok": False, "error": "codex sessions can't be closed from the dashboard"}
    return actions.close_session(pid)


@app.post("/api/windows/{pid}/review")
def api_review(pid: int) -> dict:
    cw = _snapshot_window(pid)
    if cw and cw.get("platform") == "codex":
        return {"ok": False, "error": "background review is Claude-only"}
    return actions.review_session_start(pid)


@app.get("/api/windows/{pid}/review")
def api_review_result(pid: int) -> dict:
    return actions.review_session_result(pid)


@app.get("/api/insights")
def api_insights() -> dict:
    """Cross-session mining: token/cost totals, model/project/day breakdowns,
    tool histogram, activity heatmap, leaderboards, hot files."""
    data = history.list_sessions(limit=99999)
    return insights.build_insights(data["sessions"])


@app.get("/api/forks")
def api_forks() -> dict:
    """Fork lineage: family trees of sessions, ranked so the most-reused 'mother'
    sessions (most descendants) surface first."""
    data = history.list_sessions(limit=99999)
    return insights.fork_forest(data["sessions"])


@app.get("/api/remotes")
def api_remotes() -> dict:
    """Registered remote servers + their last-poll status."""
    return {"remotes": remote.status()}


@app.post("/api/remotes")
async def api_remotes_add(request: Request) -> dict:
    body = await request.json()
    name = (body.get("name") or "").strip()
    ssh = (body.get("ssh") or "").strip()
    if not name or not ssh:
        return {"ok": False, "error": "name and ssh are both required"}
    remote.add_remote(name, ssh)
    # Kick an immediate collect so the UI shows it without waiting for the poller.
    try:
        await asyncio.get_event_loop().run_in_executor(None, remote.poll_all)
    except Exception:
        pass
    return {"ok": True, "remotes": remote.status()}


@app.delete("/api/remotes/{name}")
def api_remotes_remove(name: str) -> dict:
    remote.remove_remote(name)
    return {"ok": True, "remotes": remote.status()}


@app.get("/api/history")
def api_history(q: str = "", page: int = 1, limit: int = 30,
                platforms: str = "", skills: str = "", sort: str = "recency") -> dict:
    data = history.list_sessions(
        q=q or None, page=page, limit=limit, sort=sort,
        platforms=[p for p in platforms.split(",") if p] or None,
        skills=[s for s in skills.split(",") if s] or None,
    )
    # The per-request ledger is internal (used only for Insights dedup) — drop it
    # from the row payload to keep it small.
    for s in data.get("sessions", []):
        if isinstance(s.get("metrics"), dict):
            s["metrics"].pop("requests", None)
    return data


@app.get("/api/history/{session_id}/timeline")
def api_history_timeline(session_id: str, limit: int = 2000, source: str = "") -> dict:
    # Remote session — its transcript lives on the registered server; SSH-cat it.
    # Resolve first (before local FS scans) so a session_id collision with a local
    # session can't shadow the remote one.
    if source and source != "local":
        rsess = _find_history_session(session_id, source)
        if rsess and rsess.get("transcript_path"):
            try:
                tl = remote.remote_timeline(source, rsess["transcript_path"],
                                            rsess.get("platform", "codex"), limit=limit)
                return {"session_id": session_id, "project_slug": source,
                        "events": tl["events"], "plan_history": tl["plan_history"],
                        "platform": rsess.get("platform", "codex")}
            except Exception as e:
                raise HTTPException(502, f"remote transcript fetch failed: {e}")
        raise HTTPException(404, "remote transcript not found")
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
                return {"session_id": session_id, "project_slug": "codex",
                        "events": events, "platform": "codex",
                        "plan_history": codex.codex_plan_history(str(f))}
    # OpenCode sessions (SQLite)
    try:
        from core.opencode import opencode_timeline
        events = opencode_timeline(session_id, limit=limit)
        if events:
            return {"session_id": session_id, "project_slug": "opencode", "events": events, "platform": "opencode"}
    except Exception:
        pass
    # Remote session — its transcript lives on the registered server; SSH-cat it.
    rsess = _find_history_session(session_id)
    if rsess and rsess.get("source", "local") != "local" and rsess.get("transcript_path"):
        try:
            tl = remote.remote_timeline(rsess["source"], rsess["transcript_path"],
                                        rsess.get("platform", "codex"), limit=limit)
            return {"session_id": session_id, "project_slug": rsess["source"],
                    "events": tl["events"], "plan_history": tl["plan_history"],
                    "platform": rsess.get("platform", "codex")}
        except Exception as e:
            raise HTTPException(502, f"remote transcript fetch failed: {e}")
    raise HTTPException(404, "transcript not found")


def _find_history_session(session_id: str, source: str = "") -> Optional[dict]:
    """Return the indexed session dict for a session_id (optionally a specific
    source — local vs a remote server), or None."""
    data = history.list_sessions(limit=9999)
    for s in data["sessions"]:
        if s["session_id"] == session_id and (not source or s.get("source", "local") == source):
            return s
    return None


def _resume_or_fork(session_id: str, source: str, fork: bool) -> dict:
    sess = _find_history_session(session_id, source)
    if not sess:
        return {"ok": False, "error": "session not found in index"}
    platform = sess.get("platform", "claude")
    cwd = sess.get("project") or str(Path.home())
    src = sess.get("source", "local")
    if src != "local":
        # Remote: open a local terminal that SSHes in and resumes there.
        ssh = remote.ssh_for(src)
        if not ssh:
            return {"ok": False, "error": f"remote '{src}' is not registered"}
        return terminal.launch_session(platform, session_id, cwd, fork=fork, ssh=ssh,
                                       env_path=remote.resume_path(src))
    if cwd and not Path(cwd).is_dir():
        return {"ok": False, "error": (
            f"project directory not found: {cwd} — restore it or delete the session")}
    # If a live Claude session owns a tty, focus it instead of opening a duplicate.
    if not fork and platform == "claude":
        for w in sessions.list_windows():
            if w.session_id == session_id and w.alive and w.tty:
                result = terminal.focus(w.tty)
                if result.get("ok"):
                    return {"ok": True, "action": "focused",
                            "session_id": session_id, "pid": w.pid}
                break
    return terminal.launch_session(platform, session_id, cwd, fork=fork)


@app.post("/api/history/{session_id}/resume")
def api_history_resume(session_id: str, source: str = "") -> dict:
    return _resume_or_fork(session_id, source, fork=False)


@app.post("/api/history/{session_id}/fork")
def api_history_fork(session_id: str, source: str = "") -> dict:
    return _resume_or_fork(session_id, source, fork=True)


@app.post("/api/history/{session_id}/fork-at-node")
def api_history_fork_at_node(session_id: str, uuid: str = "") -> dict:
    """Fork a Claude session truncated at a timeline node (issue #3)."""
    if not uuid:
        return {"ok": False, "error": "missing node uuid"}
    sess = _find_history_session(session_id)
    if not sess:
        return {"ok": False, "error": "session not found in index"}
    if sess.get("platform", "claude") != "claude":
        return {"ok": False, "error": "fork-at-node is only supported for Claude sessions"}
    cwd = sess.get("project") or str(Path.home())
    return actions.fork_session_at_node(session_id, uuid, cwd)


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


@app.post("/api/history/{session_id}/share")
def api_history_share(session_id: str, redact: bool = True) -> dict:
    """Render a read-only shareable HTML page for a session (issue #4)."""
    try:
        title, page = share.render_session_html(session_id, redact=redact)
    except FileNotFoundError as e:
        return {"ok": False, "error": str(e)}
    # short, non-enumerable id derived from session + redaction mode
    share_id = hashlib.sha1(f"{session_id}:{redact}".encode()).hexdigest()[:16]
    SHARE_DIR.mkdir(parents=True, exist_ok=True)
    (SHARE_DIR / f"{share_id}.html").write_text(page, encoding="utf-8")
    return {"ok": True, "share_url": f"/share/{share_id}", "title": title, "redacted": redact}


@app.get("/share/{share_id}")
def view_share(share_id: str) -> HTMLResponse:
    """Serve a previously generated read-only share page."""
    if not share_id.isalnum():
        raise HTTPException(404, "not found")
    f = SHARE_DIR / f"{share_id}.html"
    if not f.exists():
        raise HTTPException(404, "share not found")
    return HTMLResponse(f.read_text(encoding="utf-8"))


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
                    payload = await asyncio.wait_for(queue.get(), timeout=10.0)
                    yield {"event": "snapshot", "data": payload}
                except asyncio.TimeoutError:
                    # Liveness ping so the client can tell a quiet (no-change)
                    # stream apart from a dead one and reconnect on real stalls.
                    yield {"event": "heartbeat", "data": str(int(time.time()))}
        finally:
            state.subscribers.discard(queue)

    return EventSourceResponse(event_gen())
