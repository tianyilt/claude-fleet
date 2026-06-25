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


# ---------- Fix 2a: list_codex_windows maps a live pid -> its rollout ----------

def test_list_codex_windows_matches_pid_cwd(monkeypatch):
    cs = codex.CodexSession(
        session_id="sid-1", project="/work/dir", project_name="dir",
        first_input="play slay the spire", first_ts="", last_ts="",
        transcript_path="/x.jsonl", transcript_size=1, transcript_mtime=1000,
        cli_version="1.0", model_provider="openai", model="gpt-5",
    )
    codex._win_cache.update(pids=None, windows=[], ts=0.0)
    codex._pid_rollout.clear()
    monkeypatch.setattr(codex, "list_codex_sessions", lambda: [cs])
    monkeypatch.setattr(codex, "_running_codex_pids", lambda: [4242])
    monkeypatch.setattr(codex, "_pid_files", lambda pid: ("/work/dir", None))
    monkeypatch.setattr(codex, "_pid_start_epoch", lambda pid: 1.0)
    monkeypatch.setattr("core.sessions.get_tty", lambda pid: "/dev/ttys9")

    ws = codex.list_codex_windows()
    assert len(ws) == 1
    w = ws[0]
    assert w["platform"] == "codex" and w["alive"] is True
    assert w["pid"] == 4242 and w["session_id"] == "sid-1"
    assert w["cwd"] == "/work/dir" and w["tty"] == "/dev/ttys9"
    assert w["name"] == "play slay the spire"


def test_list_codex_windows_unmatched_cwd_still_shows_placeholder(monkeypatch):
    # A running codex process whose cwd has no rollout match is STILL shown (so it's
    # visible + focusable) — as a placeholder card, not dropped.
    cs = codex.CodexSession(
        session_id="sid-1", project="/work/dir", project_name="dir",
        first_input="x", first_ts="", last_ts="", transcript_path="/x.jsonl",
        transcript_size=1, transcript_mtime=1, cli_version="", model_provider="", model="",
    )
    codex._win_cache.update(pids=None, windows=[], ts=0.0)
    codex._pid_rollout.clear()
    monkeypatch.setattr(codex, "list_codex_sessions", lambda: [cs])
    monkeypatch.setattr(codex, "_running_codex_pids", lambda: [1])
    monkeypatch.setattr(codex, "_pid_files", lambda pid: ("/somewhere/else", None))
    monkeypatch.setattr(codex, "_pid_start_epoch", lambda pid: 1.0)
    monkeypatch.setattr("core.sessions.get_tty", lambda pid: "/dev/ttys1")
    ws = codex.list_codex_windows()
    assert len(ws) == 1 and ws[0]["pid"] == 1 and ws[0]["tty"] == "/dev/ttys1"
    assert ws[0]["transcript_path"] is None        # placeholder (no rollout matched)


def test_list_codex_windows_empty_when_no_process(monkeypatch):
    monkeypatch.setattr(codex, "_running_codex_pids", lambda: [])
    assert codex.list_codex_windows() == []


def test_list_codex_windows_one_card_per_pid_same_cwd(monkeypatch):
    # Two codex sessions in the SAME cwd → must NOT collapse to one card.
    cs1 = codex.CodexSession(
        session_id="sid-1", project="/work", project_name="work",
        first_input="hi", first_ts="2026-06-01T10:00:00", last_ts="",
        transcript_path="/r1.jsonl", transcript_size=1, transcript_mtime=2000,
        cli_version="", model_provider="", model="gpt-5")
    cs2 = codex.CodexSession(
        session_id="sid-2", project="/work", project_name="work",
        first_input="bye", first_ts="2026-06-01T11:00:00", last_ts="",
        transcript_path="/r2.jsonl", transcript_size=1, transcript_mtime=1000,
        cli_version="", model_provider="", model="gpt-5")
    codex._win_cache.update(pids=None, windows=[], ts=0.0)
    codex._pid_rollout.clear()
    monkeypatch.setattr(codex, "list_codex_sessions", lambda: [cs1, cs2])
    monkeypatch.setattr(codex, "_running_codex_pids", lambda: [100, 200])
    # no open rollout caught via lsof; cwd resolves to /work for both
    monkeypatch.setattr(codex, "_pid_files", lambda pid: ("/work", None))
    # pid 200 started more recently than 100
    monkeypatch.setattr(codex, "_pid_start_epoch", lambda pid: 200.0 if pid == 200 else 100.0)
    monkeypatch.setattr("core.sessions.get_tty",
                        lambda pid: f"/dev/ttys{pid}")
    ws = codex.list_codex_windows()
    assert len(ws) == 2                                  # one per pid, not collapsed
    assert {w["pid"] for w in ws} == {100, 200}
    assert {w["session_id"] for w in ws} == {"sid-1", "sid-2"}   # distinct rollouts
    assert all(w["tty"] for w in ws)                     # each focusable


def test_list_codex_windows_exact_match_via_open_rollout(monkeypatch):
    cs = codex.CodexSession(
        session_id="sid-A", project="/w", project_name="w", first_input="task",
        first_ts="", last_ts="", transcript_path="/open.jsonl", transcript_size=1,
        transcript_mtime=5, cli_version="", model_provider="", model="gpt-5")
    codex._win_cache.update(pids=None, windows=[], ts=0.0)
    codex._pid_rollout.clear()
    monkeypatch.setattr(codex, "list_codex_sessions", lambda: [cs])
    monkeypatch.setattr(codex, "_running_codex_pids", lambda: [7])
    monkeypatch.setattr(codex, "_pid_files", lambda pid: ("/w", "/open.jsonl"))
    monkeypatch.setattr(codex, "_pid_start_epoch", lambda pid: 1.0)
    monkeypatch.setattr("core.sessions.get_tty", lambda pid: "/dev/ttys7")
    ws = codex.list_codex_windows()
    assert len(ws) == 1 and ws[0]["session_id"] == "sid-A" and ws[0]["tty"] == "/dev/ttys7"


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
