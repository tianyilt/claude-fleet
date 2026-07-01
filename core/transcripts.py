"""Parse ~/.claude/projects/{slug}/{sessionId}.jsonl transcripts."""
from __future__ import annotations

import json
import uuid as _uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from .sessions import PROJECTS_DIR

# `text` stays a compact preview for display; `search_text` carries the FULL body
# so in-session search matches everything History's full-text search matches (plan
# docs, long tool output, thinking) — capped per event to bound the payload.
SEARCH_CAP = 20_000            # max search_text chars per event
TOTAL_SEARCH_CAP = 8_000_000   # max cumulative search_text bytes per timeline response
# Big enough to read a whole transcript (so early events stay searchable, not just
# the tail); beyond this we fall back to tailing to avoid loading a giant file.
FULL_READ_MAX_BYTES = 20_000_000


def _cap(s: str) -> str:
    s = s or ""
    return s if len(s) <= SEARCH_CAP else s[:SEARCH_CAP]


@dataclass
class TurnEvent:
    ts: str
    kind: str            # user_text | assistant_text | tool_use | tool_result | thinking | system
    text: str            # ≤ 4 KB display excerpt
    tool: Optional[str]  # name of tool when kind == tool_use
    role: str            # user | assistant | system
    extra: dict          # small structured payload (e.g. tool input keys)
    uuid: str = ""       # source JSONL line uuid (for fork-at-node); "" if unknown
    search_text: str = ""  # full searchable body (capped); "" ⇒ fall back to text


def _iter_lines(path: Path) -> Iterable[dict]:
    try:
        with path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except Exception:
                    continue
    except FileNotFoundError:
        return


def _tail_lines(path: Path, n: int) -> list[dict]:
    buf: deque[dict] = deque(maxlen=n)
    for d in _iter_lines(path):
        buf.append(d)
    return list(buf)


def transcript_cwd(path: str | Path) -> Optional[str]:
    """The real working directory a Claude session ran in.

    Claude transcripts record a `cwd` on most event records; the first one is the
    source of truth for resuming (`claude --resume` looks the session up under the
    project dir derived from cwd). Far more reliable than reversing the project-dir
    slug, which is lossy (`/`, `_`, `.` all collapse to `-`).
    """
    for d in _iter_lines(Path(path)):
        cwd = d.get("cwd")
        if cwd:
            return cwd
    return None


def _flatten_assistant(msg: dict) -> list[TurnEvent]:
    out: list[TurnEvent] = []
    content = msg.get("content") or []
    ts = msg.get("timestamp") or ""
    if isinstance(content, str):
        out.append(TurnEvent(ts, "assistant_text", content[:4000], None, "assistant", {},
                             search_text=_cap(content)))
        return out
    if not isinstance(content, list):
        return out
    for c in content:
        ct = c.get("type")
        if ct == "text":
            full = c.get("text") or ""
            out.append(TurnEvent(ts, "assistant_text", full[:4000], None, "assistant", {},
                                 search_text=_cap(full)))
        elif ct == "tool_use":
            inp = c.get("input") or {}
            tool_name = c.get("name", "")
            file_path = str(inp.get("file_path", ""))

            if tool_name == "Skill":
                skill_name = inp.get("skill", "")
                out.append(TurnEvent(
                    ts, "skill_invoke", "", skill_name, "assistant",
                    {"args": (inp.get("args") or "")[:200]},
                    search_text=_cap(str(inp.get("args") or "")),
                ))
            elif tool_name in ("Read", "Write", "Edit") and "/memory/" in file_path:
                mem_name = file_path.rsplit("/", 1)[-1].replace(".md", "")
                kind = "memory_write" if tool_name in ("Write", "Edit") else "memory_read"
                out.append(TurnEvent(
                    ts, kind, "", mem_name, "assistant",
                    {"operation": tool_name.lower(), "path": file_path},
                    search_text=_cap(str(inp.get("content") or inp.get("new_string") or "")),
                ))
            else:
                preview: dict = {}
                for k, v in (inp.items() if isinstance(inp, dict) else []):
                    if isinstance(v, str):
                        preview[k] = v[:200]
                    elif isinstance(v, (int, float, bool)) or v is None:
                        preview[k] = v
                    else:
                        preview[k] = f"<{type(v).__name__}>"
                    if len(preview) >= 6:
                        break
                # Full input carries e.g. a Write/Edit `content` (plan doc body) that
                # the 6-key/200-char preview drops — searchable, not displayed.
                try:
                    full_in = json.dumps(inp, ensure_ascii=False)
                except Exception:
                    full_in = str(inp)
                out.append(TurnEvent(ts, "tool_use", "", tool_name, "assistant", preview,
                                     search_text=_cap(full_in)))
        elif ct == "thinking":
            # Kept now (searchable): compact preview for display, full for search.
            full = c.get("thinking") or c.get("text") or ""
            if full.strip():
                out.append(TurnEvent(ts, "thinking", full[:2000], None, "assistant", {},
                                     search_text=_cap(full)))
    return out


def _flatten_user(msg: dict, outer: Optional[dict] = None) -> list[TurnEvent]:
    out: list[TurnEvent] = []
    content = msg.get("content") or []
    ts = msg.get("timestamp") or ""
    # Claude echoes the full tool result out-of-band under the record's top-level
    # `toolUseResult` (file.content / originalFile / stdout). `content` only holds a
    # trimmed view, so a needle can live only here — fold it into the result's search.
    extra_full = ""
    tur = (outer or {}).get("toolUseResult")
    if isinstance(tur, dict):
        f = tur.get("file") or {}
        parts = [f.get("content"), f.get("originalFile"), tur.get("stdout"),
                 tur.get("stderr"), tur.get("output")]
        extra_full = "\n".join(str(p) for p in parts if isinstance(p, str) and p)
    elif isinstance(tur, str):
        extra_full = tur
    if isinstance(content, str):
        out.append(TurnEvent(ts, "user_text", content[:4000], None, "user", {},
                             search_text=_cap(content)))
        return out
    if not isinstance(content, list):
        return out
    for c in content:
        ct = c.get("type")
        if ct == "text":
            full = c.get("text") or ""
            out.append(TurnEvent(ts, "user_text", full[:4000], None, "user", {},
                                 search_text=_cap(full)))
        elif ct == "tool_result":
            # Display stays a 200-char preview; search covers the full output.
            content_val = c.get("content")
            if isinstance(content_val, list):
                full = " ".join(x.get("text", "") for x in content_val if isinstance(x, dict))
            else:
                full = str(content_val)
            if extra_full and extra_full not in full:
                full = (full + "\n" + extra_full) if full else extra_full
            out.append(TurnEvent(ts, "tool_result", full[:200], None, "user", {},
                                 search_text=_cap(full)))
    return out


def _normalize(d: dict) -> list[TurnEvent]:
    t = d.get("type")
    msg = d.get("message") or {}
    # `timestamp` lives on the outer envelope, not inside `message`.
    if msg and "timestamp" not in msg and d.get("timestamp"):
        msg["timestamp"] = d.get("timestamp")
    if t == "assistant":
        events = _flatten_assistant(msg)
    elif t == "user":
        events = _flatten_user(msg, d)
    elif t in {"system", "permission-mode"}:
        events = [TurnEvent(
            d.get("timestamp", ""), "system",
            t + (": " + str(d.get("permissionMode", "")) if d.get("permissionMode") else ""),
            None, "system", {}
        )]
    else:
        return []
    # Tie every event back to its source JSONL line so the UI can fork at this node.
    src_uuid = d.get("uuid", "") or ""
    for ev in events:
        ev.uuid = src_uuid
    return events


def find_transcript_path(session_id: str) -> Optional[Path]:
    """Locate ~/.claude/projects/<slug>/<session_id>.jsonl by scanning project dirs."""
    if not PROJECTS_DIR.exists():
        return None
    for proj in PROJECTS_DIR.iterdir():
        if not proj.is_dir():
            continue
        f = proj / f"{session_id}.jsonl"
        if f.exists():
            return f
    return None


def fork_transcript_at(session_id: str, target_uuid: str) -> tuple[str, str]:
    """Copy a transcript up to (and including) the line with `target_uuid`, rewriting
    every line's sessionId to a fresh id. Returns (new_session_id, new_path).

    Resuming the new id (`claude --resume <new_sid>`) continues from that node.
    Raises FileNotFoundError if the session has no transcript, ValueError if the
    uuid isn't present.
    """
    original = find_transcript_path(session_id)
    if original is None:
        raise FileNotFoundError(f"no transcript for session {session_id}")
    new_sid = str(_uuid.uuid4())
    new_path = original.parent / f"{new_sid}.jsonl"
    found = False
    with original.open() as src, new_path.open("w") as dst:
        for line in src:
            s = line.strip()
            if not s:
                continue
            try:
                obj = json.loads(s)
            except Exception:
                dst.write(line if line.endswith("\n") else line + "\n")
                continue
            if obj.get("sessionId"):
                obj["sessionId"] = new_sid
            dst.write(json.dumps(obj, ensure_ascii=False) + "\n")
            if obj.get("uuid") == target_uuid:
                found = True
                break
    if not found:
        new_path.unlink(missing_ok=True)
        raise ValueError(f"uuid {target_uuid} not found in transcript {session_id}")
    return new_sid, str(new_path)


def timeline(path: str | Path, limit: int = 50) -> list[dict]:
    """Return ≤ limit most recent flattened turn events for a transcript.

    Each event carries a full `search_text` (capped) so in-session search matches
    everything History's full-text search does. To keep early events searchable we
    read the whole file when it's reasonably sized, tailing only huge ones. A
    cumulative search_text budget (newest-first) bounds the response payload.
    """
    p = Path(path)
    if not p.exists():
        return []
    try:
        small = p.stat().st_size <= FULL_READ_MAX_BYTES
    except OSError:
        small = False
    if small:
        raw = list(_iter_lines(p))
    else:
        # One jsonl row can expand into several events, so over-read the tail.
        raw = _tail_lines(p, max(limit * 2, 100))
    events: list[TurnEvent] = []
    for d in raw:
        events.extend(_normalize(d))
    events = events[-limit:]
    # Attach search_text newest-first until the budget is spent; older events then
    # fall back to their (already truncated) `text` for search.
    budget = TOTAL_SEARCH_CAP
    truncated = False
    for e in reversed(events):
        if e.search_text:
            if budget <= 0:
                e.search_text = ""
                truncated = True
            else:
                budget -= len(e.search_text)
    out = [e.__dict__ for e in events]
    if truncated and out:
        out[0]["search_truncated"] = True
    return out


def current_task_hint(path: str | Path) -> Optional[str]:
    """Best-effort one-liner of what this session is currently doing."""
    p = Path(path)
    if not p.exists():
        return None
    raw = _tail_lines(p, 30)
    # Walk back to the most informative event.
    for d in reversed(raw):
        for ev in reversed(_normalize(d)):
            if ev.kind == "tool_use" and ev.tool:
                key_args = ", ".join(f"{k}={v!r}" for k, v in list(ev.extra.items())[:2])
                return f"{ev.tool}({key_args})" if key_args else ev.tool
            if ev.kind == "assistant_text" and ev.text.strip():
                first = ev.text.strip().splitlines()[0]
                return first[:160]
            if ev.kind == "user_text" and ev.text.strip():
                first = ev.text.strip().splitlines()[0]
                return f"↳ {first[:160]}"
    return None


def extract_skills_used(path: str | Path) -> list[str]:
    """Extract unique skill names invoked via the Skill tool."""
    counts = count_skill_invocations(path)
    return list(counts.keys())


def count_skill_invocations(path: str | Path) -> dict[str, int]:
    """Count total invocations per skill (not deduplicated)."""
    activity = count_skill_activity(path)
    return activity.get("per_skill_invokes", {})


def count_skill_activity(path: str | Path) -> dict:
    """Count all skill-related activity: invocations + file ops + bash refs.

    Returns {
        per_skill_invokes: {name: count},
        per_skill_file_ops: {name: count},
        per_skill_bash_refs: {name: count},
        totals: {invoke, file_ops, bash_refs, total},
    }
    """
    import re
    p = Path(path)
    if not p.exists():
        return {"per_skill_invokes": {}, "per_skill_file_ops": {},
                "per_skill_reads": {}, "per_skill_writes": {},
                "per_skill_bash_refs": {}, "totals": {"invoke": 0, "file_ops": 0, "reads": 0, "writes": 0, "bash_refs": 0, "total": 0}}

    invokes: dict[str, int] = {}
    file_ops: dict[str, int] = {}
    skill_reads: dict[str, int] = {}
    skill_writes: dict[str, int] = {}
    bash_refs: dict[str, int] = {}
    skill_path_re = re.compile(r'/\.claude/skills/([^/]+)/')

    for d in _iter_lines(p):
        if d.get("type") != "assistant":
            continue
        content = (d.get("message") or {}).get("content", [])
        if not isinstance(content, list):
            continue
        for c in content:
            if not isinstance(c, dict) or c.get("type") != "tool_use":
                continue
            name = c.get("name", "")
            inp = c.get("input") or {}

            if name == "Skill":
                sk = inp.get("skill", "")
                if sk:
                    invokes[sk] = invokes.get(sk, 0) + 1

            elif name in ("Read", "Write", "Edit"):
                fp = str(inp.get("file_path", ""))
                m = skill_path_re.search(fp)
                if m:
                    sk = m.group(1)
                    file_ops[sk] = file_ops.get(sk, 0) + 1
                    if name == "Read":
                        skill_reads[sk] = skill_reads.get(sk, 0) + 1
                    else:
                        skill_writes[sk] = skill_writes.get(sk, 0) + 1

            elif name == "Bash":
                cmd = str(inp.get("command", ""))
                if "skills/" in cmd or "SKILL.md" in cmd:
                    matches = skill_path_re.findall(cmd)
                    if matches:
                        for sk in set(matches):
                            bash_refs[sk] = bash_refs.get(sk, 0) + 1
                    else:
                        bash_refs["_general"] = bash_refs.get("_general", 0) + 1

    ti = sum(invokes.values())
    tf = sum(file_ops.values())
    tr = sum(skill_reads.values())
    tw = sum(skill_writes.values())
    tb = sum(bash_refs.values())
    return {
        "per_skill_invokes": invokes,
        "per_skill_file_ops": file_ops,
        "per_skill_reads": skill_reads,
        "per_skill_writes": skill_writes,
        "per_skill_bash_refs": bash_refs,
        "totals": {"invoke": ti, "file_ops": tf, "reads": tr, "writes": tw, "bash_refs": tb, "total": ti + tf + tb},
    }


def count_memory_activity(path: str | Path) -> dict:
    """Count per-memory read/write/edit counts (not deduplicated)."""
    p = Path(path)
    if not p.exists():
        return {"per_memory_reads": {}, "per_memory_writes": {}, "per_memory_edits": {}}
    reads: dict[str, int] = {}
    writes: dict[str, int] = {}
    edits: dict[str, int] = {}
    for d in _iter_lines(p):
        if d.get("type") != "assistant":
            continue
        content = (d.get("message") or {}).get("content", [])
        if not isinstance(content, list):
            continue
        for c in content:
            if not isinstance(c, dict) or c.get("type") != "tool_use":
                continue
            tool_name = c.get("name", "")
            if tool_name not in ("Read", "Write", "Edit"):
                continue
            inp = c.get("input") or {}
            fp = str(inp.get("file_path", ""))
            if "/memory/" not in fp:
                continue
            mem_name = fp.rsplit("/", 1)[-1].replace(".md", "")
            if mem_name == "MEMORY":
                continue
            if tool_name == "Read":
                reads[mem_name] = reads.get(mem_name, 0) + 1
            elif tool_name == "Write":
                writes[mem_name] = writes.get(mem_name, 0) + 1
            elif tool_name == "Edit":
                edits[mem_name] = edits.get(mem_name, 0) + 1
    return {"per_memory_reads": reads, "per_memory_writes": writes, "per_memory_edits": edits}


def extract_memory_ops(path: str | Path) -> list[dict]:
    """Extract unique memory file operations: [{name, operation, content_preview?}]."""
    p = Path(path)
    if not p.exists():
        return []
    ops: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for d in _iter_lines(p):
        if d.get("type") != "assistant":
            continue
        content = (d.get("message") or {}).get("content", [])
        if not isinstance(content, list):
            continue
        for c in content:
            if not isinstance(c, dict) or c.get("type") != "tool_use":
                continue
            tool_name = c.get("name", "")
            if tool_name not in ("Read", "Write", "Edit"):
                continue
            inp = c.get("input") or {}
            file_path = str(inp.get("file_path", ""))
            if "/memory/" not in file_path:
                continue
            mem_name = file_path.rsplit("/", 1)[-1].replace(".md", "")
            if mem_name == "MEMORY":
                continue
            op = "read" if tool_name == "Read" else tool_name.lower()
            key = (mem_name, op)
            if key not in seen:
                seen.add(key)
                entry: dict = {"name": mem_name, "operation": op}
                if tool_name == "Write":
                    entry["content_preview"] = (inp.get("content") or "")[:300]
                elif tool_name == "Edit":
                    old = (inp.get("old_string") or "")[:100]
                    new = (inp.get("new_string") or "")[:100]
                    entry["content_preview"] = f"-{old}\n+{new}" if old else new[:200]
                ops.append(entry)
    return ops


def extract_background_tasks(path: str | Path) -> list[dict]:
    """Extract ACTIVE (unresolved) background Bash/Monitor tasks."""
    p = Path(path)
    if not p.exists():
        return []
    bg_by_id: dict[str, dict] = {}
    resolved_ids: set[str] = set()
    for d in _iter_lines(p):
        if d.get("type") == "assistant":
            for c in ((d.get("message") or {}).get("content") or []):
                if not isinstance(c, dict) or c.get("type") != "tool_use":
                    continue
                name = c.get("name", "")
                inp = c.get("input") or {}
                tid = c.get("id", "")
                if name == "Bash" and inp.get("run_in_background") and tid:
                    bg_by_id[tid] = {
                        "type": "bash_bg",
                        "description": (inp.get("description") or "")[:200],
                        "command": (inp.get("command") or "")[:200],
                    }
                elif name == "Monitor" and inp.get("persistent") and tid:
                    bg_by_id[tid] = {
                        "type": "monitor",
                        "description": (inp.get("description") or "")[:200],
                        "command": (inp.get("command") or "")[:200],
                    }
        elif d.get("type") == "user":
            for c in ((d.get("message") or {}).get("content") or []):
                if isinstance(c, dict) and c.get("type") == "tool_result":
                    resolved_ids.add(c.get("tool_use_id", ""))
    return [t for tid, t in bg_by_id.items() if tid not in resolved_ids]


def extract_plan_history(path: str | Path) -> list[dict]:
    """Extract chronological plan file mutations from a transcript.

    Returns [{ts, plan_file, operation, version_label, content, diff}].
    Write = full content snapshot. Edit = old_string/new_string diff.
    """
    p = Path(path)
    if not p.exists():
        return []
    history: list[dict] = []
    write_count: dict[str, int] = {}
    edit_count: dict[str, int] = {}
    for d in _iter_lines(p):
        if d.get("type") != "assistant":
            continue
        ts = ""
        msg = d.get("message") or {}
        if "timestamp" not in msg and d.get("timestamp"):
            ts = d["timestamp"]
        else:
            ts = msg.get("timestamp", "")
        content_list = msg.get("content", [])
        if not isinstance(content_list, list):
            continue
        for c in content_list:
            if not isinstance(c, dict) or c.get("type") != "tool_use":
                continue
            tool_name = c.get("name", "")
            if tool_name not in ("Write", "Edit"):
                continue
            inp = c.get("input") or {}
            fp = str(inp.get("file_path", ""))
            if "/.claude/plans/" not in fp or not fp.endswith(".md"):
                continue
            plan_name = fp.rsplit("/", 1)[-1]
            if tool_name == "Write":
                write_count[plan_name] = write_count.get(plan_name, 0) + 1
                edit_count[plan_name] = 0
                vn = write_count[plan_name]
                history.append({
                    "ts": ts,
                    "plan_file": plan_name,
                    "operation": "write",
                    "version_label": f"v{vn}",
                    "content": inp.get("content", ""),
                    "diff": None,
                    "uuid": d.get("uuid", ""),  # source line → jump / fork-at-node
                })
            elif tool_name == "Edit":
                vn = write_count.get(plan_name, 0)
                edit_count[plan_name] = edit_count.get(plan_name, 0) + 1
                en = edit_count[plan_name]
                old_s = inp.get("old_string", "")
                new_s = inp.get("new_string", "")
                history.append({
                    "ts": ts,
                    "plan_file": plan_name,
                    "operation": "edit",
                    "version_label": f"v{vn}.{en}",
                    "content": None,
                    "diff": {"old": old_s[:2000], "new": new_s[:2000]},
                    "uuid": d.get("uuid", ""),  # source line → jump / fork-at-node
                })
    return history


def plan_title(path: str | Path) -> Optional[str]:
    """The H1 heading of the latest plan-mode plan this session wrote, if any.

    A plan's `# Title` describes what the session set out to do far better than a
    raw first prompt (which is often a long paste or a vague "看下…"). Used to title
    History rows. Returns None when the session never wrote a ~/.claude/plans/*.md.
    """
    latest = None
    for d in _iter_lines(Path(path)):
        if d.get("type") != "assistant":
            continue
        for c in ((d.get("message") or {}).get("content") or []):
            if not isinstance(c, dict) or c.get("type") != "tool_use" or c.get("name") != "Write":
                continue
            inp = c.get("input") or {}
            fp = str(inp.get("file_path", ""))
            if "/.claude/plans/" in fp and fp.endswith(".md"):
                latest = inp.get("content", "")
    if not latest:
        return None
    for line in latest.splitlines():
        s = line.strip()
        if s.startswith("# "):
            return s[2:].strip()[:120]
    for line in latest.splitlines():          # fallback: first non-empty line
        if line.strip():
            return line.strip().lstrip("#").strip()[:120]
    return None


# --- live-window enrichment cache -------------------------------------------
# The 2s dashboard poll used to run current_task_hint + extract_skills_used +
# extract_memory_ops + extract_background_tasks on EVERY live window EVERY tick —
# four uncached whole-file scans per window. On a big, growing transcript that is
# tens of MB of JSON parsing every 2s, forever, just from having the dashboard
# open. Cache the bundle by (mtime, size): an idle window (transcript unchanged)
# does zero file reads; only an actively-written window re-scans, and only when
# it actually changes.
_window_enrich_cache: dict[str, tuple[int, int, dict]] = {}


def window_enrichment(path: str | Path) -> dict:
    """current_task / skills_used / memory_ops / background_tasks for a live
    window, cached by (mtime, size). Reused by the snapshot poll and timelines."""
    tp = str(path)
    p = Path(tp)
    try:
        st = p.stat()
    except OSError:
        return {"current_task": None, "skills_used": [],
                "memory_ops": [], "background_tasks": []}
    mtime, size = int(st.st_mtime * 1000), st.st_size
    cached = _window_enrich_cache.get(tp)
    if cached and cached[0] == mtime and cached[1] == size:
        return cached[2]
    bundle = {
        "current_task": current_task_hint(tp),
        "skills_used": extract_skills_used(tp),
        "memory_ops": extract_memory_ops(tp),
        "background_tasks": extract_background_tasks(tp),
    }
    _window_enrich_cache[tp] = (mtime, size, bundle)
    return bundle


def prune_window_enrich_cache(live_paths) -> None:
    """Drop cache entries for transcripts no longer live, so the cache stays the
    size of the (few) active windows rather than growing without bound."""
    keep = {str(p) for p in live_paths if p}
    for tp in list(_window_enrich_cache.keys()):
        if tp not in keep:
            _window_enrich_cache.pop(tp, None)
