"""Run scripts/remote-collector.py against a fake ~/.claude + ~/.codex tree and
check its JSON shape. The collector is self-contained stdlib code that runs ON a
remote host via `python3 -`, so we exercise it the same way: feed it source on
stdin with HOME pointed at a fixture. No SSH, no network."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

COLLECTOR = Path(__file__).resolve().parents[1] / "scripts" / "remote-collector.py"

# The collector only ever runs on the (Linux) remote via `python3 -`; its process
# probes (`os.kill(pid, 0)`, `ps`, `lsof`) are Unix-only. History parsing is pure
# file I/O and runs anywhere, but live-window detection can't be exercised on
# Windows CI — signal 0 there means CTRL_C_EVENT, not "is this pid alive?".
_unix_only = pytest.mark.skipif(os.name == "nt",
                                reason="collector process-liveness probe is Unix-only")


def _write(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(obj if isinstance(obj, str) else json.dumps(obj))


def _run(home: Path) -> dict:
    env = dict(os.environ, HOME=str(home))
    # utf-8 explicitly: the collector source is UTF-8 (non-ASCII title helpers),
    # so reading it and piping it to the child must not use the platform default
    # codec (cp1252 on Windows → UnicodeDecodeError).
    src = COLLECTOR.read_text(encoding="utf-8")
    proc = subprocess.run([sys.executable, "-"], input=src, env=env,
                          capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_collector_empty_home(tmp_path):
    out = _run(tmp_path)
    # history is fixture-controlled → must be empty. Live windows come from a
    # system-wide `ps`/`lsof` scan that HOME can't sandbox, so a real codex/claude
    # running on the dev box may show up; only assert none reference our fixture home.
    assert out["history"] == []
    assert "home" in out
    assert all(str(tmp_path) not in (w.get("transcript_path") or "")
               for w in out["windows"])


def test_collector_claude_history(tmp_path):
    # one claude transcript with a first user message
    proj = tmp_path / ".claude" / "projects" / "-home-x-proj"
    line = json.dumps({"type": "user",
                       "message": {"role": "user", "content": "fix the parser bug"}})
    _write(proj / "abc123.jsonl", line + "\n")
    out = _run(tmp_path)
    hist = out["history"]
    assert any(h["session_id"] == "abc123" and h["platform"] == "claude"
               and "parser" in h["first_input"] for h in hist)


def test_collector_codex_history(tmp_path):
    sess = tmp_path / ".codex" / "sessions" / "2026" / "06" / "22"
    rollout = sess / "rollout-2026-06-22T10-00-00-deadbeef.jsonl"
    meta = json.dumps({"type": "session_meta",
                       "payload": {"id": "deadbeef", "cwd": "/home/x/repo"}})
    msg = json.dumps({"type": "response_item",
                      "payload": {"type": "message", "role": "user",
                                  "content": [{"type": "input_text", "text": "deploy the model"}]}})
    _write(rollout, meta + "\n" + msg + "\n")
    out = _run(tmp_path)
    hist = out["history"]
    assert any(h["platform"] == "codex" and "deploy" in h["first_input"] for h in hist)


@_unix_only
def test_collector_live_claude_window(tmp_path):
    # a session registry file pointing at THIS process (guaranteed alive)
    reg = tmp_path / ".claude" / "sessions" / "live.json"
    _write(reg, {"pid": os.getpid(), "sessionId": "live1", "cwd": "/home/x/repo",
                 "updatedAt": int(time.time() * 1000), "status": "busy"})
    # matching transcript so transcript_path resolves
    proj = tmp_path / ".claude" / "projects" / "-home-x-repo"
    _write(proj / "live1.jsonl",
           json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}}) + "\n")
    out = _run(tmp_path)
    wins = [w for w in out["windows"] if w["platform"] == "claude"]
    assert any(w["session_id"] == "live1" and w["pid"] == os.getpid() for w in wins)


def test_collector_history_capped(tmp_path):
    # write 200 claude transcripts; collector should keep the recent ~150
    base = tmp_path / ".claude" / "projects" / "-home-x-many"
    now = time.time()
    for i in range(200):
        f = base / f"s{i:03d}.jsonl"
        _write(f, json.dumps({"type": "user",
                              "message": {"role": "user", "content": f"task {i}"}}) + "\n")
        os.utime(f, (now - i, now - i))   # stagger mtimes
    out = _run(tmp_path)
    assert 0 < len(out["history"]) <= 150


def test_collector_codex_history_has_metrics(tmp_path):
    # codex rollout with a token_count event + a message + a function_call → the
    # history row must carry token/turn/tool metrics (was empty {} before).
    sess = tmp_path / ".codex" / "sessions" / "2026" / "07" / "03"
    lines = [
        {"type": "session_meta", "payload": {"id": "cx1", "cwd": "/w",
                                             "timestamp": "2026-07-03T10:00:00.000Z"}},
        {"type": "turn_context", "payload": {"model": "gpt-5.5"}},
        {"timestamp": "2026-07-03T10:00:01.000Z", "type": "response_item",
         "payload": {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": "do it"}]}},
        {"timestamp": "2026-07-03T10:00:05.000Z", "type": "response_item",
         "payload": {"type": "function_call", "name": "exec_command", "arguments": "{}"}},
        {"type": "event_msg", "payload": {"type": "token_count", "info": {
            "total_token_usage": {"input_tokens": 10000, "output_tokens": 2000,
                                  "cached_input_tokens": 300, "total_tokens": 12345},
            "model_context_window": 272000}}},
        {"timestamp": "2026-07-03T10:00:30.000Z", "type": "response_item",
         "payload": {"type": "message", "role": "assistant",
                     "content": [{"type": "output_text", "text": "done"}]}},
    ]
    _write(sess / "rollout-2026-07-03T10-00-00-cx1.jsonl",
           "\n".join(json.dumps(x) for x in lines) + "\n")
    out = _run(tmp_path)
    h = [x for x in out["history"] if x["platform"] == "codex"][0]
    m = h["metrics"]
    assert m["tokens"]["total"] == 12345
    assert m["turns"] == 2 and m["tools"].get("exec_command") == 1
    assert m["duration_sec"] == 29 and m["model"] == "gpt-5.5"   # 10:00:01→10:00:30
    assert m["cost_usd"] is None            # codex = subscription
