#!/usr/bin/env python3
"""Self-contained collector run ON a remote host over SSH (stdlib only).

Claude Fleet pipes this script to `python3 -` on a registered remote and reads the
JSON it prints: the remote's live Claude/Codex windows + recent history. The remote
has no claude-fleet code, so this mirrors (a lean version of) core/sessions.py and
core/codex.py detection here. Output (one JSON object on stdout):

    {"windows": [ {...} ], "history": [ {...} ], "home": "/root"}

Each window/history row carries platform + session_id + cwd + first_input + the
remote transcript_path (used later for `ssh cat` timelines and resume). Keep it
cheap: live processes only + the most-recent HISTORY_LIMIT transcripts.
"""
import glob
import json
import os
import re
import subprocess
import sys

HOME = os.path.expanduser("~")
CLAUDE = os.path.join(HOME, ".claude")
CODEX = os.path.join(HOME, ".codex")
HISTORY_LIMIT = 150

_SYNTHETIC = ("<environment_context>", "<permissions instructions>",
              "<user_instructions>", "<permissions>")


def _run(args, timeout=4):
    try:
        return subprocess.check_output(args, text=True, timeout=timeout,
                                       stderr=subprocess.DEVNULL)
    except Exception:
        return ""


def _pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def _pid_tty(pid):
    out = _run(["ps", "-o", "tty=", "-p", str(pid)]).strip()
    return f"/dev/{out}" if out and out != "??" else None


def _pid_env_path(pid):
    """The PATH of a running process — used so a non-interactive `ssh host 'codex
    resume'` can actually find the binary (login shells often don't expose nvm)."""
    try:                                   # Linux
        with open(f"/proc/{pid}/environ", "rb") as f:
            for kv in f.read().split(b"\0"):
                if kv.startswith(b"PATH="):
                    return kv[5:].decode("utf-8", "replace")
    except Exception:
        pass
    out = _run(["ps", "eww", "-p", str(pid)])   # BSD/macOS fallback
    m = re.search(r"(?:^| )PATH=(\S+)", out)
    return m.group(1) if m else ""


def _fallback_path():
    """A PATH likely to contain codex/claude when no live proc gave us one:
    every nvm node bin + the usual user/local bins, then the collector's own."""
    dirs = sorted(glob.glob(os.path.join(HOME, ".nvm/versions/node/*/bin")), reverse=True)
    dirs += [os.path.join(HOME, ".local/bin"), "/usr/local/bin", "/opt/homebrew/bin"]
    dirs = [d for d in dirs if os.path.isdir(d)]
    return ":".join(dirs + [os.environ.get("PATH", "")])


def _slug(cwd):
    return cwd.replace("/", "-").replace("_", "-").replace(".", "-")


def _first_json_line(path):
    try:
        with open(path) as f:
            return json.loads(f.readline())
    except Exception:
        return None


# ---------- Claude ----------

def _claude_first_user(path):
    try:
        with open(path) as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("type") != "user":
                    continue
                msg = d.get("message") or {}
                c = msg.get("content")
                if isinstance(c, str) and c.strip():
                    return c.strip()[:300]
                if isinstance(c, list):
                    for b in c:
                        if isinstance(b, dict) and b.get("type") == "text" and b.get("text", "").strip():
                            return b["text"].strip()[:300]
    except Exception:
        pass
    return ""


def _claude_windows():
    out = []
    sdir = os.path.join(CLAUDE, "sessions")
    for f in glob.glob(os.path.join(sdir, "*.json")):
        if os.path.basename(f).startswith("session-"):
            continue
        try:
            with open(f) as fh:
                d = json.load(fh)
        except Exception:
            continue
        if not isinstance(d, dict) or "pid" not in d:
            continue
        pid = d["pid"]
        if not _pid_alive(pid):
            continue
        sid = d.get("sessionId", "")
        cwd = d.get("cwd", "")
        tp = os.path.join(CLAUDE, "projects", _slug(cwd), f"{sid}.jsonl")
        out.append({
            "platform": "claude", "pid": pid, "session_id": sid, "cwd": cwd,
            "name": d.get("name"), "first_input": "",
            "status": d.get("status", "unknown"),
            "waiting_for": d.get("waitingFor"),
            "updated_at": int(d.get("updatedAt", 0)),
            "tty": _pid_tty(pid),
            "transcript_path": tp if os.path.exists(tp) else None,
            "env_path": _pid_env_path(pid),
        })
    return out


# ---------- Codex ----------

def _codex_meta(path):
    d = _first_json_line(path)
    if d and d.get("type") == "session_meta":
        return d.get("payload") or {}
    return None


def _codex_first_user(path):
    try:
        with open(path) as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("type") != "response_item":
                    continue
                p = d.get("payload") or {}
                if p.get("type") != "message" or p.get("role") != "user":
                    continue
                for c in (p.get("content") or []):
                    if isinstance(c, dict) and c.get("type") == "input_text":
                        t = (c.get("text") or "").strip()
                        if t and not any(t.startswith(s) for s in _SYNTHETIC):
                            return t[:300]
    except Exception:
        pass
    return ""


def _running_codex_pids():
    out = _run(["ps", "-axo", "pid=,comm="])
    pids = []
    for line in out.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and os.path.basename(parts[1]) == "codex":
            try:
                pids.append(int(parts[0]))
            except ValueError:
                pass
    return pids


def _pid_open_rollout(pid):
    out = _run(["lsof", "-p", str(pid), "-Ffn"])
    for line in out.splitlines():
        if line.startswith("n"):
            name = line[1:]
            if name.endswith(".jsonl") and "/.codex/sessions/" in name:
                return name
    return None


def _codex_windows():
    out = []
    for pid in _running_codex_pids():
        path = _pid_open_rollout(pid)   # honest identity: only when caught mid-turn
        if not path or not os.path.exists(path):
            continue
        meta = _codex_meta(path) or {}
        cwd = meta.get("cwd", "")
        try:
            mtime = int(os.path.getmtime(path) * 1000)
        except OSError:
            mtime = 0
        out.append({
            "platform": "codex", "pid": pid, "session_id": meta.get("id", ""),
            "cwd": cwd, "name": _codex_first_user(path), "first_input": _codex_first_user(path),
            "status": "running", "waiting_for": None, "updated_at": mtime,
            "tty": _pid_tty(pid), "transcript_path": path,
            "env_path": _pid_env_path(pid),
        })
    return out


# ---------- history (recent N transcripts) ----------

def _recent(paths, n):
    scored = []
    for p in paths:
        try:
            scored.append((os.path.getmtime(p), p))
        except OSError:
            pass
    scored.sort(reverse=True)
    return [p for _, p in scored[:n]]


def _history():
    rows = []
    claude_tx = glob.glob(os.path.join(CLAUDE, "projects", "*", "*.jsonl"))
    codex_tx = glob.glob(os.path.join(CODEX, "sessions", "**", "*.jsonl"), recursive=True)
    for p in _recent(claude_tx, HISTORY_LIMIT):
        sid = os.path.splitext(os.path.basename(p))[0]
        rows.append({
            "platform": "claude", "session_id": sid,
            "first_input": _claude_first_user(p),
            "transcript_path": p,
            "transcript_mtime": int(os.path.getmtime(p) * 1000),
            "project": "", "project_name": os.path.basename(os.path.dirname(p)),
        })
    for p in _recent(codex_tx, HISTORY_LIMIT):
        meta = _codex_meta(p) or {}
        cwd = meta.get("cwd", "")
        rows.append({
            "platform": "codex", "session_id": meta.get("id", os.path.splitext(os.path.basename(p))[0]),
            "first_input": _codex_first_user(p),
            "transcript_path": p,
            "transcript_mtime": int(os.path.getmtime(p) * 1000),
            "first_ts": meta.get("timestamp", ""),
            "project": cwd, "project_name": cwd.rsplit("/", 1)[-1] if cwd else "",
        })
    return rows


def main():
    try:
        windows = _claude_windows() + _codex_windows()
        # Host-level resume PATH: reuse a live session's env (it already has codex/
        # claude on PATH); fall back to scanning nvm/local bins. Used to resume
        # History sessions that have no live process of their own.
        path = next((w["env_path"] for w in windows if w.get("env_path")), "") or _fallback_path()
        result = {"home": HOME, "windows": windows, "history": _history(), "path": path}
    except Exception as e:
        result = {"error": str(e), "windows": [], "history": [], "path": ""}
    sys.stdout.write(json.dumps(result))


if __name__ == "__main__":
    main()
