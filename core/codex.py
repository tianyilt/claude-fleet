"""Parse ~/.codex/sessions/ into HistorySession-compatible objects + timeline."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Optional

from .sessions import HOME_BASE

CODEX_HOME = HOME_BASE / ".codex"
CODEX_SESSIONS_DIR = CODEX_HOME / "sessions"

# Match skill path like /.claude/skills/foo/ or /.codex/skills/foo/
# Stop at whitespace, quote, &&, ||, semicolons, or maxdepth/-flag args
_SKILL_PATH_RE = re.compile(r'/\.(?:claude|codex)/skills/([A-Za-z0-9_-]+)(?:/|\b)')
_MEMORY_PATH_RE = re.compile(r'/memory/([A-Za-z0-9_-]+)\.md')


@dataclass
class CodexSession:
    session_id: str
    project: str
    project_name: str
    first_input: str
    first_ts: str
    last_ts: str
    transcript_path: str
    transcript_size: int
    transcript_mtime: int
    cli_version: str
    model_provider: str
    model: str = ""
    skills_used: list = field(default_factory=list)
    memory_ops: list = field(default_factory=list)
    skill_breakdown: dict = field(default_factory=dict)

    def to_history_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "project": self.project,
            "project_name": self.project_name,
            "first_input": self.first_input,
            "input_count": 0,
            "first_ts": self.first_ts,
            "last_ts": self.last_ts,
            "transcript_path": self.transcript_path,
            "transcript_size": self.transcript_size,
            "transcript_mtime": self.transcript_mtime,
            "is_alive": False,
            "platform": "codex",
            "model": self.model,
            "skills_used": self.skills_used,
            "memory_ops": self.memory_ops,
            "skill_breakdown": self.skill_breakdown,
        }


def _parse_session_meta(path: Path) -> Optional[dict]:
    try:
        with path.open() as f:
            first_line = f.readline()
            d = json.loads(first_line)
            if d.get("type") != "session_meta":
                return None
            return d.get("payload") or {}
    except Exception:
        return None


def _extract_first_user_input(path: Path) -> str:
    """Try user input first, fall back to first assistant response text."""
    try:
        with path.open() as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("type") == "event_msg":
                    payload = d.get("payload") or {}
                    if payload.get("role") == "user":
                        content = payload.get("content")
                        if isinstance(content, str) and content.strip():
                            return content[:300]
                        if isinstance(content, list):
                            for c in content:
                                if isinstance(c, dict) and c.get("type") == "input_text":
                                    t = (c.get("text") or "").strip()
                                    if t:
                                        return t[:300]
                if d.get("type") == "response_item":
                    payload = d.get("payload") or {}
                    if payload.get("type") == "message":
                        for c in (payload.get("content") or []):
                            if isinstance(c, dict) and c.get("type") == "output_text":
                                t = (c.get("text") or "").strip()
                                if t:
                                    return t[:300]
    except Exception:
        pass
    return ""


def extract_codex_session_activity(path: Path | str) -> dict:
    """Codex has no file I/O tools — everything goes through exec_command.
    We must scan the command strings for skill/memory file references.
    """
    p = Path(path)
    if not p.exists():
        return {
            "skills_used": [], "memory_ops": [], "model": "",
            "skill_breakdown": {
                "per_skill_invokes": {}, "per_skill_reads": {},
                "per_skill_writes": {}, "per_skill_bash_refs": {},
            },
        }

    bash_refs: dict[str, int] = {}
    skill_reads: dict[str, int] = {}
    skill_writes: dict[str, int] = {}
    memory_ops_seen: set[tuple[str, str]] = set()
    memory_ops: list[dict] = []
    model = ""

    try:
        with p.open() as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                t = d.get("type", "")
                payload = d.get("payload") or {}

                if t == "turn_context":
                    m = payload.get("model", "")
                    if m:
                        model = m

                if t != "response_item":
                    continue
                if payload.get("type") != "function_call":
                    continue
                name = payload.get("name", "")
                if name != "exec_command":
                    continue

                args_str = payload.get("arguments", "")
                try:
                    args = json.loads(args_str) if isinstance(args_str, str) else args_str
                except Exception:
                    args = {}
                cmd = str(args.get("cmd", "") or args.get("command", ""))
                workdir = str(args.get("workdir", ""))
                # Codex sets workdir to skill dir, then runs cmd inside it.
                # Need to scan both for skill references.
                haystack = cmd + " " + workdir
                if not haystack.strip():
                    continue

                # Skill path mentions (in cmd OR workdir)
                skill_matches = set(_SKILL_PATH_RE.findall(haystack))
                if skill_matches:
                    write_kw = any(k in cmd for k in ("write_file", " > ", " >> ", "tee ", "echo ", "cat <<", "cp ", "mv ", "mkdir"))
                    for sk in skill_matches:
                        bash_refs[sk] = bash_refs.get(sk, 0) + 1
                        if write_kw:
                            skill_writes[sk] = skill_writes.get(sk, 0) + 1
                        else:
                            skill_reads[sk] = skill_reads.get(sk, 0) + 1

                # Memory path mentions
                mem_matches = _MEMORY_PATH_RE.findall(haystack)
                for mem_name in set(mem_matches):
                    if mem_name == "MEMORY":
                        continue
                    write_kw = any(k in cmd for k in (" > ", " >> ", "tee ", "echo ", "cat <<"))
                    op = "write" if write_kw else "read"
                    key = (mem_name, op)
                    if key not in memory_ops_seen:
                        memory_ops_seen.add(key)
                        memory_ops.append({"name": mem_name, "operation": op})
    except Exception:
        pass

    skills_used = list(set(list(skill_reads.keys()) + list(skill_writes.keys())))
    return {
        "skills_used": skills_used,
        "memory_ops": memory_ops,
        "model": model,
        "skill_breakdown": {
            "per_skill_invokes": {},
            "per_skill_reads": skill_reads,
            "per_skill_writes": skill_writes,
            "per_skill_bash_refs": bash_refs,
        },
    }


# Codex transcripts are immutable once written; parsing each one fully (meta +
# activity scan) dominates list_codex_sessions(). Cache by (path, mtime, size)
# so a rebuild only re-parses files that changed.
_codex_cache: dict[str, tuple[int, int, "CodexSession"]] = {}


def list_codex_sessions() -> list[CodexSession]:
    if not CODEX_SESSIONS_DIR.exists():
        return []
    sessions: list[CodexSession] = []
    for f in CODEX_SESSIONS_DIR.rglob("*.jsonl"):
        try:
            st = f.stat()
        except Exception:
            continue
        key = str(f)
        cached = _codex_cache.get(key)
        if cached and cached[0] == int(st.st_mtime * 1000) and cached[1] == st.st_size:
            sessions.append(cached[2])
            continue
        meta = _parse_session_meta(f)
        if not meta:
            continue
        cwd = meta.get("cwd", "")
        activity = extract_codex_session_activity(f)
        cs = CodexSession(
            session_id=meta.get("id", f.stem),
            project=cwd,
            project_name=cwd.rsplit("/", 1)[-1] if cwd else f.stem,
            first_input=_extract_first_user_input(f),
            first_ts=meta.get("timestamp", ""),
            last_ts=meta.get("timestamp", ""),
            transcript_path=str(f),
            transcript_size=st.st_size,
            transcript_mtime=int(st.st_mtime * 1000),
            cli_version=meta.get("cli_version", ""),
            model_provider=meta.get("model_provider", ""),
            model=activity["model"],
            skills_used=activity["skills_used"],
            memory_ops=activity["memory_ops"],
            skill_breakdown=activity["skill_breakdown"],
        )
        _codex_cache[key] = (int(st.st_mtime * 1000), st.st_size, cs)
        sessions.append(cs)
    sessions.sort(key=lambda s: s.transcript_mtime, reverse=True)
    return sessions


def codex_timeline(path: str | Path, limit: int = 60) -> list[dict]:
    """Parse Codex JSONL into TurnEvent-compatible dicts."""
    p = Path(path)
    if not p.exists():
        return []
    events: list[dict] = []
    try:
        with p.open() as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                t = d.get("type")
                ts = d.get("timestamp", "")
                payload = d.get("payload") or {}

                if t == "event_msg":
                    role = payload.get("role", "")
                    content = payload.get("content")
                    text = ""
                    if isinstance(content, str):
                        text = content
                    elif isinstance(content, list):
                        for c in content:
                            if isinstance(c, dict) and c.get("type") == "input_text":
                                text = c.get("text", "")
                                break
                    if text and role == "user":
                        events.append({
                            "ts": ts, "kind": "user_text",
                            "text": text[:4000], "tool": None,
                            "role": "user", "extra": {},
                        })

                elif t == "response_item":
                    item_type = payload.get("type", "")
                    if item_type == "function_call":
                        events.append({
                            "ts": ts, "kind": "tool_use",
                            "text": "", "tool": payload.get("name", "function"),
                            "role": "assistant",
                            "extra": {"arguments": (payload.get("arguments") or "")[:200]},
                        })
                    elif item_type == "function_call_output":
                        events.append({
                            "ts": ts, "kind": "tool_result",
                            "text": (payload.get("output") or "")[:200],
                            "tool": None, "role": "user", "extra": {},
                        })
                    elif item_type == "message":
                        content = payload.get("content")
                        if isinstance(content, list):
                            for c in content:
                                if isinstance(c, dict) and c.get("type") == "output_text":
                                    events.append({
                                        "ts": ts, "kind": "assistant_text",
                                        "text": (c.get("text") or "")[:4000],
                                        "tool": None, "role": "assistant", "extra": {},
                                    })
    except Exception:
        pass
    return events[-limit:]
