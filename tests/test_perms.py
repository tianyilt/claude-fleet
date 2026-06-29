"""Tests for focus-log permission detection: zh timestamp parsing, approval-vs-input
classification, expiry, and the snapshot's blocked/self-clear waiting_perm logic."""
import time

import app
from core import perms


# ---------- timestamp + kind parsing ----------

def test_parse_zh_timestamp():
    ep = perms._parse_zh_ts("2026年 6月24日 星期三 14时22分53秒 CST")
    assert ep is not None
    import datetime
    assert datetime.datetime.fromtimestamp(ep).hour == 14


def test_parse_zh_timestamp_bad():
    assert perms._parse_zh_ts("not a date") is None


def test_classify_kind():
    assert perms._classify_kind("Bash 需要授权") == "approval"
    assert perms._classify_kind("Claude Code needs your approval for the plan") == "approval"
    assert perms._classify_kind("Claude needs your permission") == "approval"
    assert perms._classify_kind("Claude is waiting for your input") == "input"
    assert perms._classify_kind("something else") == "other"


# ---------- pending_by_tty expiry + kind filter ----------

def _write_log(path, lines):
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _ts(epoch):
    import datetime
    d = datetime.datetime.fromtimestamp(epoch)
    return f"{d.year}年 {d.month}月{d.day}日 星期X {d.hour}时{d.minute}分{d.second}秒 CST"


def test_pending_by_tty_filters_stale_and_input(tmp_path, monkeypatch):
    now = time.time()
    log = tmp_path / "focus.log"
    _write_log(log, [
        f"{_ts(now - 30)} notify: project=p tty=/dev/ttys1 msg=Bash 需要授权",          # fresh approval ✓
        f"{_ts(now - 99999)} notify: project=p tty=/dev/ttys2 msg=Bash 需要授权",        # stale approval ✗
        f"{_ts(now - 10)} notify: project=p tty=/dev/ttys3 msg=Claude is waiting for your input",  # input ✗
    ])
    monkeypatch.setattr(perms, "FOCUS_LOG", log)
    out = perms.pending_by_tty(max_age_sec=900)
    assert set(out) == {"/dev/ttys1"}


def test_pending_by_tty_keeps_latest_per_tty(tmp_path, monkeypatch):
    now = time.time()
    log = tmp_path / "focus.log"
    _write_log(log, [
        f"{_ts(now - 100)} notify: project=p tty=/dev/ttysX msg=Bash 需要授权",
        f"{_ts(now - 5)} notify: project=p tty=/dev/ttysX msg=AskUserQuestion 需要授权",
    ])
    monkeypatch.setattr(perms, "FOCUS_LOG", log)
    out = perms.pending_by_tty(max_age_sec=900)
    assert out["/dev/ttysX"].msg == "AskUserQuestion 需要授权"   # most recent wins


# ---------- snapshot blocked / self-clear ----------

class _W:
    def __init__(self, pid, tty, updated_at, status="idle"):
        self.pid, self.tty, self.updated_at, self.status = pid, tty, updated_at, status
        self.session_id = f"s{pid}"
        self.name = f"w{pid}"
        self.cwd = "/x"
        self.project_name = "x"
        self.project_slug = "x"
        self.waiting_for = None
        self.started_at = 0
        self.version = ""
        self.transcript_path = None
        self.alive = True

    def to_dict(self):
        import dataclasses
        d = {k: getattr(self, k) for k in
             ("pid", "session_id", "cwd", "project_name", "project_slug", "name",
              "status", "waiting_for", "started_at", "updated_at", "version", "tty",
              "transcript_path", "alive")}
        d["idle_seconds"] = 0
        return d


def _patch_snapshot(monkeypatch, windows, perm_map):
    monkeypatch.setattr(app.sessions, "snapshot",
                        lambda: {"windows": [w.to_dict() for w in windows],
                                 "counts": {"total": len(windows), "busy": 0, "waiting": 0, "idle": 0}})
    monkeypatch.setattr(app.perms, "pending_by_tty", lambda *a, **k: perm_map)
    monkeypatch.setattr(app.codex, "list_codex_windows", lambda: [])


def test_snapshot_fresh_approval_is_waiting_perm(monkeypatch):
    now = time.time()
    # event newer than the window's last activity → blocked
    ev = perms.PermEvent("p", "/dev/ttysA", "Bash 需要授权", "", now, "approval")
    w = _W(pid=1, tty="/dev/ttysA", updated_at=int((now - 60) * 1000))
    _patch_snapshot(monkeypatch, [w], {"/dev/ttysA": ev})
    snap = app._build_enriched_snapshot()
    win = snap["windows"][0]
    assert win["triage"] == "waiting_perm" and win["permission_msg"] == "Bash 需要授权"


def test_snapshot_self_clears_when_session_advanced(monkeypatch):
    now = time.time()
    # approval happened, but the session has since advanced (updated_at AFTER event)
    ev = perms.PermEvent("p", "/dev/ttysA", "Bash 需要授权", "", now - 120, "approval")
    w = _W(pid=1, tty="/dev/ttysA", updated_at=int(now * 1000), status="busy")
    _patch_snapshot(monkeypatch, [w], {"/dev/ttysA": ev})
    snap = app._build_enriched_snapshot()
    win = snap["windows"][0]
    assert win["triage"] != "waiting_perm" and win["permission_msg"] is None
