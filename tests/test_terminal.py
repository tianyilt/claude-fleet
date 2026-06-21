"""Tests for platform-dispatched terminal control. No real osascript is invoked."""
import types

import pytest

from core import terminal


class _FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# ---------- automation_hint ----------

@pytest.mark.parametrize("errno", ["-1728", "-2741", "-1743"])
def test_automation_hint_detects_permission_errnos(errno):
    msg = terminal.automation_hint(f"123:45: execution error: blah ({errno})")
    assert msg and "Automation" in msg


def test_automation_hint_detects_not_authorized():
    assert "Automation" in terminal.automation_hint("Not authorized to send Apple events")


def test_automation_hint_clean_is_none():
    assert terminal.automation_hint("") is None
    assert terminal.automation_hint("   ") is None


def test_automation_hint_other_passes_through():
    assert terminal.automation_hint("some other error") == "some other error"


# ---------- launch_session (cross-platform launcher, PR #5) ----------

def test_session_cli_command_claude():
    assert terminal.session_cli_command("claude", "sid-123", "/tmp/project") == \
        "cd /tmp/project && claude --resume sid-123"
    assert terminal.session_cli_command("claude", "sid-123", "/tmp/project", fork=True) == \
        "cd /tmp/project && claude --resume sid-123 --fork-session"


def test_session_cli_command_codex():
    assert terminal.session_cli_command("codex", "sid-123", "/tmp/project") == \
        "cd /tmp/project && codex resume sid-123"
    assert terminal.session_cli_command("codex", "sid-123", "/tmp/project", fork=True) == \
        "cd /tmp/project && codex fork sid-123"


def test_session_cli_command_quotes_cwd():
    cmd = terminal.session_cli_command("claude", "sid", "/tmp/has space")
    assert "'/tmp/has space'" in cmd


def test_launch_no_terminal_returns_command(monkeypatch):
    monkeypatch.setattr(terminal, "_terminal_command", lambda c, w: None)
    r = terminal.launch_session("codex", "sid-123", "/tmp/project")
    assert r["ok"] is False
    assert r["platform"] == "codex"
    assert r["command"] == "cd /tmp/project && codex resume sid-123"


def test_launch_unsupported_platform():
    r = terminal.launch_session("opencode", "sid-123", "/tmp/project")
    assert r["ok"] is False and "cannot be resumed" in r["error"]


def test_launch_success_spawns_detached(monkeypatch):
    monkeypatch.setattr(terminal, "_terminal_command", lambda c, w: ["echo", c])
    calls = {}
    monkeypatch.setattr(terminal.subprocess, "Popen",
                        lambda *a, **k: calls.setdefault("spawned", True))
    r = terminal.launch_session("claude", "sid-123", "/tmp/project", fork=True)
    assert r["ok"] is True and r["action"] == "forked"
    assert r["command"].endswith("--fork-session")
    assert calls.get("spawned") is True


# ---------- keepalive script (Fix 1b: no exec, hold window open on fast exit) ----------

def test_keepalive_script_no_exec():
    # `exec` replaced the shell so a failed resume flash-closed the window before
    # the error could be read. The body must run the command without exec.
    body = terminal._keepalive_script("cd /tmp && claude --resume sid")
    assert "exec " not in body
    assert "claude --resume sid" in body


def test_keepalive_script_holds_open_on_fast_exit():
    body = terminal._keepalive_script("claude --resume bad")
    # sub-3s guard + read keeps the window so "No conversation found" is visible.
    assert "read -r" in body
    assert "-lt 3" in body


# ---------- focus ----------

def test_focus_non_mac_unsupported(monkeypatch):
    monkeypatch.setattr(terminal, "IS_MAC", False)
    r = terminal.focus("/dev/ttys000")
    assert r["ok"] is False and r["unsupported"] is True


def _mac_focus(monkeypatch, tmp_path, returncode, stderr=""):
    monkeypatch.setattr(terminal, "IS_MAC", True)
    script = tmp_path / "focus-tty.sh"
    script.write_text("#!/bin/bash\n")
    # force resolution to our stub script (no user override / bundled lookup)
    monkeypatch.setattr(terminal, "_resolve_focus_script", lambda: script)
    monkeypatch.setattr(terminal.subprocess, "run",
                        lambda *a, **k: _FakeProc(returncode=returncode, stderr=stderr))
    return terminal.focus("/dev/ttys000")


def test_focus_found(monkeypatch, tmp_path):
    assert _mac_focus(monkeypatch, tmp_path, 0)["ok"] is True


def test_focus_permission_denied_maps_to_hint(monkeypatch, tmp_path):
    r = _mac_focus(monkeypatch, tmp_path, 5)
    assert r["ok"] is False and "Automation" in r["error"]


def test_focus_no_tab_not_found(monkeypatch, tmp_path):
    r = _mac_focus(monkeypatch, tmp_path, 4)
    assert r["ok"] is False and r["code"] == 4


def test_focus_unsupported_exit6(monkeypatch, tmp_path):
    r = _mac_focus(monkeypatch, tmp_path, 6)
    assert r["ok"] is False and r["unsupported"] is True


def test_focus_no_script(monkeypatch):
    monkeypatch.setattr(terminal, "IS_MAC", True)
    monkeypatch.setattr(terminal, "_resolve_focus_script", lambda: None)
    r = terminal.focus("/dev/ttys000")
    assert r["ok"] is False and "no focus-tty.sh" in r["error"]
