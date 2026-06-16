"""Generic secret/PII redaction for shared transcripts (issue #4).

Deliberately GENERIC — this ships in the public repo, so it must contain no
personal or org-specific values. It scrubs common secret/PII shapes from any
user's transcript before it's exported to a shareable page.
"""
from __future__ import annotations

import re

_REDACTED = "[REDACTED]"

# (label, pattern, replacement). Order matters a little; specific before generic.
_PATTERNS: list[tuple[str, re.Pattern, str]] = [
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), _REDACTED),
    ("openai key", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"), _REDACTED),
    ("anthropic key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}\b"), _REDACTED),
    ("github token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), _REDACTED),
    ("aws access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b"), _REDACTED),
    ("google api key", re.compile(r"\bAIza[0-9A-Za-z_-]{16,}\b"), _REDACTED),
    ("slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), _REDACTED),
    ("bearer token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._-]{16,}\b"), "Bearer " + _REDACTED),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"), _REDACTED),
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), _REDACTED),
    ("ipv4", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), _REDACTED),
    # Home-dir usernames: keep the structure, drop the identity.
    ("home path", re.compile(r"(/(?:Users|home))/[^/\s\"']+"), r"\1/<user>"),
    ("windows userprofile", re.compile(r"([A-Za-z]:\\Users\\)[^\\\s\"']+"), r"\1<user>"),
]


def redact(text: str) -> str:
    """Return `text` with common secrets / PII patterns masked."""
    if not text:
        return text
    for _label, pat, repl in _PATTERNS:
        text = pat.sub(repl, text)
    return text
