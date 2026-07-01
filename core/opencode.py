"""Parse OpenCode sessions from SQLite DB at ~/.local/share/opencode/opencode.db."""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Optional

from .sessions import HOME_BASE

_SEARCH_CAP = 20_000   # full searchable body per event (matches transcripts.SEARCH_CAP)


def _search_cap(s: str) -> str:
    s = s or ""
    return s if len(s) <= _SEARCH_CAP else s[:_SEARCH_CAP]


def _opencode_db() -> Path:
    """OpenCode SQLite path. XDG-style on POSIX, %LOCALAPPDATA% on Windows."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base) / "opencode" / "opencode.db"
    return HOME_BASE / ".local/share/opencode/opencode.db"


OPENCODE_DB = _opencode_db()


def _get_conn() -> Optional[sqlite3.Connection]:
    if not OPENCODE_DB.exists():
        return None
    try:
        return sqlite3.connect(str(OPENCODE_DB), timeout=3)
    except Exception:
        return None


def list_opencode_sessions() -> list[dict]:
    conn = _get_conn()
    if not conn:
        return []
    try:
        cur = conn.execute("""
            SELECT s.id, s.title, s.directory, s.time_created, s.time_updated,
                   (SELECT substr(json_extract(p.data, '$.text'), 1, 300)
                    FROM part p JOIN message m ON p.message_id = m.id
                    WHERE m.session_id = s.id
                      AND json_extract(m.data, '$.role') = 'user'
                      AND json_extract(p.data, '$.type') = 'text'
                    ORDER BY p.time_created ASC LIMIT 1
                   ) as first_input,
                   (SELECT json_extract(m2.data, '$.model.providerID') || '/' || json_extract(m2.data, '$.model.modelID')
                    FROM message m2
                    WHERE m2.session_id = s.id AND json_extract(m2.data, '$.model') IS NOT NULL
                    ORDER BY m2.time_created DESC LIMIT 1
                   ) as model
            FROM session s
            ORDER BY s.time_updated DESC
        """)
        rows = cur.fetchall()
    except Exception:
        return []
    finally:
        conn.close()

    sessions = []
    for row in rows:
        sid, title, directory, created, updated, first_input, model = row
        project_name = directory.rsplit("/", 1)[-1] if directory else "opencode"
        activity = extract_opencode_session_activity(sid)
        sessions.append({
            "session_id": sid,
            "project": directory or "",
            "project_name": project_name,
            "first_input": (first_input or title or "")[:300],
            "input_count": 0,
            "first_ts": _ms_to_iso(created),
            "last_ts": _ms_to_iso(updated),
            "transcript_path": None,
            "transcript_size": 0,
            "transcript_mtime": updated or 0,
            "is_alive": False,
            "platform": "opencode",
            "model": model or "",
            "skills_used": activity["skills_used"],
            "memory_ops": activity["memory_ops"],
            "skill_breakdown": activity.get("skill_activity", {}),
            "memory_breakdown": activity.get("memory_activity", {}),
        })
    return sessions


def opencode_timeline(session_id: str, limit: int = 2000) -> list[dict]:
    conn = _get_conn()
    if not conn:
        return []
    try:
        cur = conn.execute("""
            SELECT m.id, m.time_created, json_extract(m.data, '$.role') as role,
                   p.data as part_data, p.time_created as part_time
            FROM message m
            JOIN part p ON p.message_id = m.id
            WHERE m.session_id = ?
            ORDER BY p.time_created ASC
        """, (session_id,))
        rows = cur.fetchall()
    except Exception:
        return []
    finally:
        conn.close()

    events: list[dict] = []
    for msg_id, msg_time, role, part_data_str, part_time in rows:
        try:
            pd = json.loads(part_data_str)
        except Exception:
            continue
        ptype = pd.get("type", "")
        ts = _ms_to_iso(part_time)

        if ptype == "text":
            text = pd.get("text", "")
            if not text.strip():
                continue
            kind = "user_text" if role == "user" else "assistant_text"
            events.append({"ts": ts, "kind": kind, "text": text[:4000], "tool": None, "role": role or "assistant",
                           "extra": {}, "search_text": _search_cap(text)})

        elif ptype == "tool":
            tool_name = pd.get("tool", "")
            state = pd.get("state") or {}
            inp = state.get("input") or {}
            status = state.get("status", "")
            try:
                inp_full = json.dumps(inp, ensure_ascii=False)
            except Exception:
                inp_full = str(inp)
            if status == "completed" and state.get("output"):
                events.append({"ts": ts, "kind": "tool_use", "text": "", "tool": tool_name, "role": "assistant",
                               "extra": _tool_preview(tool_name, inp), "search_text": _search_cap(inp_full)})
                out_full = state.get("output") or ""
                events.append({"ts": ts, "kind": "tool_result", "text": out_full[:200], "tool": None,
                               "role": "user", "extra": {}, "search_text": _search_cap(out_full)})
            elif status == "running" or not state.get("output"):
                events.append({"ts": ts, "kind": "tool_use", "text": "", "tool": tool_name, "role": "assistant",
                               "extra": _tool_preview(tool_name, inp), "search_text": _search_cap(inp_full)})

        elif ptype == "reasoning":
            continue

    return events[-limit:]


def search_opencode(query: str) -> dict[str, list[str]]:
    """Search OpenCode parts for a query. Returns {session_id: [snippets]}."""
    conn = _get_conn()
    if not conn:
        return {}
    ql = f"%{query}%"
    try:
        cur = conn.execute("""
            SELECT p.session_id, substr(p.data, 1, 500)
            FROM part p
            WHERE p.data LIKE ?
            AND json_extract(p.data, '$.type') IN ('text', 'tool')
            LIMIT 100
        """, (ql,))
        rows = cur.fetchall()
    except Exception:
        return {}
    finally:
        conn.close()

    result: dict[str, list[str]] = {}
    for sid, data_str in rows:
        try:
            pd = json.loads(data_str)
        except Exception:
            continue
        text = ""
        if pd.get("type") == "text":
            text = pd.get("text", "")
        elif pd.get("type") == "tool":
            text = json.dumps((pd.get("state") or {}).get("input") or {})
        if query.lower() not in text.lower():
            continue
        idx = text.lower().find(query.lower())
        start = max(0, idx - 60)
        end = min(len(text), idx + len(query) + 60)
        snippet = text[start:end].replace("\n", " ")
        if start > 0:
            snippet = "…" + snippet
        if end < len(text):
            snippet += "…"
        if sid not in result:
            result[sid] = []
        if len(result[sid]) < 3:
            result[sid].append(snippet)
    return result


def extract_opencode_session_activity(session_id: str) -> dict:
    """Extract skill/memory activity for an OpenCode session.

    Returns {skills_used, memory_ops, skill_activity} matching
    Claude Code's format but adapted for OpenCode's tool naming.
    OpenCode tools: bash, read, write, edit, skill (all lowercase).
    File path field: filePath (not file_path).
    """
    import re
    conn = _get_conn()
    if not conn:
        return {"skills_used": [], "memory_ops": [], "skill_activity": {}}
    try:
        cur = conn.execute("""
            SELECT data FROM part
            WHERE session_id = ? AND json_extract(data, '$.type') = 'tool'
        """, (session_id,))
        rows = cur.fetchall()
    except Exception:
        return {"skills_used": [], "memory_ops": [], "skill_activity": {}}
    finally:
        conn.close()

    skill_invokes: dict[str, int] = {}
    skill_reads: dict[str, int] = {}
    skill_writes: dict[str, int] = {}
    skill_bash: dict[str, int] = {}
    memory_reads: dict[str, int] = {}
    memory_writes: dict[str, int] = {}
    memory_edits: dict[str, int] = {}
    memory_ops: list[dict] = []
    mem_seen: set[tuple[str, str]] = set()
    skill_re = re.compile(r'/\.claude/skills/([^/]+)/')

    for (data_str,) in rows:
        try:
            pd = json.loads(data_str)
        except:
            continue
        tool = pd.get("tool", "")
        state = pd.get("state") or {}
        inp = state.get("input") or {}
        fp = inp.get("filePath", "") or inp.get("file_path", "") or ""
        cmd = inp.get("command", "") or ""

        # Skill formal invocation (opencode has a 'skill' tool)
        if tool == "skill":
            name = inp.get("name", "")
            if name:
                skill_invokes[name] = skill_invokes.get(name, 0) + 1

        # File operations on skill files
        if tool in ("read", "write", "edit", "patch"):
            m = skill_re.search(fp)
            if m:
                sk = m.group(1)
                if tool == "read":
                    skill_reads[sk] = skill_reads.get(sk, 0) + 1
                else:
                    skill_writes[sk] = skill_writes.get(sk, 0) + 1

        # File operations on memory files
        if tool in ("read", "write", "edit", "patch") and "/memory/" in fp:
            mem_name = fp.rsplit("/", 1)[-1].replace(".md", "")
            if mem_name == "MEMORY":
                continue
            op = "read" if tool == "read" else tool
            if tool == "read":
                memory_reads[mem_name] = memory_reads.get(mem_name, 0) + 1
            elif tool == "write":
                memory_writes[mem_name] = memory_writes.get(mem_name, 0) + 1
            elif tool in ("edit", "patch"):
                memory_edits[mem_name] = memory_edits.get(mem_name, 0) + 1
            key = (mem_name, op)
            if key not in mem_seen:
                mem_seen.add(key)
                memory_ops.append({"name": mem_name, "operation": op})

        # Bash referencing skills
        if tool == "bash" and ("skills/" in cmd or "SKILL.md" in cmd):
            matches = skill_re.findall(cmd)
            if matches:
                for sk in set(matches):
                    skill_bash[sk] = skill_bash.get(sk, 0) + 1
            else:
                skill_bash["_general"] = skill_bash.get("_general", 0) + 1

    skills_used = list(set(list(skill_invokes.keys()) + list(skill_reads.keys()) + list(skill_writes.keys())))

    return {
        "skills_used": skills_used,
        "memory_ops": memory_ops,
        "skill_activity": {
            "per_skill_invokes": skill_invokes,
            "per_skill_reads": skill_reads,
            "per_skill_writes": skill_writes,
            "per_skill_bash_refs": skill_bash,
        },
        "memory_activity": {
            "per_memory_reads": memory_reads,
            "per_memory_writes": memory_writes,
            "per_memory_edits": memory_edits,
        },
    }


def _tool_preview(tool_name: str, inp: dict) -> dict:
    preview: dict = {}
    for k, v in list(inp.items())[:4]:
        if isinstance(v, str):
            preview[k] = v[:200]
        elif isinstance(v, (int, float, bool)) or v is None:
            preview[k] = v
    return preview


def _ms_to_iso(ms: Optional[int]) -> str:
    if not ms:
        return ""
    import datetime
    return datetime.datetime.fromtimestamp(ms / 1000, tz=datetime.timezone.utc).isoformat()
