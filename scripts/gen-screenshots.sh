#!/usr/bin/env bash
# Regenerate the README screenshots from synthetic demo data.
#
#   1. seed fixtures/demo-home with fake "Acme" sessions
#   2. serve the dashboard against it (CLAUDE_FLEET_HOME)
#   3. drive headless Chrome to capture each panel via hash deep-links
#   4. tear everything down
#
# No real user data is ever involved. Requires Google Chrome / Chromium.
# Override the browser with CHROME=/path/to/chrome.
set -euo pipefail
cd "$(dirname "$0")/.."

PORT="${SHOT_PORT:-7901}"
DEMO="$PWD/fixtures/demo-home"
OUT="$PWD/docs"

CHROME="${CHROME:-}"
if [ -z "$CHROME" ]; then
  for c in "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
           "/Applications/Chromium.app/Contents/MacOS/Chromium" \
           google-chrome chromium chromium-browser; do
    if command -v "$c" >/dev/null 2>&1 || [ -x "$c" ]; then CHROME="$c"; break; fi
  done
fi
[ -n "$CHROME" ] || { echo "✗ no Chrome/Chromium found; set CHROME=..." >&2; exit 1; }

cleanup() {
  [ -n "${SRV_PID:-}" ] && kill "$SRV_PID" 2>/dev/null || true
  python3 fixtures/seed.py --stop >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "· seeding demo data"
python3 fixtures/seed.py >/dev/null

echo "· starting dashboard on :$PORT"
source .venv/bin/activate 2>/dev/null || { python3 -m venv .venv && source .venv/bin/activate; }
python -c "import fastapi" 2>/dev/null || pip install -q -e .
CLAUDE_FLEET_HOME="$DEMO" uvicorn app:app --host 127.0.0.1 --port "$PORT" >/tmp/fleet-shots.log 2>&1 &
SRV_PID=$!

for _ in $(seq 1 30); do
  curl -sf -o /dev/null "http://127.0.0.1:$PORT/api/windows" && break
  sleep 0.5
done

shot() {  # <hash-or-empty> <height> <outfile>
  local url="http://127.0.0.1:$PORT/?snapshot${1:+#$1}"
  rm -rf "/tmp/cprof_$3"
  timeout 30 "$CHROME" --headless --disable-gpu --no-sandbox --hide-scrollbars \
    --no-first-run --no-default-browser-check --disable-background-networking \
    --disable-component-update --disable-default-apps --force-device-scale-factor=2 \
    --user-data-dir="/tmp/cprof_$3" --window-size="1440,$2" --virtual-time-budget=7000 \
    --screenshot="$OUT/$3" "$url" >/dev/null 2>&1 || true
  [ -s "$OUT/$3" ] && echo "  ✓ $3" || { echo "  ✗ $3 (not written)"; return 1; }
}

shot_url() {  # <path> <height> <outfile>  — capture an arbitrary page (e.g. /share/<id>)
  local url="http://127.0.0.1:$PORT$1"
  rm -rf "/tmp/cprof_$3"
  timeout 30 "$CHROME" --headless --disable-gpu --no-sandbox --hide-scrollbars \
    --no-first-run --no-default-browser-check --disable-background-networking \
    --disable-component-update --disable-default-apps --force-device-scale-factor=2 \
    --user-data-dir="/tmp/cprof_$3" --window-size="1100,$2" --virtual-time-budget=7000 \
    --screenshot="$OUT/$3" "$url" >/dev/null 2>&1 || true
  [ -s "$OUT/$3" ] && echo "  ✓ $3" || { echo "  ✗ $3 (not written)"; return 1; }
}

echo "· capturing → $OUT"
shot ""                                  1600 screenshot-hero.png
shot "skills"                            1520 screenshot-skills.png
shot "memory"                            1520 screenshot-memory.png
shot "search=postgres"                   1200 screenshot-search.png
# timeline now shows the per-event "⑂ fork" buttons (#3) + the "Share" header button (#4)
shot "timeline=demo-0005-migrate-postgres" 1720 screenshot-timeline.png

# #4 web-share: render a redacted share page from the demo session, then capture it
SHARE_URL=$(curl -s -X POST "http://127.0.0.1:$PORT/api/history/demo-0005-migrate-postgres/share?redact=true" \
  | python3 -c "import sys,json; print(json.load(sys.stdin).get('share_url',''))" 2>/dev/null || true)
if [ -n "$SHARE_URL" ]; then shot_url "$SHARE_URL" 1400 screenshot-share.png
else echo "  ✗ screenshot-share.png (share render failed)"; fi

echo "✓ done"
