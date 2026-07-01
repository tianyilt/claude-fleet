"""Tests for codex live-window tracking (Fix 2a) + real-prompt titles (Fix 2b)
and transcript cwd recovery used by Resume (Fix 1a)."""
import json

from core import codex, transcripts


def _write_jsonl(path, records):
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


# ---------- Fix 2b: _extract_first_user_input returns the user's prompt ----------

def test_codex_first_input_skips_synthetic_and_assistant(tmp_path):
    f = tmp_path / "rollout.jsonl"
    _write_jsonl(f, [
        {"type": "session_meta", "payload": {"id": "x", "cwd": "/tmp"}},
        {"type": "response_item", "payload": {"type": "message", "role": "developer",
         "content": [{"type": "input_text", "text": "<permissions instructions>\nstuff"}]}},
        {"type": "response_item", "payload": {"type": "message", "role": "user",
         "content": [{"type": "input_text", "text": "<environment_context>\n  <cwd>/tmp</cwd>"}]}},
        {"type": "response_item", "payload": {"type": "message", "role": "user",
         "content": [{"type": "input_text", "text": "看看这个存档怎么放到杀戮尖塔2里面"}]}},
        {"type": "response_item", "payload": {"type": "message", "role": "assistant",
         "content": [{"type": "output_text", "text": "我会先核对……"}]}},
    ])
    assert codex._extract_first_user_input(f) == "看看这个存档怎么放到杀戮尖塔2里面"


def test_codex_first_input_falls_back_to_assistant(tmp_path):
    f = tmp_path / "rollout.jsonl"
    _write_jsonl(f, [
        {"type": "session_meta", "payload": {"id": "x", "cwd": "/tmp"}},
        {"type": "response_item", "payload": {"type": "message", "role": "assistant",
         "content": [{"type": "output_text", "text": "assistant only"}]}},
    ])
    assert codex._extract_first_user_input(f) == "assistant only"


# ---------- list_codex_windows: open windows, honest identity (lsof-confirmed) ----------

def _mk_cs(sid, path, first_input="task", cwd="/w"):
    return codex.CodexSession(
        session_id=sid, project=cwd, project_name=cwd.rsplit("/", 1)[-1],
        first_input=first_input, first_ts="", last_ts="", transcript_path=path,
        transcript_size=1, transcript_mtime=1000, cli_version="", model_provider="", model="gpt-5")


def test_codex_window_real_identity_when_lsof_confirms(monkeypatch):
    # lsof catches the rollout the process has open → card shows that real session.
    codex._win_cache.update(pids=None, windows=[], ts=0.0)
    codex._pid_rollout.clear()
    monkeypatch.setattr(codex, "_running_codex_pids", lambda: [4242])
    monkeypatch.setattr(codex, "_pid_files", lambda pid: ("/w", "/open.jsonl"))
    monkeypatch.setattr(codex, "_build_codex_session",
                        lambda f: _mk_cs("sid-A", str(f), "play slay the spire"))
    monkeypatch.setattr("core.sessions.get_tty", lambda pid: "/dev/ttys9")
    ws = codex.list_codex_windows()
    assert len(ws) == 1
    w = ws[0]
    assert w["pid"] == 4242 and w["session_id"] == "sid-A" and w["tty"] == "/dev/ttys9"
    assert w["name"] == "play slay the spire" and w["transcript_path"] == "/open.jsonl"


def test_codex_window_hidden_when_unidentified(monkeypatch):
    # No rollout open and never confirmed → NOT shown (avoids a useless grey card).
    codex._win_cache.update(pids=None, windows=[], ts=0.0)
    codex._pid_rollout.clear()
    monkeypatch.setattr(codex, "_running_codex_pids", lambda: [1])
    monkeypatch.setattr(codex, "_pid_files", lambda pid: ("/Users/x", None))
    monkeypatch.setattr("core.sessions.get_tty", lambda pid: "/dev/ttys1")
    assert codex.list_codex_windows() == []


def test_codex_windows_one_card_per_identified_pid_same_cwd(monkeypatch):
    # Two identified codex terminals in the SAME cwd → two cards, never collapsed.
    codex._win_cache.update(pids=None, windows=[], ts=0.0)
    codex._pid_rollout.clear()
    monkeypatch.setattr(codex, "_running_codex_pids", lambda: [100, 200])
    monkeypatch.setattr(codex, "_pid_files",
                        lambda pid: ("/work", f"/r{pid}.jsonl"))
    monkeypatch.setattr(codex, "_build_codex_session",
                        lambda f: _mk_cs(f"sid-{f.name}", str(f)))
    monkeypatch.setattr("core.sessions.get_tty", lambda pid: f"/dev/ttys{pid}")
    ws = codex.list_codex_windows()
    assert len(ws) == 2 and {w["pid"] for w in ws} == {100, 200}
    assert len({w["session_id"] for w in ws}) == 2 and all(w["tty"] for w in ws)


def test_codex_window_persists_confirmed_identity_across_idle(monkeypatch):
    # Once lsof confirms a pid's rollout, it sticks even when later polls don't
    # catch it open (the session went idle) — so identity doesn't flap to neutral.
    codex._win_cache.update(pids=None, windows=[], ts=0.0)
    codex._pid_rollout.clear()
    monkeypatch.setattr(codex, "_running_codex_pids", lambda: [5])
    monkeypatch.setattr(codex, "_build_codex_session", lambda f: _mk_cs("sid-X", str(f)))
    monkeypatch.setattr("core.sessions.get_tty", lambda pid: "/dev/ttys5")
    # poll 1: caught writing
    monkeypatch.setattr(codex, "_pid_files", lambda pid: ("/w", "/r.jsonl"))
    codex.list_codex_windows()
    # poll 2: now idle (no open rollout) but cache expired
    codex._win_cache.update(pids=None, windows=[], ts=0.0)
    monkeypatch.setattr(codex, "_pid_files", lambda pid: ("/w", None))
    ws = codex.list_codex_windows()
    assert ws[0]["session_id"] == "sid-X"        # remembered, not reverted to neutral


def test_list_codex_windows_empty_when_no_process(monkeypatch):
    monkeypatch.setattr(codex, "_running_codex_pids", lambda: [])
    assert codex.list_codex_windows() == []


# ---------- Fix 1a: transcript_cwd recovers the real working directory ----------

def test_transcript_cwd_reads_first_cwd(tmp_path):
    f = tmp_path / "t.jsonl"
    _write_jsonl(f, [
        {"type": "summary"},
        {"type": "user", "cwd": "/Users/me/project/foo", "sessionId": "s"},
    ])
    assert transcripts.transcript_cwd(f) == "/Users/me/project/foo"


def test_transcript_cwd_none_when_absent(tmp_path):
    f = tmp_path / "t.jsonl"
    _write_jsonl(f, [{"type": "summary"}])
    assert transcripts.transcript_cwd(f) is None


# ---------- B2: codex_timeline captures user messages + reasoning ----------

def test_codex_timeline_includes_user_messages(tmp_path):
    f = tmp_path / "rollout.jsonl"
    _write_jsonl(f, [
        {"type": "session_meta", "payload": {"id": "x", "cwd": "/w"}},
        {"type": "response_item", "payload": {"type": "message", "role": "user",
         "content": [{"type": "input_text", "text": "<environment_context>\n  skip me"}]}},
        {"type": "response_item", "payload": {"type": "message", "role": "user",
         "content": [{"type": "input_text", "text": "real user prompt"}]}},
        {"type": "response_item", "payload": {"type": "reasoning",
         "summary": [{"type": "summary_text", "text": "thinking about it"}]}},
        {"type": "response_item", "payload": {"type": "function_call", "name": "exec_command",
         "arguments": "{\"cmd\": \"ls\"}"}},
        {"type": "response_item", "payload": {"type": "function_call_output", "output": "a\nb"}},
        {"type": "response_item", "payload": {"type": "message", "role": "assistant",
         "content": [{"type": "output_text", "text": "done"}]}},
    ])
    evs = codex.codex_timeline(f, limit=50)
    kinds = [e["kind"] for e in evs]
    assert "user_text" in kinds and "reasoning" in kinds
    user = [e["text"] for e in evs if e["kind"] == "user_text"]
    assert user == ["real user prompt"]          # synthetic wrapper skipped
    assert any(e["kind"] == "reasoning" and "thinking" in e["text"] for e in evs)


# ---------- codex_current_task ----------

def test_codex_current_task_prefers_last_assistant(tmp_path):
    f = tmp_path / "rollout.jsonl"
    _write_jsonl(f, [
        {"type": "session_meta", "payload": {"id": "x", "cwd": "/w"}},
        {"type": "response_item", "payload": {"type": "message", "role": "user",
         "content": [{"type": "input_text", "text": "task"}]}},
        {"type": "response_item", "payload": {"type": "message", "role": "assistant",
         "content": [{"type": "output_text", "text": "latest assistant line"}]}},
    ])
    assert codex.codex_current_task(f) == "latest assistant line"


# ---------- B1: codex share resolves ----------

def test_codex_share_renders(tmp_path, monkeypatch):
    from core import share, transcripts as tr
    f = tmp_path / "rollout-x.jsonl"
    _write_jsonl(f, [
        {"type": "session_meta", "payload": {"id": "codexsid1", "cwd": "/proj/foo"}},
        {"type": "response_item", "payload": {"type": "message", "role": "user",
         "content": [{"type": "input_text", "text": "hello codex"}]}},
    ])
    # claude lookup misses; codex lookup hits our temp file
    monkeypatch.setattr(tr, "find_transcript_path", lambda sid: None)
    monkeypatch.setattr(codex, "find_codex_transcript_path", lambda sid: f)
    title, html = share.render_session_html("codexsid1")
    assert "foo" in title and "hello codex" in html


# ---------- Fix: codex triage from mtime, not the always-open rollout fd ----------

def test_codex_idle_not_stuck_working(monkeypatch):
    """codex holds its rollout fd open for life, so active=True is permanent. An
    idle session (old transcript) must read 'idle', never a fake 'working'."""
    import app
    monkeypatch.setattr(app.sessions, "snapshot", lambda: {"windows": [], "counts": {}, "ts": 0})
    monkeypatch.setattr(app.perms, "pending_by_tty", lambda: {})
    monkeypatch.setattr(app.remote, "cached_windows", lambda: [])
    win = {"pid": 1, "platform": "codex", "session_id": "s", "tty": "/dev/ttys1",
           "transcript_path": None, "active": True,             # fd open (alive)
           "idle_seconds": 99999, "updated_at": 1, "status": "running"}
    monkeypatch.setattr(app.codex, "list_codex_windows", lambda: [dict(win)])
    snap = app._build_enriched_snapshot()
    cx = [w for w in snap["windows"] if w["platform"] == "codex"][0]
    assert cx["triage"] == "idle"


def test_codex_recent_write_is_working(monkeypatch):
    import app
    monkeypatch.setattr(app.sessions, "snapshot", lambda: {"windows": [], "counts": {}, "ts": 0})
    monkeypatch.setattr(app.perms, "pending_by_tty", lambda: {})
    monkeypatch.setattr(app.remote, "cached_windows", lambda: [])
    win = {"pid": 1, "platform": "codex", "session_id": "s", "tty": "/dev/ttys1",
           "transcript_path": None, "active": True,
           "idle_seconds": 5, "updated_at": 1, "status": "running"}   # just wrote
    monkeypatch.setattr(app.codex, "list_codex_windows", lambda: [dict(win)])
    snap = app._build_enriched_snapshot()
    cx = [w for w in snap["windows"] if w["platform"] == "codex"][0]
    assert cx["triage"] == "working"


# ---------- Fix: codex plan (update_plan) → plan_history versions ----------

def test_codex_plan_history_versions(tmp_path):
    f = tmp_path / "rollout.jsonl"
    _write_jsonl(f, [
        {"type": "session_meta", "payload": {"id": "x", "cwd": "/tmp"}},
        {"timestamp": "t1", "type": "response_item", "payload": {
            "type": "function_call", "name": "update_plan", "arguments": json.dumps(
                {"explanation": "first", "plan": [
                    {"step": "read", "status": "in_progress"},
                    {"step": "fix", "status": "pending"}]})}},
        {"timestamp": "t2", "type": "response_item", "payload": {
            "type": "function_call", "name": "update_plan", "arguments": json.dumps(
                {"plan": [{"step": "read", "status": "completed"},
                          {"step": "fix", "status": "in_progress"}]})}},
    ])
    ph = codex.codex_plan_history(f)
    assert [v["version_label"] for v in ph] == ["v1", "v2"]
    assert "[~] read" in ph[0]["content"] and "[ ] fix" in ph[0]["content"]
    assert "[x] read" in ph[1]["content"] and "[~] fix" in ph[1]["content"]


def test_codex_plan_history_empty_when_no_plan(tmp_path):
    f = tmp_path / "rollout.jsonl"
    _write_jsonl(f, [{"type": "session_meta", "payload": {"id": "x", "cwd": "/tmp"}}])
    assert codex.codex_plan_history(f) == []


# ---------- Fix: readable codex titles (skip low-info openers + compress paths) ----------

def test_codex_title_skips_greeting_opener(tmp_path):
    f = tmp_path / "r.jsonl"
    _write_jsonl(f, [
        {"type": "session_meta", "payload": {"id": "x", "cwd": "/tmp"}},
        {"type": "response_item", "payload": {"type": "message", "role": "user",
         "content": [{"type": "input_text", "text": "<environment_context>\n  <cwd>/tmp</cwd>"}]}},
        {"type": "response_item", "payload": {"type": "message", "role": "user",
         "content": [{"type": "input_text", "text": "你好"}]}},
        {"type": "response_item", "payload": {"type": "message", "role": "assistant",
         "content": [{"type": "output_text", "text": "你好！有什么可以帮你"}]}},
        {"type": "response_item", "payload": {"type": "message", "role": "user",
         "content": [{"type": "input_text", "text": "这个机器上是不是跑着一个 autoevaluator manager"}]}},
    ])
    # skips synthetic + "你好" → the real intent
    assert codex._extract_first_user_input(f) == "这个机器上是不是跑着一个 autoevaluator manager"


def test_codex_title_skips_single_char_and_digits(tmp_path):
    f = tmp_path / "r.jsonl"
    _write_jsonl(f, [
        {"type": "response_item", "payload": {"type": "message", "role": "user",
         "content": [{"type": "input_text", "text": "1"}]}},
        {"type": "response_item", "payload": {"type": "message", "role": "user",
         "content": [{"type": "input_text", "text": "查找 19091 端口的进程并给我重启命令"}]}},
    ])
    assert codex._extract_first_user_input(f) == "查找 19091 端口的进程并给我重启命令"


def test_codex_title_compresses_long_path(tmp_path):
    f = tmp_path / "r.jsonl"
    long = "看下这个里面 /srv/data/project/alpha/beta/gamma/delta/20260611"
    _write_jsonl(f, [
        {"type": "response_item", "payload": {"type": "message", "role": "user",
         "content": [{"type": "input_text", "text": long}]}},
    ])
    title = codex._extract_first_user_input(f)
    assert "看下这个里面" in title
    assert "…/20260611" in title and "/srv/data/project/alpha" not in title


def test_codex_title_falls_back_when_all_low_info(tmp_path):
    """If the user only ever said '你好', don't drop the title to empty — keep it."""
    f = tmp_path / "r.jsonl"
    _write_jsonl(f, [
        {"type": "response_item", "payload": {"type": "message", "role": "user",
         "content": [{"type": "input_text", "text": "你好"}]}},
    ])
    assert codex._extract_first_user_input(f) == "你好"


def test_codex_low_info_helper():
    assert codex._low_info("1") and codex._low_info("a") and codex._low_info("你好")
    assert codex._low_info("ok") and codex._low_info("  hi  ") and codex._low_info("123")
    assert not codex._low_info("重启服务") and not codex._low_info("这个机器上是不是跑着")
