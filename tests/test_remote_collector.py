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
    # utf-8 explicitly — seeds carry CJK and Windows' default is cp1252.
    p.write_text(obj if isinstance(obj, str) else json.dumps(obj), encoding="utf-8")


def _run(home: Path, *argv: str, env_extra: dict | None = None) -> dict:
    env = dict(os.environ, HOME=str(home))
    if env_extra:
        env.update(env_extra)
    # utf-8 explicitly: the collector source is UTF-8 (non-ASCII title helpers),
    # so reading it and piping it to the child must not use the platform default
    # codec (cp1252 on Windows → UnicodeDecodeError).
    src = COLLECTOR.read_text(encoding="utf-8")
    proc = subprocess.run([sys.executable, "-", *argv], input=src, env=env,
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


# ---------- search mode (`python3 - search <query>`) ----------

def _seed_search_home(tmp_path):
    proj = tmp_path / ".claude" / "projects" / "-work-exp"
    # ensure_ascii=False: real Claude Code transcripts store CJK raw, and the
    # snippet extractor works on raw lines — escaped \uXXXX would never match.
    _write(proj / "cl-1.jsonl", "\n".join([
        json.dumps({"type": "user", "cwd": "/work/exp",
                    "message": {"role": "user", "content": "跑 caption v2 对照实验"}},
                   ensure_ascii=False),
        json.dumps({"type": "assistant",
                    "message": {"role": "assistant",
                                "content": [{"type": "text", "text": "caption_compare dashboard built"}]}}),
    ]) + "\n")
    sess = tmp_path / ".codex" / "sessions" / "2026" / "05" / "12"
    _write(sess / "rollout-2026-05-12T01-00-00-cx-7.jsonl", "\n".join([
        json.dumps({"type": "session_meta", "payload": {"id": "cx-7", "cwd": "/tmp/audit"}}),
        json.dumps({"type": "response_item",
                    "payload": {"type": "message", "role": "user",
                                "content": [{"type": "input_text",
                                             "text": "audit the caption_compare bitable"}]}}),
    ]) + "\n")


def test_collector_search_hits_both_platforms(tmp_path):
    _seed_search_home(tmp_path)
    out = _run(tmp_path, "search", "caption_compare")
    matches = out["matches"]
    assert {m["platform"] for m in matches} == {"claude", "codex"}
    cl = next(m for m in matches if m["platform"] == "claude")
    assert cl["session_id"] == "cl-1"
    assert cl["project"] == "/work/exp"          # cwd recovered for resume
    assert any("caption_compare" in s for s in cl["snippets"])
    cx = next(m for m in matches if m["platform"] == "codex")
    assert cx["session_id"] == "cx-7"            # meta id, not the rollout stem
    assert cx["project"] == "/tmp/audit"
    assert out["path"]                           # resume env PATH rides along


def test_collector_search_case_insensitive_unicode(tmp_path):
    _seed_search_home(tmp_path)
    out = _run(tmp_path, "search", "CAPTION V2")
    assert any("对照实验" in s for m in out["matches"] for s in m["snippets"])


def test_collector_search_no_match(tmp_path):
    _seed_search_home(tmp_path)
    out = _run(tmp_path, "search", "zzz-not-there")
    assert out["matches"] == []


def test_collector_search_days_window(tmp_path):
    _seed_search_home(tmp_path)
    old = time.time() - 40 * 86400
    for f in (tmp_path / ".claude").rglob("*.jsonl"):
        os.utime(f, (old, old))
    for f in (tmp_path / ".codex").rglob("*.jsonl"):
        os.utime(f, (old, old))
    out = _run(tmp_path, "search", "caption_compare", "--days", "7")
    assert out["matches"] == []
    out = _run(tmp_path, "search", "caption_compare", "--days", "90")
    assert len(out["matches"]) == 2


def test_collector_search_pure_python_fallback(tmp_path):
    # Empty PATH → shutil.which('rg') fails → the stdlib line scan must produce
    # the same hits (this is the path a bare remote box without ripgrep takes).
    _seed_search_home(tmp_path)
    out = _run(tmp_path, "search", "caption_compare", env_extra={"PATH": ""})
    assert {m["platform"] for m in out["matches"]} == {"claude", "codex"}


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
