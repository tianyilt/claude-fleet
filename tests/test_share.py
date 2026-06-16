"""Tests for web-share rendering + generic redaction + endpoints (#4)."""
import json

import pytest
from fastapi.testclient import TestClient

import app as app_module
from core import redaction, share, transcripts

client = TestClient(app_module.app)


# ---------- redaction (generic, must not contain personal values) ----------

def test_redaction_masks_common_secrets():
    cases = {
        "alice@example.com": "[REDACTED]",
        "sk-ABCDEFGHIJKLMNOPQRSTUV": "[REDACTED]",
        "ghp_" + "A" * 36: "[REDACTED]",
        "AKIA" + "B" * 16: "[REDACTED]",
        "10.1.2.3": "[REDACTED]",
    }
    for raw, expect in cases.items():
        assert expect in redaction.redact(f"x {raw} y"), raw


def test_redaction_keeps_path_structure_drops_user():
    out = redaction.redact("/Users/somebody/code /home/bob/x")
    assert "/Users/<user>/code" in out and "/home/<user>/x" in out
    assert "somebody" not in out and "bob" not in out


# (The "module contains no personal/org values" guarantee is enforced repo-wide
#  by scripts/secrets-audit.py, so no meta-test that would itself embed those
#  literal values is needed here.)


# ---------- render ----------

def _transcript(tmp_path):
    lines = [
        {"type": "user", "uuid": "u1", "sessionId": "s", "cwd": "/Users/bob/proj",
         "message": {"role": "user", "content": "my key is sk-SECRETSECRETSECRET123"}},
        {"type": "assistant", "uuid": "u2", "sessionId": "s",
         "message": {"role": "assistant", "content": "noted"}},
    ]
    f = tmp_path / "s.jsonl"
    f.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    return f


def test_render_redacted_hides_secret(tmp_path, monkeypatch):
    f = _transcript(tmp_path)
    monkeypatch.setattr(transcripts, "find_transcript_path", lambda sid: f)
    title, html = share.render_session_html("s", redact=True)
    assert "sk-SECRETSECRETSECRET123" not in html
    assert "REDACTED" in html
    assert "/Users/bob" not in html  # home path username scrubbed


def test_render_unredacted_keeps_text(tmp_path, monkeypatch):
    f = _transcript(tmp_path)
    monkeypatch.setattr(transcripts, "find_transcript_path", lambda sid: f)
    title, html = share.render_session_html("s", redact=False)
    assert "noted" in html


def test_render_missing_raises(monkeypatch):
    monkeypatch.setattr(transcripts, "find_transcript_path", lambda sid: None)
    with pytest.raises(FileNotFoundError):
        share.render_session_html("ghost")


# ---------- endpoints ----------

def test_share_endpoint_creates_page(monkeypatch):
    monkeypatch.setattr(app_module.share, "render_session_html",
                        lambda sid, redact=True: ("My Session", "<html>body</html>"))
    r = client.post("/api/history/s7/share?redact=true").json()
    assert r["ok"] is True and r["share_url"].startswith("/share/")
    assert client.get(r["share_url"]).status_code == 200
    assert "body" in client.get(r["share_url"]).text


def test_share_view_404_for_unknown():
    assert client.get("/share/deadbeefdeadbeef").status_code == 404


def test_share_view_rejects_path_traversal():
    assert client.get("/share/..%2f..%2fapp").status_code == 404
