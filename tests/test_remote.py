"""core/remote.py — registry read/write, SSH collect parsing, source-tagged
merge views, and terminal.remote_session_command. SSH itself is mocked; no
network and no real subprocess to a host."""
import json

import pytest

from core import remote, terminal


@pytest.fixture
def remotes_file(tmp_path, monkeypatch):
    p = tmp_path / "fleet-remotes.json"
    monkeypatch.setattr(remote, "REMOTES_PATH", p)
    remote.CACHE.clear()
    yield p
    remote.CACHE.clear()


# ---------- registry ----------

def test_registry_roundtrip(remotes_file):
    assert remote.load_remotes() == []
    remote.add_remote("srv1", "ssh -p 2222 user@host")
    remote.add_remote("box", "ssh box")
    names = {r["name"] for r in remote.load_remotes()}
    assert names == {"srv1", "box"}
    assert remote.ssh_for("srv1") == "ssh -p 2222 user@host"
    assert remote.ssh_for("nope") is None


def test_add_remote_dedups_by_name(remotes_file):
    remote.add_remote("srv1", "ssh old")
    remote.add_remote("srv1", "ssh new")
    rs = remote.load_remotes()
    assert len(rs) == 1 and rs[0]["ssh"] == "ssh new"


def test_remove_remote(remotes_file):
    remote.add_remote("srv1", "ssh x")
    remote.CACHE["srv1"] = {"ok": True, "windows": [], "history": []}
    remote.remove_remote("srv1")
    assert remote.load_remotes() == []
    assert "srv1" not in remote.CACHE


def test_load_remotes_tolerates_garbage(remotes_file):
    remotes_file.write_text("not json {{{")
    assert remote.load_remotes() == []
    remotes_file.write_text(json.dumps({"remotes": [{"name": "x"}, {"ssh": "y"},
                                                    {"name": "ok", "ssh": "ssh ok"}]}))
    assert remote.load_remotes() == [{"name": "ok", "ssh": "ssh ok"}]


# ---------- collect / poll (mocked SSH) ----------

def _fake_proc(stdout="", stderr="", rc=0):
    class P:
        returncode = rc
    p = P()
    p.stdout = stdout
    p.stderr = stderr
    return p


def test_collect_parses_json(remotes_file, monkeypatch):
    payload = {"home": "/root", "windows": [{"platform": "codex"}], "history": []}
    monkeypatch.setattr(remote.subprocess, "run",
                        lambda *a, **k: _fake_proc(stdout=json.dumps(payload)))
    out = remote.collect({"name": "srv1", "ssh": "ssh -p 2222 user@host"})
    assert out["windows"][0]["platform"] == "codex"


def test_collect_raises_on_ssh_failure(remotes_file, monkeypatch):
    monkeypatch.setattr(remote.subprocess, "run",
                        lambda *a, **k: _fake_proc(stderr="Permission denied", rc=255))
    with pytest.raises(RuntimeError):
        remote.collect({"name": "x", "ssh": "ssh x"})


def test_poll_all_keeps_last_good_on_failure(remotes_file, monkeypatch):
    remote.add_remote("srv1", "ssh x")
    good = {"home": "/root", "windows": [{"platform": "codex", "session_id": "s1",
                                          "cwd": "/r", "updated_at": 0}],
            "history": [{"session_id": "h1", "platform": "codex"}]}
    monkeypatch.setattr(remote, "collect", lambda r: good)
    remote.poll_all()
    assert remote.CACHE["srv1"]["ok"] is True
    assert len(remote.CACHE["srv1"]["windows"]) == 1

    def boom(r):
        raise RuntimeError("tunnel down")
    monkeypatch.setattr(remote, "collect", boom)
    remote.poll_all()
    e = remote.CACHE["srv1"]
    assert e["ok"] is False and "tunnel down" in e["error"]
    assert len(e["windows"]) == 1   # last-good retained


def test_poll_all_drops_deregistered(remotes_file, monkeypatch):
    remote.add_remote("srv1", "ssh x")
    monkeypatch.setattr(remote, "collect",
                        lambda r: {"home": "", "windows": [], "history": []})
    remote.poll_all()
    assert "srv1" in remote.CACHE
    remote.remove_remote("srv1")
    remote.poll_all()
    assert "srv1" not in remote.CACHE


# ---------- merged views are source-tagged ----------

def test_cached_windows_source_tagged(remotes_file):
    remote.CACHE["srv1"] = {"ok": True, "windows": [
        {"platform": "codex", "session_id": "s1", "cwd": "/home/x/repo",
         "first_input": "do thing", "updated_at": 0, "transcript_path": "/r.jsonl"}],
        "history": []}
    wins = remote.cached_windows()
    assert len(wins) == 1
    w = wins[0]
    assert w["source"] == "srv1"
    assert w["tty"] is None          # remote tty is never locally focusable
    assert w["project_name"] == "repo"
    assert w["triage"] in ("working", "idle")


def test_cached_history_source_tagged(remotes_file):
    remote.CACHE["srv1"] = {"ok": True, "windows": [], "history": [
        {"session_id": "h1", "platform": "codex", "first_input": "x",
         "transcript_path": "/r.jsonl", "project_name": "repo"}]}
    rows = remote.cached_history()
    assert len(rows) == 1 and rows[0]["source"] == "srv1"
    assert rows[0]["is_alive"] is False


def test_status_shape(remotes_file):
    remote.add_remote("srv1", "ssh x")
    remote.CACHE["srv1"] = {"ok": True, "error": "", "ts": 1,
                            "windows": [{}, {}], "history": [{}]}
    st = remote.status()
    assert st[0]["name"] == "srv1" and st[0]["windows"] == 2 and st[0]["history"] == 1


# ---------- remote resume command ----------

def test_remote_session_command_codex():
    cmd = terminal.remote_session_command("ssh -p 2222 user@host", "codex",
                                          "abc", "/home/x/repo")
    assert cmd.startswith("ssh -p 2222 user@host -t ")
    assert "codex resume abc" in cmd
    assert "/home/x/repo" in cmd


def test_remote_session_command_claude_fork():
    cmd = terminal.remote_session_command("ssh box", "claude", "abc", "/r", fork=True)
    assert "claude --resume abc --fork-session" in cmd
    assert cmd.startswith("ssh box -t ")


def test_remote_session_command_exports_env_path():
    """The non-interactive SSH shell lacks codex on PATH; the resume command must
    prepend the captured remote PATH so the binary resolves."""
    cmd = terminal.remote_session_command("ssh box", "codex", "abc", "/r",
                                          env_path="/root/.nvm/x/bin:/usr/bin")
    assert "export PATH=/root/.nvm/x/bin:/usr/bin:$PATH &&" in cmd
    assert "codex resume abc" in cmd


def test_resume_path_reads_cache(remotes_file):
    remote.CACHE["srv1"] = {"ok": True, "path": "/root/bin:/usr/bin",
                            "windows": [], "history": []}
    assert remote.resume_path("srv1") == "/root/bin:/usr/bin"
    assert remote.resume_path("missing") == ""


def test_collect_carries_path(remotes_file, monkeypatch):
    remote.add_remote("srv1", "ssh x")
    monkeypatch.setattr(remote, "collect",
                        lambda r: {"home": "/root", "path": "/root/bin", "windows": [], "history": []})
    remote.poll_all()
    assert remote.CACHE["srv1"]["path"] == "/root/bin"


def test_launch_session_remote_uses_ssh(monkeypatch):
    seen = {}
    monkeypatch.setattr(terminal, "_terminal_command",
                        lambda command, cwd: seen.update(command=command) or None)
    r = terminal.launch_session("codex", "abc", "/r", ssh="ssh box")
    # no launcher → ok False, but the SSH command is what we built
    assert "ssh box -t" in seen["command"]
