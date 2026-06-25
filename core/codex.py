"""Parse ~/.codex/sessions/ into HistorySession-compatible objects + timeline."""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
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
    metrics: dict = field(default_factory=dict)

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
            "metrics": self.metrics,
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


# Synthetic role=user/developer records Codex injects before the real prompt
# (sandbox/permission preamble + environment context). Skip them so the title
# shows the user's actual first message, not the boilerplate or the assistant's
# reply.
_SYNTHETIC_PREFIXES = (
    "<environment_context>",
    "<permissions instructions>",
    "<user_instructions>",
    "<permissions>",
)


def _first_text(content) -> str:
    """Pull the first non-empty text out of a Codex message content payload."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        for c in content:
            if isinstance(c, dict) and c.get("type") in ("input_text", "text", "output_text"):
                t = (c.get("text") or "").strip()
                if t:
                    return t
    return ""


def _extract_first_user_input(path: Path) -> str:
    """Return the user's real first prompt.

    The genuine prompt is a `response_item`/`message` with role=="user" carrying
    `input_text`, but Codex precedes it with synthetic user/developer records
    (permissions + environment_context). The old code grabbed the first
    `output_text` instead, which is the *assistant's* opening reply — so every
    Codex session was mistitled with "我会先…" / "I'll…". Skip the wrappers and
    take the first real user message; only fall back to assistant text if none.
    """
    assistant_fallback = ""
    try:
        with path.open() as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("type") != "response_item":
                    continue
                payload = d.get("payload") or {}
                if payload.get("type") != "message":
                    continue
                role = payload.get("role")
                text = _first_text(payload.get("content"))
                if not text:
                    continue
                if role == "user":
                    if any(text.startswith(p) for p in _SYNTHETIC_PREFIXES):
                        continue
                    return text[:300]
                if role == "assistant" and not assistant_fallback:
                    assistant_fallback = text[:300]
    except Exception:
        pass
    return assistant_fallback


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
            "metrics": {},
        }

    bash_refs: dict[str, int] = {}
    skill_reads: dict[str, int] = {}
    skill_writes: dict[str, int] = {}
    memory_ops_seen: set[tuple[str, str]] = set()
    memory_ops: list[dict] = []
    model = ""

    # --- mining metrics (single-pass alongside skill scan) ---
    m_tokens = {"input": 0, "output": 0, "cache_read": 0,
                "cache_creation": 0, "reasoning": 0, "total": 0}
    m_tools: dict[str, int] = {}
    m_turns = 0
    m_first_ts = m_last_ts = ""
    ctx_window = 0
    ctx_pct = None

    try:
        with p.open() as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                t = d.get("type", "")
                payload = d.get("payload") or {}
                ts = d.get("timestamp", "")
                if ts:
                    if not m_first_ts:
                        m_first_ts = ts
                    m_last_ts = ts

                # token_count carries the running cumulative usage; keep the last.
                if t == "event_msg" and payload.get("type") == "token_count":
                    info = payload.get("info") or {}
                    tot = info.get("total_token_usage") or {}
                    if tot:
                        m_tokens["input"] = tot.get("input_tokens", 0) or 0
                        m_tokens["cache_read"] = tot.get("cached_input_tokens", 0) or 0
                        m_tokens["output"] = tot.get("output_tokens", 0) or 0
                        m_tokens["reasoning"] = tot.get("reasoning_output_tokens", 0) or 0
                        m_tokens["total"] = tot.get("total_tokens", 0) or 0
                    ctx_window = info.get("model_context_window", 0) or ctx_window
                    prim = (payload.get("rate_limits") or {}).get("primary") or {}
                    if prim.get("used_percent") is not None:
                        ctx_pct = prim["used_percent"]

                if t == "turn_context":
                    m = payload.get("model", "")
                    if m:
                        model = m

                if t != "response_item":
                    continue

                # count user/assistant messages + every function call (tool histogram)
                if payload.get("type") == "message":
                    if payload.get("role") in ("user", "assistant"):
                        m_turns += 1
                if payload.get("type") != "function_call":
                    continue
                name = payload.get("name", "")
                m_tools[name] = m_tools.get(name, 0) + 1
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
    from .metrics import _duration
    seen_input = m_tokens["input"] + m_tokens["cache_creation"] + m_tokens["cache_read"]
    metrics = {
        "tokens": m_tokens,
        "cache_hit": round(m_tokens["cache_read"] / seen_input, 3) if seen_input else None,
        "context_pct": ctx_pct,
        "context_window": ctx_window or None,
        "duration_sec": _duration(m_first_ts, m_last_ts),
        "turns": m_turns,
        "tools": m_tools,
        "files": [],
        "errors": 0,
        "cost_usd": None,  # codex is a subscription — show tokens/context%, not $
        "model": model,
    }
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
        "metrics": metrics,
    }


# Codex transcripts are immutable once written; parsing each one fully (meta +
# activity scan) dominates list_codex_sessions(). Cache by (path, mtime, size)
# so a rebuild only re-parses files that changed.
_codex_cache: dict[str, tuple[int, int, "CodexSession"]] = {}


def _build_codex_session(f: Path) -> Optional[CodexSession]:
    """Parse ONE rollout file into a CodexSession, cached by (mtime, size).

    Shared by list_codex_sessions (full scan, for History) and list_codex_windows
    (which only needs the specific file an lsof confirmed a process has open) —
    so the live board never has to rglob the whole sessions tree."""
    try:
        st = f.stat()
    except Exception:
        return None
    key = str(f)
    mtime = int(st.st_mtime * 1000)
    cached = _codex_cache.get(key)
    if cached and cached[0] == mtime and cached[1] == st.st_size:
        return cached[2]
    meta = _parse_session_meta(f)
    if not meta:
        return None
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
        transcript_mtime=mtime,
        cli_version=meta.get("cli_version", ""),
        model_provider=meta.get("model_provider", ""),
        model=activity["model"],
        skills_used=activity["skills_used"],
        memory_ops=activity["memory_ops"],
        skill_breakdown=activity["skill_breakdown"],
        metrics=activity.get("metrics", {}),
    )
    _codex_cache[key] = (mtime, st.st_size, cs)
    return cs


def list_codex_sessions() -> list[CodexSession]:
    if not CODEX_SESSIONS_DIR.exists():
        return []
    sessions: list[CodexSession] = []
    for f in CODEX_SESSIONS_DIR.rglob("*.jsonl"):
        cs = _build_codex_session(f)
        if cs is not None:
            sessions.append(cs)
    sessions.sort(key=lambda s: s.transcript_mtime, reverse=True)
    return sessions


# ---------------------------------------------------------------------------
# Live codex tracking for the top board.
#
# Codex (unlike Claude Code) writes no pid registry under ~/.claude/sessions/, so
# `sessions.list_windows()` never sees a running codex task. We instead detect
# live `codex` processes via ps, resolve each one's cwd with lsof, and match it to
# the rollout file being appended in that directory.
# ---------------------------------------------------------------------------

def _running_codex_pids() -> list[int]:
    try:
        out = subprocess.check_output(["ps", "-axo", "pid=,comm="], text=True, timeout=4)
    except Exception:
        return []
    pids: list[int] = []
    for line in out.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2:
            continue
        pid_s, comm = parts
        # comm is the full executable path; match the codex binary itself, not the
        # node wrapper that launches it.
        if os.path.basename(comm) == "codex":
            try:
                pids.append(int(pid_s))
            except ValueError:
                continue
    return pids


def _pid_files(pid: int) -> tuple:
    """(cwd, open_rollout_path) for a pid from a single lsof call. open_rollout is
    the ~/.codex/sessions/*.jsonl the process currently has open (only present
    while it's writing), which is the EXACT process↔session link when available."""
    try:
        out = subprocess.check_output(
            ["lsof", "-p", str(pid), "-Ffn"],
            text=True, timeout=4, stderr=subprocess.DEVNULL,
        )
    except Exception:
        return None, None
    cwd = None
    rollout = None
    fd = None
    for line in out.splitlines():
        if line.startswith("f"):
            fd = line[1:]
        elif line.startswith("n"):
            name = line[1:]
            if fd == "cwd" and cwd is None:
                cwd = name or None
            elif name.endswith(".jsonl") and "/.codex/sessions/" in name:
                rollout = name
    return cwd, rollout


# pid -> rollout path the process was last seen with OPEN via lsof. This is the
# ONLY reliable process↔session link (codex records no pid/tty in the rollout, and
# a long-lived TUI runs many sessions, so start-time matching is meaningless).
# It's caught whenever the session does a turn; persists across polls.
_pid_rollout: dict[int, str] = {}

# Short cache so the watcher's 2s poll doesn't lsof every tick.
_win_cache: dict = {"pids": None, "windows": [], "ts": 0.0}
_WIN_CACHE_TTL = 2.0


def _codex_window(pid: int, tty, cwd, cs, now_ms: int) -> dict:
    if cs is not None:
        return {
            "pid": pid, "session_id": cs.session_id, "cwd": cwd or cs.project,
            "project_name": cs.project_name, "project_slug": "",
            "name": cs.first_input or cs.project_name, "first_input": cs.first_input,
            "status": "running", "waiting_for": None,
            "started_at": cs.transcript_mtime, "updated_at": cs.transcript_mtime,
            "version": cs.cli_version, "tty": tty,
            "transcript_path": cs.transcript_path, "alive": True,
            "platform": "codex", "model": cs.model,
            "idle_seconds": max(0, int((now_ms - cs.transcript_mtime) / 1000)),
        }
    # Identity not yet confirmed (idle at a prompt, never caught mid-turn). Show a
    # NEUTRAL card — never a guessed/stale title — still focusable via its tty.
    base = (cwd or "").rsplit("/", 1)[-1] or "~"
    return {
        "pid": pid, "session_id": f"codex-pid-{pid}", "cwd": cwd or "",
        "project_name": base, "project_slug": "",
        "name": f"codex · {base}", "first_input": "",
        "status": "running", "waiting_for": None,
        "started_at": now_ms, "updated_at": now_ms, "version": "",
        "tty": tty, "transcript_path": None, "alive": True,
        "platform": "codex", "model": "", "idle_seconds": 0,
    }


def list_codex_windows() -> list[dict]:
    """Open codex terminals as Window-compatible dicts (platform='codex').

    ONE card per running codex process (= one open terminal). Identity is honest:
    if lsof has caught which rollout the process has open (now or earlier, while it
    did a turn) we show that real session; otherwise a neutral "codex · <cwd>"
    card. We never fabricate a title from an unrelated old rollout — that's what
    surfaced long-closed sessions. Focus only needs the process's own tty.
    """
    pids = _running_codex_pids()
    if not pids:
        _win_cache.update(pids=(), windows=[], ts=time.time())
        _pid_rollout.clear()
        return []

    now = time.time()
    now_ms = int(now * 1000)
    pidset = tuple(sorted(pids))
    if pidset == _win_cache["pids"] and now - _win_cache["ts"] < _WIN_CACHE_TTL:
        for w in _win_cache["windows"]:
            if w.get("transcript_path"):
                w["idle_seconds"] = max(0, int((now_ms - w["updated_at"]) / 1000))
        return _win_cache["windows"]

    from .sessions import get_tty

    windows: list[dict] = []
    for pid in pids:
        cwd, open_rollout = _pid_files(pid)
        if open_rollout:
            _pid_rollout[pid] = open_rollout      # confirmed: this is its session
        path = _pid_rollout.get(pid)
        cs = _build_codex_session(Path(path)) if path else None
        windows.append(_codex_window(pid, get_tty(pid), cwd, cs, now_ms))
    for dead in [p for p in _pid_rollout if p not in set(pids)]:
        _pid_rollout.pop(dead, None)

    _win_cache.update(pids=pidset, windows=windows, ts=now)
    return windows


def find_codex_transcript_path(session_id: str) -> Optional[Path]:
    """Locate a codex rollout .jsonl by session id (it's embedded in the filename
    after the timestamp, e.g. rollout-<ts>-<id>.jsonl)."""
    if not CODEX_SESSIONS_DIR.exists():
        return None
    for f in CODEX_SESSIONS_DIR.rglob("*.jsonl"):
        if session_id in f.stem:
            return f
    return None


def codex_meta(path: str | Path) -> dict:
    """cwd + session id from the codex session_meta first line."""
    m = _parse_session_meta(Path(path)) or {}
    return {"cwd": m.get("cwd", ""), "session_id": m.get("id", "")}


_current_task_cache: dict[str, tuple[int, int, Optional[str]]] = {}


def codex_current_task(path: str | Path) -> Optional[str]:
    """A one-line hint of where a codex session is, for the live card.

    Mirrors transcripts.current_task_hint for Claude: prefer the latest assistant
    message, else the latest tool call, else the latest user prompt. Cached by
    (mtime, size) — codex_timeline reads the whole rollout, which can be tens of MB,
    so the 2s poll must not re-parse an unchanged file every tick.
    """
    tp = str(path)
    try:
        st = Path(tp).stat()
    except OSError:
        return None
    mtime, size = int(st.st_mtime * 1000), st.st_size
    cached = _current_task_cache.get(tp)
    if cached and cached[0] == mtime and cached[1] == size:
        return cached[2]
    evs = codex_timeline(path, limit=60)
    result: Optional[str] = None
    for kinds in (("assistant_text",), ("tool_use",), ("user_text",)):
        for e in reversed(evs):
            if e.get("kind") in kinds:
                txt = (e.get("text") or e.get("tool") or "").strip()
                if txt:
                    result = txt[:160]
                    break
        if result:
            break
    _current_task_cache[tp] = (mtime, size, result)
    return result


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
                            "extra": {"arguments": (payload.get("arguments") or "")[:2000]},
                        })
                    elif item_type == "function_call_output":
                        out = payload.get("output")
                        if isinstance(out, dict):
                            out = out.get("output") or out.get("content") or json.dumps(out)
                        events.append({
                            "ts": ts, "kind": "tool_result",
                            "text": str(out or "")[:4000],
                            "tool": None, "role": "user", "extra": {},
                        })
                    elif item_type == "reasoning":
                        # Codex encrypts the reasoning trace; only the (often empty)
                        # summary list is human-readable. Skip when there's nothing.
                        summary = payload.get("summary") or []
                        text = "\n".join(
                            s.get("text", "") if isinstance(s, dict) else str(s)
                            for s in summary
                        ).strip()
                        if text:
                            events.append({
                                "ts": ts, "kind": "reasoning",
                                "text": text[:4000], "tool": None,
                                "role": "assistant", "extra": {},
                            })
                    elif item_type == "message":
                        role = payload.get("role", "")
                        for c in (payload.get("content") or []):
                            if not isinstance(c, dict):
                                continue
                            ctype = c.get("type")
                            text = (c.get("text") or "").strip()
                            if not text:
                                continue
                            if ctype == "output_text":
                                events.append({
                                    "ts": ts, "kind": "assistant_text",
                                    "text": text[:4000], "tool": None,
                                    "role": "assistant", "extra": {},
                                })
                            elif ctype == "input_text" and role == "user":
                                # the genuine user prompt — skip synthetic wrappers
                                if any(text.startswith(p) for p in _SYNTHETIC_PREFIXES):
                                    continue
                                events.append({
                                    "ts": ts, "kind": "user_text",
                                    "text": text[:4000], "tool": None,
                                    "role": "user", "extra": {},
                                })
    except Exception:
        pass
    return events[-limit:]
