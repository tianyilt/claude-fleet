"""Tests for desktop-notification transition logic (core/notify.py).

Platform-independent: we monkeypatch `_enabled` and `_osascript` so the edge
detection is exercised on any OS/CI.
"""
from core import notify


def _cap(monkeypatch, enabled=True):
    calls = []
    monkeypatch.setattr(notify, "_enabled", lambda: enabled)
    monkeypatch.setattr(notify, "_osascript", lambda t, s, b: calls.append((t, s, b)))
    notify._reset()
    return calls


def _w(sid, triage, name="sess"):
    return {"session_id": sid, "pid": 1, "name": name, "triage": triage, "triage_reason": "r"}


def test_first_snapshot_seeds_without_notifying(monkeypatch):
    calls = _cap(monkeypatch)
    # boot with sessions already waiting / completed — must NOT blast
    notify.notify_transitions([_w("a", "waiting_perm"), _w("b", "completed"), _w("c", "working")])
    assert calls == []


def test_edge_into_waiting_and_completed_fires(monkeypatch):
    calls = _cap(monkeypatch)
    notify.notify_transitions([_w("a", "working"), _w("b", "working")])   # seed
    notify.notify_transitions([_w("a", "waiting_perm"), _w("b", "completed")])
    titles = sorted(t for (t, s, b) in calls)
    assert titles == ["Fleet · 已完成", "Fleet · 需要授权"]


def test_no_refire_while_staying_in_state(monkeypatch):
    calls = _cap(monkeypatch)
    notify.notify_transitions([_w("a", "working")])          # seed
    notify.notify_transitions([_w("a", "completed")])        # fires
    notify.notify_transitions([_w("a", "completed")])        # same state → silent
    assert len(calls) == 1


def test_refire_on_new_permission_after_working(monkeypatch):
    calls = _cap(monkeypatch)
    notify.notify_transitions([_w("a", "working")])          # seed
    notify.notify_transitions([_w("a", "waiting_perm")])     # fire 1
    notify.notify_transitions([_w("a", "working")])          # approved, back to work
    notify.notify_transitions([_w("a", "waiting_perm")])     # next prompt → fire 2
    assert len(calls) == 2


def test_disabled_never_fires(monkeypatch):
    calls = _cap(monkeypatch, enabled=False)
    notify.notify_transitions([_w("a", "working")])
    notify.notify_transitions([_w("a", "waiting_perm")])
    notify.notify_transitions([_w("a", "completed")])
    assert calls == []


def test_working_and_closeable_never_fire(monkeypatch):
    calls = _cap(monkeypatch)
    notify.notify_transitions([_w("a", "idle")])             # seed
    notify.notify_transitions([_w("a", "working")])
    notify.notify_transitions([_w("a", "closeable")])
    assert calls == []
