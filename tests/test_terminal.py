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


# ---------- spawn_window ----------

def test_spawn_window_non_mac_returns_fallback(monkeypatch):
    monkeypatch.setattr(terminal, "IS_MAC", False)

    def boom(*a, **k):
        raise AssertionError("osascript must not be called off macOS")

    monkeypatch.setattr(terminal.subprocess, "run", boom)
    r = terminal.spawn_window("cd /x && claude --resume y")
    assert r["ok"] is False
    assert r["unsupported"] is True
    assert r["fallback_cmd"] == "cd /x && claude --resume y"


def test_spawn_window_mac_success(monkeypatch):
    monkeypatch.setattr(terminal, "IS_MAC", True)
    captured = {}

    def fake_run(args, **k):
        captured["args"] = args
        return _FakeProc(returncode=0, stderr="")

    monkeypatch.setattr(terminal.subprocess, "run", fake_run)
    r = terminal.spawn_window('cd /x && claude --resume "id with spaces"')
    assert r["ok"] is True
    assert r["error"] is None
    assert "fallback_cmd" not in r
    # the inner command is embedded as an escaped AppleScript string literal
    script = captured["args"][-1]
    assert 'write text "cd /x && claude --resume \\"id with spaces\\""' in script


def test_spawn_window_mac_permission_error(monkeypatch):
    monkeypatch.setattr(terminal, "IS_MAC", True)
    monkeypatch.setattr(terminal.subprocess, "run",
                        lambda *a, **k: _FakeProc(returncode=1, stderr="65:71: syntax error (-2741)"))
    inner = "cd /x && claude --resume y"
    r = terminal.spawn_window(inner)
    assert r["ok"] is False
    assert "Automation" in r["error"]
    assert r["fallback_cmd"] == inner


def test_spawn_window_then_cmd_two_writes(monkeypatch):
    monkeypatch.setattr(terminal, "IS_MAC", True)
    captured = {}

    def fake_run(args, **k):
        captured["args"] = args
        return _FakeProc(returncode=0)

    monkeypatch.setattr(terminal.subprocess, "run", fake_run)
    terminal.spawn_window("resume", then_cmd="review", then_delay=3)
    script = captured["args"][-1]
    assert "delay 3" in script
    assert script.count("write text") == 2


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
