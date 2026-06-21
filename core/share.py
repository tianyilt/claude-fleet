"""Render a Claude/Codex session timeline to a self-contained, read-only HTML page
for sharing (issue #4). No CDN/JS — inline CSS only, so it works offline and embeds
cleanly in a wiki / Feishu doc. Optional generic redaction of secrets/PII.
"""
from __future__ import annotations

import html
import json

from . import codex, transcripts
from .redaction import redact as _redact
from .sessions import PROJECTS_DIR

_CSS = """
:root{color-scheme:light}
*{box-sizing:border-box}
body{margin:0;background:#f5f0e8;color:#1f2937;font:14px/1.6 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif}
.wrap{max-width:880px;margin:0 auto;padding:24px}
header{background:#fff;border:1px solid #e5e7eb;border-radius:14px;padding:18px 20px;margin-bottom:18px}
header h1{margin:0 0 6px;font-size:18px}
.meta{font-size:12px;color:#6b7280;display:flex;flex-wrap:wrap;gap:6px 14px}
.meta code{background:#f3f4f6;border-radius:4px;padding:1px 5px}
.tags{margin-top:8px;display:flex;flex-wrap:wrap;gap:6px}
.tag{background:#eef2ff;color:#4338ca;border-radius:5px;padding:1px 7px;font-size:11px}
.ev{border-left:3px solid #d1d5db;padding:6px 0 6px 12px;margin:10px 0}
.ev .h{font-size:11px;color:#9ca3af;margin-bottom:3px;text-transform:uppercase;letter-spacing:.04em}
.ev pre{margin:0;white-space:pre-wrap;word-break:break-word;font:13px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace;background:#fff;border:1px solid #eee;border-radius:8px;padding:8px 10px}
.user{border-color:#60a5fa}.assistant{border-color:#34d399}.tool_use{border-color:#fbbf24}
.tool_result{border-color:#d1d5db}.skill_invoke{border-color:#a855f7}.system{border-color:#e5e7eb}
.memory_read{border-color:#38bdf8}.memory_write{border-color:#ec4899}
details summary{cursor:pointer;color:#6b7280;font-size:12px}
.foot{margin-top:22px;color:#9ca3af;font-size:11px;text-align:center}
.redacted-note{background:#fef3c7;color:#92400e;border-radius:8px;padding:6px 10px;font-size:12px;margin-bottom:14px}
"""


def _meta_from_transcript(path) -> dict:
    """First-line cwd + session id from the transcript."""
    for d in transcripts._iter_lines(path):
        return {"cwd": d.get("cwd", ""), "session_id": d.get("sessionId", "")}
    return {"cwd": "", "session_id": ""}


def _esc(s: str, redact: bool) -> str:
    s = s or ""
    if redact:
        s = _redact(s)
    return html.escape(s)


def render_session_html(session_id: str, redact: bool = False) -> tuple[str, str]:
    """Return (title, html) for a session, or raise FileNotFoundError.

    Works for both Claude and Codex transcripts — Codex rollouts live under
    ~/.codex/sessions and use a different JSONL schema, so we dispatch on which
    store the session id is found in.
    """
    path = transcripts.find_transcript_path(session_id)
    if path is not None:
        meta = _meta_from_transcript(path)
        events = transcripts.timeline(str(path), limit=5000)
        skills = transcripts.extract_skills_used(str(path))
    else:
        path = codex.find_codex_transcript_path(session_id)
        if path is None:
            raise FileNotFoundError(f"no transcript for session {session_id}")
        meta = codex.codex_meta(path)
        events = codex.codex_timeline(str(path), limit=5000)
        skills = codex.extract_codex_session_activity(path)["skills_used"]

    cwd = meta.get("cwd", "")
    project = cwd.rsplit("/", 1)[-1] if cwd else session_id[:8]
    title = f"{project} · {session_id[:8]}"

    parts: list[str] = []
    for ev in events:
        kind = ev.get("kind", "system")
        head = ev.get("tool") or kind
        ts = (ev.get("ts") or "")[:19]
        # tool_use: show the structured args; others: the text body
        if kind == "tool_use":
            body = "\n".join(f"{k}={v}" for k, v in (ev.get("extra") or {}).items())
        else:
            body = ev.get("text") or ""
        body_html = _esc(body, redact)
        block = (f'<div class="ev {html.escape(kind)}"><div class="h">'
                 f'{html.escape(str(head))} · {html.escape(ts)}</div>')
        if kind == "tool_result" and len(body) > 200:
            block += f'<details><summary>tool result ({len(body)} chars)</summary><pre>{body_html}</pre></details>'
        elif body_html:
            block += f'<pre>{body_html}</pre>'
        block += '</div>'
        parts.append(block)

    redactnote = ('<div class="redacted-note">🔒 Sensitive values (emails, keys, '
                  'tokens, home paths…) have been redacted.</div>') if redact else ''
    tags = "".join(f'<span class="tag">{html.escape(s)}</span>' for s in skills)
    cwd_disp = _esc(cwd, redact)
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)} — Claude Fleet</title>
<style>{_CSS}</style></head>
<body><div class="wrap">
<header>
  <h1>{html.escape(title)}</h1>
  <div class="meta">
    <span>project: <code>{cwd_disp or '—'}</code></span>
    <span>session: <code>{html.escape(session_id)}</code></span>
    <span>{len(events)} events</span>
  </div>
  {f'<div class="tags">{tags}</div>' if tags else ''}
</header>
{redactnote}
{''.join(parts)}
<div class="foot">Shared via Claude Fleet · read-only</div>
</div></body></html>"""
    return title, page
