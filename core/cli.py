"""claude-fleet CLI — find, inspect, and resume past sessions from the terminal.

The headline workflow: you remember *what* you worked on but not *where* —
`claude-fleet search <topic>` greps every session (local Claude/Codex/OpenCode
plus registered remote servers), prints a ranked list with match snippets, and
attaches a ready-to-paste resume/fork command to each hit.

Data-source chain for remote sessions, cheapest first:
1. a running dashboard (http://127.0.0.1:7878) — its poller already holds every
   remote's recent history, so one HTTP call avoids any SSH;
2. `--deep` (or no dashboard): pipe the collector's search mode over SSH for a
   real full-text grep of the remote transcripts (the dashboard cache only knows
   first_input-level metadata);
3. the local index (core.history) always answers for local sessions.

Read-only like the rest of claude-fleet: nothing here mutates agent state.
`resume` prints the command by default; `--launch` opens a terminal window.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

# exit codes: 0 = hits / ok · 1 = no results · 2 = error / bad usage
EXIT_OK, EXIT_EMPTY, EXIT_ERROR = 0, 1, 2

_SNIPPET_MAX = 160
_FIRST_INPUT_MAX = 120


# ---------- dashboard reuse (avoid SSH when the poller already did it) ----------

def _dashboard_url() -> str:
    port = os.environ.get("CLAUDE_FLEET_PORT", "7878")
    return f"http://127.0.0.1:{port}"


def _dashboard_get(path: str, params: dict | None = None, timeout: float = 2.0) -> dict | None:
    """GET a dashboard API endpoint; None when it isn't running (degrade signal)."""
    url = _dashboard_url() + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None


# ---------- row helpers ----------

def _row_key(row: dict) -> tuple:
    return (row.get("source", "local"), row.get("session_id", ""))


def _merge_row(base: dict, extra: dict) -> dict:
    """Fold a second sighting of the same session into `base` (richer wins)."""
    snips = list(base.get("match_snippets") or [])
    for s in extra.get("match_snippets") or []:
        if s not in snips:
            snips.append(s)
    base["match_snippets"] = snips[:5]
    for k in ("first_input", "project", "project_name", "env_path", "first_ts"):
        if not base.get(k) and extra.get(k):
            base[k] = extra[k]
    return base


def _from_remote_match(m: dict, source: str, env_path: str) -> dict:
    return {
        "session_id": m.get("session_id", ""),
        "platform": m.get("platform", "codex"),
        "source": source,
        "project": m.get("project", ""),
        "project_name": m.get("project_name", "") or source,
        "first_input": m.get("first_input", ""),
        "first_ts": m.get("first_ts", ""),
        "transcript_path": m.get("transcript_path"),
        "transcript_mtime": m.get("transcript_mtime", 0),
        "match_snippets": m.get("snippets", []),
        "env_path": env_path,
        "origin": "ssh",
    }


def _resume_command(row: dict, fork: bool = False) -> str:
    """The paste-ready command for this session, local or remote. '' when the
    platform can't be resumed from a CLI (opencode)."""
    from . import remote as _remote, terminal as _terminal
    platform = row.get("platform", "claude")
    sid = row.get("session_id", "")
    cwd = row.get("project") or ""
    source = row.get("source", "local")
    try:
        if source and source != "local":
            ssh = _remote.ssh_for(source)
            if not ssh:
                return ""
            return _terminal.remote_session_command(
                ssh, platform, sid, cwd, fork=fork,
                env_path=row.get("env_path", "") or _remote.resume_path(source))
        return _terminal.session_cli_command(platform, sid, cwd, fork=fork)
    except ValueError:
        return ""


def _fmt_ts(row: dict) -> str:
    import datetime
    mt = row.get("transcript_mtime") or 0
    if mt:
        try:
            return datetime.datetime.fromtimestamp(mt / 1000).strftime("%Y-%m-%d %H:%M")
        except (ValueError, OSError, OverflowError):
            pass
    return (row.get("first_ts") or row.get("last_ts") or "")[:16].replace("T", " ")


def _one_line(text: str, cap: int) -> str:
    t = " ".join((text or "").split())
    return t[:cap] + ("…" if len(t) > cap else "")


def _print_hits(rows: list[dict], as_json: bool, query: str = "",
                verbose: bool = False) -> None:
    if as_json:
        out = []
        for r in rows:
            d = dict(r)
            d["resume_command"] = _resume_command(r)
            d["fork_command"] = _resume_command(r, fork=True)
            out.append(d)
        print(json.dumps({"total": len(out), "query": query, "sessions": out},
                         ensure_ascii=False, indent=2))
        return
    if not rows:
        print(f"no sessions matched {query!r}")
        return
    print(f"{len(rows)} session(s) matched {query!r}\n")
    for i, r in enumerate(rows, 1):
        src = r.get("source", "local")
        origin = f" ·{r.get('origin', '')}" if verbose and r.get("origin") else ""
        print(f"{i:3}. [{r.get('platform', '?')} @ {src}{origin}] "
              f"{r.get('session_id', '')}  {_fmt_ts(r)}")
        proj = r.get("project") or r.get("project_name") or ""
        if proj:
            print(f"     dir : {proj}")
        if r.get("first_input"):
            print(f"     first: {_one_line(r['first_input'], _FIRST_INPUT_MAX)}")
        for s in (r.get("match_snippets") or [])[:2]:
            print(f"     match: {_one_line(s, _SNIPPET_MAX)}")
        cmd = _resume_command(r)
        if cmd:
            print(f"     resume: {cmd}")
        print()


# ---------- search ----------

def _local_rows(query: str, limit: int, runtime: str | None) -> list[dict]:
    from . import history
    data = history.list_sessions(q=query, limit=limit, platform=runtime or None)
    rows = []
    for s in data.get("sessions", []):
        s.setdefault("source", "local")
        s["origin"] = "index"
        if isinstance(s.get("metrics"), dict):
            s["metrics"].pop("requests", None)
        rows.append(s)
    return rows


def _resolve_targets(args) -> list[dict]:
    from . import remote as _remote
    if getattr(args, "local_only", False):
        return []
    remotes = _remote.load_remotes()
    if getattr(args, "remote", None):
        wanted = set(args.remote)
        targets = [r for r in remotes if r["name"] in wanted]
        missing = wanted - {r["name"] for r in targets}
        if missing:
            raise SystemExit(f"error: unregistered remote(s): {', '.join(sorted(missing))} "
                             f"(see: claude-fleet remotes list)")
        return targets
    if getattr(args, "all_remotes", False) or getattr(args, "deep", False):
        return remotes
    return []


def cmd_search(args) -> int:
    from . import remote as _remote
    query = args.query
    fetch = max(args.limit * 3, 60)
    merged: dict[tuple, dict] = {}

    # 1) dashboard (one HTTP call = local rg + every remote's cached metadata;
    #    covers --local-only too since local rows ride along)
    http_data = _dashboard_get("/api/history", {"q": query, "limit": fetch})
    if http_data:
        for s in http_data.get("sessions", []):
            s.setdefault("source", "local")
            s["origin"] = "http"
            if args.local_only and s["source"] != "local":
                continue
            merged[_row_key(s)] = s
    else:
        for s in _local_rows(query, fetch, None):
            merged[_row_key(s)] = s

    # 2) deep SSH full-text (also the only remote path when no dashboard runs)
    try:
        targets = _resolve_targets(args)
    except SystemExit as e:
        print(e, file=sys.stderr)
        return EXIT_ERROR
    if targets and (args.deep or http_data is None):
        def _one(r):
            try:
                return r["name"], _remote.search_remote(r, query, days=args.days), ""
            except Exception as e:
                return r["name"], None, str(e)[:200]
        with ThreadPoolExecutor(max_workers=min(8, len(targets))) as ex:
            for name, data, err in ex.map(_one, targets):
                if data is None:
                    print(f"warn: remote '{name}' search failed: {err}", file=sys.stderr)
                    continue
                env_path = data.get("path", "")
                for m in data.get("matches", []):
                    row = _from_remote_match(m, name, env_path)
                    key = _row_key(row)
                    if key in merged:
                        merged[key] = _merge_row(merged[key], row)
                    else:
                        merged[key] = row

    rows = list(merged.values())
    if args.runtime:
        rows = [r for r in rows if r.get("platform") == args.runtime]
    rows.sort(key=lambda r: r.get("transcript_mtime") or 0, reverse=True)
    rows = rows[:args.limit]
    _print_hits(rows, args.json, query=query, verbose=args.verbose)
    return EXIT_OK if rows else EXIT_EMPTY


# ---------- show / resume ----------

def _find_session(session_id: str, source: str) -> tuple[dict | None, list[dict]]:
    """Locate one session by id (prefix ok). Returns (row, candidates) — row is
    None when zero or >1 sessions matched; candidates carries the ambiguity."""
    if source and source != "local":
        from . import remote as _remote
        ssh = _remote.ssh_for(source)
        if not ssh:
            raise SystemExit(f"error: remote '{source}' is not registered")
        data = _remote.collect({"name": source, "ssh": ssh})
        env_path = data.get("path", "")
        rows = []
        for h in data.get("history", []):
            if h.get("session_id", "").startswith(session_id):
                h = dict(h)
                h["source"] = source
                h["env_path"] = env_path
                rows.append(h)
    else:
        from . import history
        data = history.list_sessions(limit=9999)
        rows = [s for s in data.get("sessions", [])
                if s.get("session_id", "").startswith(session_id)
                and s.get("source", "local") == "local"]
    if len(rows) == 1:
        return rows[0], rows
    return None, rows


def _resolve_or_report(args) -> dict | None:
    try:
        row, candidates = _find_session(args.session_id, args.source)
    except SystemExit as e:
        print(e, file=sys.stderr)
        return None
    if row:
        return row
    if not candidates:
        where = args.source if args.source and args.source != "local" else "the local index"
        print(f"error: no session matching '{args.session_id}' in {where}", file=sys.stderr)
        return None
    print(f"error: '{args.session_id}' is ambiguous ({len(candidates)} matches):",
          file=sys.stderr)
    for c in candidates[:10]:
        print(f"  {c.get('session_id', '')}  [{c.get('platform', '?')}]  "
              f"{_one_line(c.get('first_input', ''), 80)}", file=sys.stderr)
    return None


def _tail_events(row: dict, n: int) -> list[dict]:
    from . import codex as _codex, remote as _remote, transcripts as _tx
    tp = row.get("transcript_path")
    if not tp:
        return []
    source = row.get("source", "local")
    platform = row.get("platform", "claude")
    if source and source != "local":
        tl = _remote.remote_timeline(source, tp, platform, limit=max(n, 10))
        return (tl.get("events") or [])[-n:]
    if platform == "codex":
        return _codex.codex_timeline(tp, limit=max(n, 10))[-n:]
    return _tx.timeline(tp, limit=max(n, 10))[-n:]


def cmd_show(args) -> int:
    row = _resolve_or_report(args)
    if row is None:
        return EXIT_ERROR
    events: list[dict] = []
    tail_err = ""
    if args.tail > 0:
        try:
            events = _tail_events(row, args.tail)
        except Exception as e:
            tail_err = str(e)[:200]
    if args.json:
        d = dict(row)
        d["resume_command"] = _resume_command(row)
        d["fork_command"] = _resume_command(row, fork=True)
        d["tail"] = events
        if tail_err:
            d["tail_error"] = tail_err
        print(json.dumps(d, ensure_ascii=False, indent=2))
        return EXIT_OK
    metrics = row.get("metrics") or {}
    tokens = (metrics.get("tokens") or {}).get("total")
    print(f"session : {row.get('session_id', '')}")
    print(f"platform: {row.get('platform', '?')} @ {row.get('source', 'local')}")
    print(f"when    : {_fmt_ts(row)}")
    if row.get("project"):
        print(f"dir     : {row['project']}")
    if row.get("first_input"):
        print(f"first   : {_one_line(row['first_input'], 300)}")
    if row.get("plan_title"):
        print(f"plan    : {row['plan_title']}")
    if row.get("skills_used"):
        print(f"skills  : {', '.join(row['skills_used'])}")
    if metrics.get("model") or row.get("model"):
        print(f"model   : {row.get('model') or metrics.get('model')}")
    if tokens:
        extra = f" · ${metrics['cost_usd']:.2f}" if metrics.get("cost_usd") else ""
        print(f"usage   : {tokens:,} tokens · {metrics.get('turns', 0)} turns{extra}")
    cmd = _resume_command(row)
    if cmd:
        print(f"resume  : {cmd}")
        print(f"fork    : {_resume_command(row, fork=True)}")
    if tail_err:
        print(f"tail    : unavailable ({tail_err})")
    elif events:
        print(f"\nlast {len(events)} event(s):")
        for e in events:
            ts = (e.get("ts") or "")[:19].replace("T", " ")
            kind = e.get("kind") or e.get("role") or "?"
            text = _one_line(e.get("text") or "", 140)
            tool = f" [{e['tool']}]" if e.get("tool") else ""
            print(f"  {ts}  {kind}{tool}: {text}")
    return EXIT_OK


def cmd_resume(args) -> int:
    row = _resolve_or_report(args)
    if row is None:
        return EXIT_ERROR
    cmd = _resume_command(row, fork=args.fork)
    if not cmd:
        print(f"error: {row.get('platform', '?')} sessions can't be resumed from a CLI",
              file=sys.stderr)
        return EXIT_ERROR
    if not args.launch:
        print(cmd)
        return EXIT_OK
    from pathlib import Path
    from . import remote as _remote, terminal as _terminal
    source = row.get("source", "local")
    cwd = row.get("project") or ""
    if source == "local" and cwd and not Path(cwd).is_dir():
        print(f"error: project directory not found: {cwd}", file=sys.stderr)
        return EXIT_ERROR
    ssh = _remote.ssh_for(source) if source != "local" else None
    result = _terminal.launch_session(
        row.get("platform", "claude"), row.get("session_id", ""), cwd,
        fork=args.fork, ssh=ssh,
        env_path=row.get("env_path", "") if ssh else "")
    if result.get("ok"):
        print(f"{result.get('action', 'launched')}: {row.get('session_id', '')}")
        return EXIT_OK
    print(f"error: {result.get('error', 'launch failed')}", file=sys.stderr)
    if result.get("command"):
        print(result["command"])   # still give the paste-ready fallback
    return EXIT_ERROR


# ---------- handoff (live session → another frontend, e.g. an Orca pane) ----------

def cmd_handoff(args) -> int:
    from . import actions, sessions
    wins = sessions.list_windows(include_dead=False)
    token = (args.session_id or "").strip()

    if not token:
        if not wins:
            print("no live local sessions")
            return EXIT_EMPTY
        print(f"{len(wins)} live session(s) — handoff takes a session-id prefix or pid:\n")
        for w in wins:
            print(f"  {w.session_id[:12]}  pid={w.pid:<7} {w.status:<8} "
                  f"{_one_line(w.name or w.project_name or '', 60)}")
        return EXIT_OK

    if token.isdigit():
        matches = [w for w in wins if w.pid == int(token)]
    else:
        matches = [w for w in wins if w.session_id.startswith(token)]
    if not matches:
        print(f"error: no LIVE local session matching '{token}' "
              f"(handoff only targets running local sessions; for history use "
              f"`claude-fleet resume`, for remotes use `--source <name>` there)",
              file=sys.stderr)
        return EXIT_ERROR
    if len(matches) > 1:
        print(f"error: '{token}' is ambiguous ({len(matches)} live sessions):", file=sys.stderr)
        for w in matches:
            print(f"  {w.session_id}  pid={w.pid}  {w.status}", file=sys.stderr)
        return EXIT_ERROR

    w = matches[0]
    result = actions.handoff_session(w.pid, force=args.force)
    if not result.get("ok"):
        print(f"error: {result.get('error', 'handoff failed')}", file=sys.stderr)
        return EXIT_ERROR
    print(f"handed off: {result['session_id']}  ({_one_line(result.get('name') or '', 60)})")
    print(f"resume : {result['resume_command']}")
    if result.get("copied"):
        print("copied to clipboard — paste into an Orca pane, or hit resume in Orca's AI Vault")
    else:
        print("paste into an Orca pane, or hit resume in Orca's AI Vault")
    return EXIT_OK


# ---------- live board ----------

def cmd_live(args) -> int:
    from . import live
    return live.run(interval=max(0.5, args.interval), once=args.once)


# ---------- remotes ----------

def cmd_remotes(args) -> int:
    from . import remote as _remote
    if args.remotes_cmd == "add":
        _remote.add_remote(args.name, args.ssh)
        print(f"registered '{args.name}' → {args.ssh}")
        return EXIT_OK
    if args.remotes_cmd == "remove":
        _remote.remove_remote(args.name)
        print(f"removed '{args.name}'")
        return EXIT_OK
    if args.remotes_cmd == "check":
        remotes = _remote.load_remotes()
        if args.name:
            remotes = [r for r in remotes if r["name"] == args.name]
            if not remotes:
                print(f"error: remote '{args.name}' is not registered", file=sys.stderr)
                return EXIT_ERROR
        ok = True
        for r in remotes:
            try:
                data = _remote.collect(r)
                print(f"{r['name']}: ok · {len(data.get('windows', []))} live window(s) · "
                      f"{len(data.get('history', []))} recent session(s)")
            except Exception as e:
                ok = False
                print(f"{r['name']}: FAILED · {str(e)[:200]}")
        return EXIT_OK if ok else EXIT_ERROR
    # list (default)
    remotes = _remote.load_remotes()
    dash = _dashboard_get("/api/remotes")
    print(f"dashboard: {'running at ' + _dashboard_url() if dash is not None else 'not running'}")
    if not remotes:
        print("no remotes registered (claude-fleet remotes add <name> '<ssh cmd>')")
        return EXIT_EMPTY
    for r in remotes:
        print(f"  {r['name']}: {r['ssh']}")
    return EXIT_OK


# ---------- entry ----------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="claude-fleet",
        description="Search, inspect, and resume Claude/Codex sessions "
                    "(local + registered remote servers).")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("search", help="full-text search past sessions")
    sp.add_argument("query")
    sp.add_argument("--remote", action="append", metavar="NAME",
                    help="search this registered remote (repeatable)")
    sp.add_argument("--all-remotes", action="store_true", help="search every registered remote")
    sp.add_argument("--local-only", action="store_true", help="skip remotes entirely")
    sp.add_argument("--deep", action="store_true",
                    help="force SSH full-text search of remotes (default: reuse the "
                         "running dashboard's cache, which is metadata-only)")
    sp.add_argument("--runtime", choices=["claude", "codex", "opencode"])
    sp.add_argument("--days", type=int, default=90,
                    help="remote search window in days (default 90)")
    sp.add_argument("--limit", type=int, default=20)
    sp.add_argument("--json", action="store_true")
    sp.add_argument("--verbose", action="store_true",
                    help="annotate each hit with its data source (http/ssh/index)")
    sp.set_defaults(func=cmd_search)

    sh = sub.add_parser("show", help="one session's summary card (+ last events)")
    sh.add_argument("session_id", help="session id (unique prefix ok)")
    sh.add_argument("--source", default="local", help="'local' or a remote name")
    sh.add_argument("--tail", type=int, default=10, help="trailing timeline events (0 = off)")
    sh.add_argument("--json", action="store_true")
    sh.set_defaults(func=cmd_show)

    rp = sub.add_parser("resume", help="print (or --launch) the resume command")
    rp.add_argument("session_id", help="session id (unique prefix ok)")
    rp.add_argument("--source", default="local", help="'local' or a remote name")
    rp.add_argument("--fork", action="store_true", help="fork instead of resume")
    rp.add_argument("--launch", action="store_true", help="open a terminal window now")
    rp.set_defaults(func=cmd_resume)

    hp = sub.add_parser(
        "handoff",
        help="gracefully stop a LIVE local session and get its resume command "
             "(for picking it up in another frontend, e.g. an Orca pane)")
    hp.add_argument("session_id", nargs="?", default="",
                    help="session id prefix or pid (omit to list live sessions)")
    hp.add_argument("--force", action="store_true",
                    help="hand off even while the session is busy (drops the in-flight turn)")
    hp.set_defaults(func=cmd_handoff)

    lv = sub.add_parser(
        "live",
        help="live board of running sessions with one-key handoff "
             "(built to run inside an Orca pane; read-only without a tty)")
    lv.add_argument("--interval", type=float, default=2.0,
                    help="refresh interval in seconds (default 2)")
    lv.add_argument("--once", action="store_true", help="print one snapshot and exit")
    lv.set_defaults(func=cmd_live)

    rm = sub.add_parser("remotes", help="manage registered remote servers")
    rmsub = rm.add_subparsers(dest="remotes_cmd")
    rmsub.add_parser("list", help="registered remotes + dashboard status")
    ra = rmsub.add_parser("add", help="register a remote")
    ra.add_argument("name")
    ra.add_argument("ssh", help="full ssh prefix, e.g. 'ssh -p 2222 user@host'")
    rr = rmsub.add_parser("remove", help="deregister a remote")
    rr.add_argument("name")
    rc = rmsub.add_parser("check", help="SSH-probe remotes right now")
    rc.add_argument("name", nargs="?", default="")
    rm.set_defaults(func=cmd_remotes, remotes_cmd=None)

    return p


def main(argv: list[str] | None = None) -> int:
    # Session titles / snippets are routinely CJK; never let a cp936/cp1252
    # console codec crash the print (Windows).
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass
    args = _build_parser().parse_args(argv)
    if getattr(args, "cmd", None) == "remotes" and not args.remotes_cmd:
        args.remotes_cmd = "list"
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return EXIT_ERROR
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
