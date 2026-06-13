English | [中文](README.zh-CN.md)

# Claude Fleet

When you're vibe coding with 5–7 Claude Code windows open at once, you need one
place to see what every window is doing — who's stuck, who's waiting on you, who's
done.

![](docs/screenshot-hero.png)

## Run it in 30 seconds

```bash
git clone https://github.com/tianyilt/claude-fleet
cd claude-fleet && bash run.sh
# open http://127.0.0.1:7878 in your browser
```

The first run creates a venv and installs dependencies automatically — nothing to set up.

## Install options

**Run as a macOS app (recommended on Mac).** Packaging Fleet as a signed `.app`
gives it a stable code identity, so macOS binds the "control iTerm2" Automation
grant to it permanently — resume/fork/focus keep working even across restarts
(a plain `./run.sh` backgrounded process can lose that permission once its
terminal closes):

```bash
./scripts/build-app.sh --install      # builds + copies to /Applications
```

Launch *Claude Fleet* from /Applications, click **Resume** on any session once,
and approve **“Claude Fleet” wants to control “iTerm.app”**. For development with
hot-reload, keep using `./run.sh`.

**Run anywhere (Windows / Linux).** The dashboard, history, search and monitoring
are cross-platform:

```bash
pip install -e .
./run.sh        # macOS/Linux
run.bat         # Windows
```

Opening/focusing a real terminal window is macOS-only (iTerm2). On Windows/Linux,
resume/fork instead copy the `claude --resume …` command to your clipboard for you
to paste into your own terminal.

**Releases.** Tagged releases ship three artifacts: `claude-fleet-macos-app.zip`
(double-clickable app), `claude-fleet-src.tar.gz` (run anywhere), and
`claude-fleet-windows.zip`.

## What it solves

The everyday pain of multi-window vibe coding:

- **Permission prompts flash by and you miss them** → a persistent red bar at the top; click it to jump back to that terminal.
- **You don't know what each window is doing** → every card shows the current task, triage status, and background jobs.
- **Finished windows get left open** → the patrol engine marks them `closeable`; close with one click.
- **You can't find that session from last week** → full-text search returns in ~50ms with VS Code–style match context.
- **You don't know how much a skill actually gets used** → 3-dimensional stats (invokes + file read/write + bash references).
- **You don't know who touched a memory** → in-degree (↓ sessions that read it) + out-degree (↑ sessions that wrote it).

## Core features

### Triage classification

Not a simple busy/idle flag. The patrol engine reads each transcript's
`stop_reason`, `queue-operation` events, and background-task state:

| Status | Meaning | How it's decided |
|--------|---------|------------------|
| 🟢 working | actively working | busy, or has a live Monitor/Bash background task |
| 🔴 waiting | waiting on you | permission prompt / dialog open |
| 🟡 stalled | stuck | stop_reason=tool_use + idle > 5 min |
| 🔵 completed | done | stop_reason=end_turn + idle > 5 min |
| ⚪ closeable | safe to close | completed + idle > 1 h |

Background tasks (`Bash run_in_background`, `Monitor persistent`) are tracked by
pairing tool_use/tool_result; finished ones are cleared automatically, so they
don't get misread as `working`.

### Search

ripgrep across all Claude + Codex transcripts, ~50ms. It doesn't just search
session titles — searching "hailuo" finds a session that mentioned Hailuo in the
conversation, even if the title is "you should check klingai.com".

Each result carries up to 3 match-context snippets so you can see at a glance why
it matched.

![](docs/screenshot-search.png)

### Skill / memory tracking

The skill panel reports three dimensions:

```
paper2video        333   1 invoke · ↓122 reads · ↑53 writes · 157 bash
feishu-notify       45  24 invokes · ↓7 reads · ↑7 writes · 7 bash
qzcli-topdowneval   12   3 invokes · ↓1 reads · ↑2 writes · 6 bash
```

If you only counted formal `/skill-name` invocations you'd get 44; adding
Read/Write/Edit of skill files plus Bash references to `skills/` brings the real
total to 431.

The memory panel groups by type (user / feedback / project / reference) and shows
`↓3 ↑2` per entry (read by 3 sessions, modified by 2).

![](docs/screenshot-skills.png)
![](docs/screenshot-memory.png)

### Timeline + plan history

Open any session to see the full conversation flow. Skill calls are purple,
memory reads are dashed blue, memory writes are pink.

Plan version history: a session typically iterates on its plan 5–14 times — each
Write is a full snapshot, each Edit is a red/green diff.

![](docs/screenshot-timeline.png)

### Actions

| Button | What it does |
|--------|--------------|
| Focus | jump to that terminal tab |
| Fork | `claude --resume <sid> --fork-session` — new session inherits the history |
| Resume | `claude --resume <sid>` — continue the original session |
| Review | run `claude -p` review in the background; the verdict (PASS/FAIL/PARTIAL) shows on the card |
| Close | SIGTERM |

On Windows/Linux the dashboard still works, but Fork/Resume can't open a native
window — they copy the `claude --resume …` command to your clipboard to paste into
your own terminal.

> **Focus setup (macOS).** Focus works out of the box on Terminal.app and iTerm2 —
> including when your sessions run inside **tmux** — via the bundled
> [`scripts/focus-tty.sh`](scripts/focus-tty.sh) (maps process tty → owning terminal
> tab → raises it). To customize for another terminal/window manager, drop an
> executable `~/.claude/focus-tty.sh` taking a `<tty>` arg; it takes precedence over
> the bundled default. *(Bundled shim contributed by [@wanshuiyin](https://github.com/wanshuiyin).)*

## Architecture

Single-file frontend (Alpine.js + Tailwind via CDN — no npm). The Python backend
only **reads** `~/.claude/` and `~/.codex/`; it never mutates any agent state.

```
app.py                FastAPI + SSE (2s polling)
core/
  sessions.py         read sessions/*.json, map to TTY
  transcripts.py      parse JSONL; extract skill/memory/plan/background tasks
  patrol.py           triage classification engine
  codex.py            Codex session parsing
  search.py           cross-platform ripgrep search
  terminal.py         per-platform terminal control (macOS iTerm2; degrades elsewhere)
  actions.py          fork / review / close (session lookup around terminal.py)
  history.py          unified index + full-text rg search
  skills.py           skill directory scan
  memory.py           memory file parsing
  plans.py            plan association (extracted from transcripts)
  perms.py            permission events
static/index.html     single-file SPA
```

## Acknowledgements

- [HarnessKit](https://github.com/RealZST/HarnessKit) — UI reference for cross-platform skill management
- [Synergy](https://github.com/SII-Holos/synergy) — inspiration for the memory-engram classification view

## License

[MIT](LICENSE)
