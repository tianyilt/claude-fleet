#!/usr/bin/env python3
"""
Seed a fake ~/.claude tree for demos, screenshots, and local testing.

Everything here is synthetic — generic "Acme" SaaS engineering work, no real
user data. Point the dashboard at it with CLAUDE_FLEET_HOME:

    python3 fixtures/seed.py                 # populate fixtures/demo-home
    CLAUDE_FLEET_HOME=fixtures/demo-home bash run.sh
    # open http://127.0.0.1:7878

The live "cards" require alive PIDs, so this spawns a few detached `sleep`
processes and records them in fixtures/demo-home/.demo-pids. Clean up with:

    python3 fixtures/seed.py --stop

Re-running reseeds from scratch (and restarts the sleep processes).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEMO = Path(os.environ.get("CLAUDE_FLEET_HOME") or (HERE / "demo-home")).expanduser()
CLAUDE = DEMO / ".claude"
PIDFILE = DEMO / ".demo-pids"

NOW = time.time()
MIN = 60
HOUR = 3600
MODEL = "claude-sonnet-4-6"
DEMO_CWD_ROOT = "/home/dev/acme"
# Paths shown verbatim in the timeline. The parsers only string-match these
# (they never open them), so we keep them generic instead of leaking the real
# demo-home absolute path. The actual files live under CLAUDE_FLEET_HOME.
DISPLAY_CLAUDE = "/home/dev/.claude"


# ---------- transcript event builders ----------

def _ts(secs_ago: float) -> str:
    # ISO-ish; only relative ordering matters to the parsers.
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(NOW - secs_ago)) + "Z"


def user_text(text: str, secs_ago: float) -> dict:
    return {"type": "user", "timestamp": _ts(secs_ago),
            "message": {"role": "user", "content": [{"type": "text", "text": text}]}}


def asst(blocks: list, secs_ago: float, stop_reason: str = "end_turn") -> dict:
    return {"type": "assistant", "timestamp": _ts(secs_ago),
            "message": {"role": "assistant", "model": MODEL,
                        "stop_reason": stop_reason, "content": blocks}}


def t_text(s: str) -> dict:
    return {"type": "text", "text": s}


def t_tool(name: str, inp: dict, tid: str) -> dict:
    return {"type": "tool_use", "id": tid, "name": name, "input": inp}


def tool_result(tid: str, text: str, secs_ago: float) -> dict:
    return {"type": "user", "timestamp": _ts(secs_ago),
            "message": {"role": "user",
                        "content": [{"type": "tool_result", "tool_use_id": tid, "content": text}]}}


def skill_call(skill: str, args: str, tid: str) -> dict:
    return t_tool("Skill", {"skill": skill, "args": args}, tid)


def mem_read(name: str, tid: str) -> dict:
    return t_tool("Read", {"file_path": f"{DISPLAY_CLAUDE}/projects/{SLUG}/memory/{name}.md"}, tid)


def mem_write(name: str, content: str, tid: str) -> dict:
    return t_tool("Write", {"file_path": f"{DISPLAY_CLAUDE}/projects/{SLUG}/memory/{name}.md",
                            "content": content}, tid)


def skill_file_read(skill: str, tid: str) -> dict:
    return t_tool("Read", {"file_path": f"{DISPLAY_CLAUDE}/skills/{skill}/SKILL.md"}, tid)


def bash(cmd: str, tid: str, desc: str = "", bg: bool = False) -> dict:
    inp = {"command": cmd, "description": desc}
    if bg:
        inp["run_in_background"] = True
    return t_tool("Bash", inp, tid)


def plan_write(fname: str, content: str, tid: str) -> dict:
    return t_tool("Write", {"file_path": f"{DISPLAY_CLAUDE}/plans/{fname}", "content": content}, tid)


def plan_edit(fname: str, old: str, new: str, tid: str) -> dict:
    return t_tool("Edit", {"file_path": f"{DISPLAY_CLAUDE}/plans/{fname}",
                           "old_string": old, "new_string": new}, tid)


SLUG = DEMO_CWD_ROOT.replace("/", "-").replace("_", "-").replace(".", "-")  # -home-dev-acme


# ---------- fixture content ----------

SKILLS = {
    "api-scaffold":  "# api-scaffold\n\nScaffold a REST endpoint with router, handler, and tests.\n\nUse when the user asks to add a new API route.\n",
    "db-migrate":    "# db-migrate\n\nGenerate and apply database migrations safely.\n\nTrigger: 'add migration', 'alter table', schema changes.\n",
    "test-runner":   "# test-runner\n\nRun the suite, parse failures, and summarize flaky tests.\n\nUse when the user says 'run tests' or 'why is CI red'.\n",
    "lint-fixer":    "# lint-fixer\n\nApply eslint/ruff autofixes and report what changed.\n\nTrigger: 'fix lint', pre-commit failures.\n",
    "changelog-gen": "# changelog-gen\n\nDraft a CHANGELOG entry from the current diff.\n\nUse when cutting a release.\n",
    "deploy-helper": "# deploy-helper\n\nBuild, tag, and roll out a service to staging.\n\nTrigger: 'deploy to staging'.\n",
}

MEMORIES = {
    "user-profile": ("user", "The team uses TypeScript on the backend (Fastify) and Postgres. Prefers small PRs."),
    "coding-style": ("feedback", "Always add tests with a feature. Keep functions under ~40 lines. **Why:** review speed. **How to apply:** split early."),
    "ci-pipeline": ("project", "CI runs on GitHub Actions; the e2e job is allowed to retry once. Staging deploy is manual."),
    "api-conventions": ("reference", "REST routes are versioned under /v1; errors use RFC-7807 problem+json."),
    "release-process": ("project", "Releases are cut on Thursdays; changelog-gen drafts the notes, a human edits before tagging."),
    "db-guidelines": ("reference", "Migrations are forward-only. Never drop a column in the same release that stops writing it."),
}

# (key, name, status, waitingFor, stop_reason, idle_secs, first_input, blocks_builder)
def live_sessions() -> list[dict]:
    return [
        dict(key="build-auth-api", name="build-auth-api", status="busy", waiting=None,
             stop="tool_use", idle=40, model=MODEL,
             first="Add a JWT auth middleware to the /v1 API",
             events=lambda: [
                 user_text("Add a JWT auth middleware to the /v1 API", 600),
                 asst([t_text("I'll scaffold the middleware and wire it into the router."),
                       skill_call("api-scaffold", "route=auth", "t1")], 560, "tool_use"),
                 tool_result("t1", "created src/middleware/auth.ts", 555),
                 asst([t_text("Now adding the token verification helper."),
                       t_tool("Write", {"file_path": f"{DEMO_CWD_ROOT}/src/auth/verify.ts",
                                        "content": "export function verify(t){...}"}, "t2")], 50, "tool_use"),
             ]),
        dict(key="fix-flaky-tests", name="fix-flaky-tests", status="waiting",
             waiting="Bash(npm test) needs approval", stop="tool_use", idle=20, model=MODEL,
             first="The checkout e2e test is flaky, can you stabilize it",
             events=lambda: [
                 user_text("The checkout e2e test is flaky, can you stabilize it", 300),
                 asst([t_text("Let me reproduce it first."),
                       skill_call("test-runner", "suite=e2e", "t1")], 260, "tool_use"),
                 tool_result("t1", "FAIL checkout.e2e.ts (2/10 runs)", 255),
                 asst([t_text("I'll run it 20x to measure the flake rate."),
                       bash("npm test -- checkout.e2e --runs 20", "t2", "stress the flaky test")], 25, "tool_use"),
             ]),
        dict(key="refactor-payments", name="refactor-payments", status="idle", waiting=None,
             stop="tool_use", idle=12 * MIN, model=MODEL,
             first="Refactor the payments module to use the new ledger client",
             events=lambda: [
                 user_text("Refactor the payments module to use the new ledger client", 40 * MIN),
                 asst([t_text("Reading the existing payments code and the ledger conventions."),
                       mem_read("api-conventions", "t1")], 38 * MIN, "tool_use"),
                 tool_result("t1", "loaded api-conventions", 37 * MIN),
                 asst([t_text("Editing the charge handler."),
                       t_tool("Edit", {"file_path": f"{DEMO_CWD_ROOT}/src/payments/charge.ts",
                                       "old_string": "oldClient", "new_string": "ledgerClient"}, "t2")], 13 * MIN, "tool_use"),
             ]),
        dict(key="write-api-docs", name="write-api-docs", status="idle", waiting=None,
             stop="end_turn", idle=22 * MIN, model=MODEL,
             first="Document the new auth endpoints in the API reference",
             events=lambda: [
                 user_text("Document the new auth endpoints in the API reference", 60 * MIN),
                 asst([t_text("Drafting docs from the route definitions."),
                       skill_call("changelog-gen", "", "t1"),
                       mem_read("api-conventions", "t2")], 55 * MIN, "tool_use"),
                 tool_result("t1", "drafted", 54 * MIN),
                 asst([t_text("Done — added /v1/auth/login and /v1/auth/refresh to docs/api.md with examples.")], 22 * MIN, "end_turn"),
             ]),
        dict(key="migrate-postgres", name="migrate-postgres-15", status="idle", waiting=None,
             stop="end_turn", idle=2 * HOUR + 10 * MIN, model=MODEL,
             first="Migrate the staging database to Postgres 15",
             events=lambda: [
                 user_text("Migrate the staging database to Postgres 15", 5 * HOUR),
                 asst([t_text("Planning the migration."),
                       plan_write("pg15-migration.md", "# PG15 migration\n\n1. snapshot\n2. upgrade\n3. verify", "t1")], 4.5 * HOUR, "tool_use"),
                 asst([t_text("Refining the plan after checking replica lag."),
                       plan_edit("pg15-migration.md", "3. verify", "3. verify\n4. cut over replicas", "t2"),
                       skill_call("db-migrate", "target=pg15", "t3"),
                       mem_read("db-guidelines", "t4")], 4 * HOUR, "tool_use"),
                 tool_result("t3", "migration applied to staging", 3.9 * HOUR),
                 asst([t_text("Migration complete; staging is on PG15 and smoke tests pass.")], 2 * HOUR + 10 * MIN, "end_turn"),
             ]),
        dict(key="optimize-images", name="optimize-image-pipeline", status="idle", waiting=None,
             stop="tool_use", idle=2 * MIN, model=MODEL,
             first="Speed up the thumbnail generation pipeline",
             events=lambda: [
                 user_text("Speed up the thumbnail generation pipeline", 30 * MIN),
                 asst([t_text("Profiling the current pipeline in the background while I read the code."),
                       bash("python bench/profile_thumbs.py --runs 200 > /tmp/prof.txt", "bg1",
                            "profile thumbnail pipeline", bg=True)], 28 * MIN, "tool_use"),
                 asst([t_text("Reading the resize worker."),
                       skill_file_read("deploy-helper", "t2"),
                       bash("grep -rn skills/ src/ | head", "t3", "find skill refs")], 2 * MIN, "tool_use"),
             ]),
    ]


# History-only sessions (no live PID needed — they populate the History list).
HISTORY_ONLY = [
    ("add-rate-limiting", "Add rate limiting to the public API", 6 * HOUR, [
        user_text("Add rate limiting to the public API", 6 * HOUR),
        asst([t_text("Added a token-bucket limiter middleware."),
              skill_call("api-scaffold", "mw=ratelimit", "t1")], 6 * HOUR - 200, "end_turn"),
    ]),
    ("upgrade-deps", "Upgrade dependencies and fix breaking changes", 26 * HOUR, [
        user_text("Upgrade dependencies and fix breaking changes", 26 * HOUR),
        asst([t_text("Bumped 14 packages, fixed the Fastify v5 route changes."),
              skill_call("lint-fixer", "", "t1"),
              bash("npm run lint -- --fix", "t2", "autofix lint")], 26 * HOUR - 300, "end_turn"),
    ]),
    ("fix-webhook-retries", "Webhooks aren't retrying on 5xx", 50 * HOUR, [
        user_text("Webhooks aren't retrying on 5xx from the receiver", 50 * HOUR),
        asst([t_text("Found the bug: the retry guard checked == 500 instead of >= 500."),
              mem_read("ci-pipeline", "t1")], 50 * HOUR - 250, "end_turn"),
    ]),
    ("cut-release-2-4", "Cut the 2.4 release", 72 * HOUR, [
        user_text("Cut the 2.4 release", 72 * HOUR),
        asst([t_text("Drafted the changelog and tagged v2.4.0."),
              skill_call("changelog-gen", "", "t1"),
              skill_call("deploy-helper", "env=staging", "t2"),
              mem_read("release-process", "t3")], 72 * HOUR - 400, "end_turn"),
    ]),
    ("add-search-endpoint", "Add a full-text search endpoint", 96 * HOUR, [
        user_text("Add a full-text search endpoint over orders", 96 * HOUR),
        asst([t_text("Implemented /v1/orders/search backed by Postgres tsvector."),
              skill_call("api-scaffold", "route=search", "t1"),
              skill_call("db-migrate", "add=search_index", "t2")], 96 * HOUR - 350, "end_turn"),
    ]),
]


# ---------- writers ----------

def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Give every line a uuid so the timeline's per-event "⑂ fork" button renders
    # (it only shows when ev.uuid is present), mirroring real Claude transcripts.
    for r in rows:
        r.setdefault("uuid", uuid.uuid4().hex)
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def stop_demo() -> None:
    if PIDFILE.exists():
        for line in PIDFILE.read_text().split():
            try:
                os.kill(int(line), 15)
            except (ProcessLookupError, ValueError, OSError):
                pass
        PIDFILE.unlink()
        print("· stopped demo sleep processes")
    else:
        print("· no .demo-pids file; nothing to stop")


def main() -> int:
    if "--stop" in sys.argv:
        stop_demo()
        return 0

    stop_demo()  # clean any prior run
    if CLAUDE.exists():
        shutil.rmtree(CLAUDE)
    (CLAUDE / "sessions").mkdir(parents=True)
    (CLAUDE / "plans").mkdir(parents=True)
    proj = CLAUDE / "projects" / SLUG
    (proj / "memory").mkdir(parents=True)

    # skills
    for name, body in SKILLS.items():
        d = CLAUDE / "skills" / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(body)

    # memory + MEMORY.md index
    index = ["# Memory", ""]
    for name, (mtype, body) in MEMORIES.items():
        fm = f"---\nname: {name}\ndescription: {body[:60]}\nmetadata:\n  type: {mtype}\n---\n\n{body}\n"
        (proj / "memory" / f"{name}.md").write_text(fm)
        index.append(f"- [{name}]({name}.md) — {body[:50]}")
    (proj / "memory" / "MEMORY.md").write_text("\n".join(index) + "\n")

    history_rows: list[dict] = []
    pids: list[int] = []
    sid_n = 0

    # live sessions (need alive PIDs)
    for spec in live_sessions():
        sid_n += 1
        sid = f"demo-{sid_n:04d}-{spec['key']}"
        proc = subprocess.Popen(["sleep", "100000"], start_new_session=True,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        pids.append(proc.pid)
        rows = spec["events"]()
        write_jsonl(proj / f"{sid}.jsonl", rows)
        sess = {
            "pid": proc.pid, "sessionId": sid, "cwd": DEMO_CWD_ROOT,
            "name": spec["name"], "status": spec["status"],
            "waitingFor": spec["waiting"],
            "startedAt": int((NOW - 6 * HOUR) * 1000),
            "updatedAt": int((NOW - spec["idle"]) * 1000),
            "version": "1.0.0",
        }
        (CLAUDE / "sessions" / f"{sid}.json").write_text(json.dumps(sess))
        history_rows.append({"sessionId": sid, "display": spec["first"],
                             "timestamp": _ts(spec["idle"]), "project": DEMO_CWD_ROOT})

    # history-only sessions
    for key, first, idle, rows in HISTORY_ONLY:
        sid_n += 1
        sid = f"demo-{sid_n:04d}-{key}"
        write_jsonl(proj / f"{sid}.jsonl", rows)
        history_rows.append({"sessionId": sid, "display": first,
                             "timestamp": _ts(idle), "project": DEMO_CWD_ROOT})

    write_jsonl(CLAUDE / "history.jsonl", history_rows)
    PIDFILE.write_text(" ".join(str(p) for p in pids))

    print(f"✓ seeded demo home at {DEMO}")
    print(f"  {len(pids)} live sessions + {len(HISTORY_ONLY)} history-only, "
          f"{len(SKILLS)} skills, {len(MEMORIES)} memories")
    print(f"  run: CLAUDE_FLEET_HOME={DEMO} bash run.sh")
    print(f"  stop live processes later: python3 {Path(__file__).name} --stop")
    return 0


if __name__ == "__main__":
    sys.exit(main())
