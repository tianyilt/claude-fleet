"""CLI (core/cli.py) — search/show/resume against a monkeypatched index.

No ripgrep, no SSH, no dashboard: the local index and remote layers are faked at
module boundaries (history.list_sessions / remote.collect / cli._dashboard_get),
mirroring how test_api.py isolates the HTTP layer.
"""
import json

import pytest

import core.cli as cli
import core.history as history
import core.remote as remote


def _row(**over):
    base = {
        "session_id": "aaaa1111-2222-3333-4444-555566667777",
        "platform": "claude",
        "source": "local",
        "project": "/work/proj",
        "project_name": "proj",
        "first_input": "fix the caption compare dashboard",
        "first_ts": "2026-05-12T01:00:00Z",
        "transcript_path": "/home/x/.claude/projects/-work-proj/aaaa.jsonl",
        "transcript_mtime": 1770000000000,
        "match_snippets": ["…caption_compare aggregate.csv…"],
        "metrics": {},
    }
    base.update(over)
    return base


@pytest.fixture(autouse=True)
def _no_dashboard(monkeypatch):
    """Default every test to 'dashboard not running'; opt back in per-test."""
    monkeypatch.setattr(cli, "_dashboard_get", lambda *a, **k: None)


def test_search_local_hit_exit0(monkeypatch, capsys):
    monkeypatch.setattr(history, "list_sessions",
                        lambda **kw: {"total": 1, "sessions": [_row()]})
    rc = cli.main(["search", "caption", "--local-only"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "aaaa1111" in out
    assert "claude --resume aaaa1111-2222-3333-4444-555566667777" in out
    assert "caption_compare aggregate.csv" in out


def test_search_no_hit_exit1(monkeypatch, capsys):
    monkeypatch.setattr(history, "list_sessions",
                        lambda **kw: {"total": 0, "sessions": []})
    rc = cli.main(["search", "nothing-here", "--local-only"])
    assert rc == 1
    assert "no sessions matched" in capsys.readouterr().out


def test_search_json_output(monkeypatch, capsys):
    monkeypatch.setattr(history, "list_sessions",
                        lambda **kw: {"total": 1, "sessions": [_row()]})
    rc = cli.main(["search", "caption", "--local-only", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert rc == 0 and data["total"] == 1
    sess = data["sessions"][0]
    assert "claude --resume" in sess["resume_command"]
    assert "--fork-session" in sess["fork_command"]


def test_search_prefers_running_dashboard(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_dashboard_get",
                        lambda *a, **k: {"total": 1, "sessions": [_row()]})

    def _boom(**kw):
        raise AssertionError("local index must not be scanned when HTTP answered")
    monkeypatch.setattr(history, "list_sessions", _boom)
    rc = cli.main(["search", "caption", "--local-only"])
    assert rc == 0
    assert "aaaa1111" in capsys.readouterr().out


def test_search_deep_merges_remote_matches(monkeypatch, capsys):
    monkeypatch.setattr(history, "list_sessions",
                        lambda **kw: {"total": 0, "sessions": []})
    monkeypatch.setattr(remote, "load_remotes",
                        lambda: [{"name": "gpu1", "ssh": "ssh -p 2222 root@h"}])
    monkeypatch.setattr(remote, "search_remote", lambda r, q, days=90: {
        "path": "/opt/node/bin",
        "matches": [{"session_id": "cx-99", "platform": "codex",
                     "project": "/data/exp", "project_name": "exp",
                     "first_input": "run the eval", "transcript_mtime": 1770000000001,
                     "transcript_path": "/root/.codex/sessions/x.jsonl",
                     "snippets": ["…caption v2 sweet spot…"]}],
    })
    rc = cli.main(["search", "caption", "--deep"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "codex @ gpu1" in out
    assert "caption v2 sweet spot" in out
    # remote resume command: SSH wrapper + captured toolchain PATH
    assert "ssh -p 2222 root@h -t" in out and "codex resume cx-99" in out


def test_search_unknown_remote_exit2(monkeypatch, capsys):
    monkeypatch.setattr(history, "list_sessions",
                        lambda **kw: {"total": 0, "sessions": []})
    monkeypatch.setattr(remote, "load_remotes", lambda: [])
    rc = cli.main(["search", "x", "--remote", "nope"])
    assert rc == 2
    assert "unregistered" in capsys.readouterr().err


def test_show_prefix_match_and_card(monkeypatch, capsys):
    monkeypatch.setattr(history, "list_sessions",
                        lambda **kw: {"total": 1, "sessions": [_row()]})
    rc = cli.main(["show", "aaaa1111", "--tail", "0"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "platform: claude @ local" in out
    assert "dir     : /work/proj" in out
    assert "resume  :" in out and "fork    :" in out


def test_show_ambiguous_prefix_exit2(monkeypatch, capsys):
    rows = [_row(), _row(session_id="aaaa1111-ffff-3333-4444-555566667777")]
    monkeypatch.setattr(history, "list_sessions",
                        lambda **kw: {"total": 2, "sessions": rows})
    rc = cli.main(["show", "aaaa1111", "--tail", "0"])
    assert rc == 2
    assert "ambiguous" in capsys.readouterr().err


def test_show_missing_session_exit2(monkeypatch, capsys):
    monkeypatch.setattr(history, "list_sessions",
                        lambda **kw: {"total": 0, "sessions": []})
    rc = cli.main(["show", "deadbeef"])
    assert rc == 2
    assert "no session matching" in capsys.readouterr().err


def test_resume_prints_command(monkeypatch, capsys):
    monkeypatch.setattr(history, "list_sessions",
                        lambda **kw: {"total": 1, "sessions": [_row()]})
    rc = cli.main(["resume", "aaaa1111"])
    out = capsys.readouterr().out.strip()
    assert rc == 0
    assert out.startswith("cd ") and "claude --resume" in out
    assert "--fork-session" not in out


def test_resume_fork_flag(monkeypatch, capsys):
    monkeypatch.setattr(history, "list_sessions",
                        lambda **kw: {"total": 1, "sessions": [_row()]})
    rc = cli.main(["resume", "aaaa1111", "--fork"])
    assert rc == 0
    assert "--fork-session" in capsys.readouterr().out


def test_resume_remote_uses_collect(monkeypatch, capsys):
    monkeypatch.setattr(remote, "ssh_for",
                        lambda name: "ssh -p 2222 user@gpu1" if name == "gpu1" else None)
    monkeypatch.setattr(remote, "collect", lambda r: {
        "path": "/root/.nvm/versions/node/v22/bin",
        "history": [{"session_id": "cx-42", "platform": "codex",
                     "project": "/data/work", "first_input": "train it",
                     "transcript_path": "/root/.codex/sessions/y.jsonl",
                     "transcript_mtime": 1}],
    })
    rc = cli.main(["resume", "cx-42", "--source", "gpu1"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "ssh -p 2222 user@gpu1 -t" in out
    assert "codex resume cx-42" in out
    assert "/root/.nvm/versions/node/v22/bin" in out   # env_path threaded through


def test_remotes_list_empty_exit1(monkeypatch, capsys):
    monkeypatch.setattr(remote, "load_remotes", lambda: [])
    rc = cli.main(["remotes", "list"])
    assert rc == 1
    assert "no remotes registered" in capsys.readouterr().out
