#!/bin/bash
# Build a macOS .app bundle for Claude Fleet.
#
# Why: when the server runs as an orphaned/launchd process, macOS denies its
# osascript Apple Events (no responsible app with Automation permission), so
# resume/fork/focus break. Running it inside a signed .app gives a stable code
# identity that the TCC Automation grant binds to — approve once, works forever.
#
# Usage:
#   ./scripts/build-app.sh            # local app: uses this repo + its .venv
#   ./scripts/build-app.sh --install  # also copy to /Applications
#   SELF_CONTAINED=1 ./scripts/build-app.sh   # bundle source + venv-on-first-run
#                                             # (for downloadable release artifacts)
#   OUT_DIR=/custom ./scripts/build-app.sh    # override output dir (used by tests)
#
# Two launcher modes:
#  - local (default): bakes this repo path + uses $REPO/.venv. Lightest, for your
#    own machine. Breaks if the repo moves.
#  - self-contained (SELF_CONTAINED=1): copies app.py/core/static into the bundle
#    and creates a venv under Application Support on first launch. Works on any
#    Mac with python3 — this is what the release pipeline ships.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
OUT_DIR="${OUT_DIR:-$REPO/dist}"
APP="$OUT_DIR/Claude Fleet.app"
BUNDLE_ID="com.tianyilt.claude-fleet"
SELF_CONTAINED="${SELF_CONTAINED:-0}"
INSTALL=0
[ "${1:-}" = "--install" ] && INSTALL=1

echo "[build-app] repo=$REPO self_contained=$SELF_CONTAINED"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

# ---- Info.plist ----
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>Claude Fleet</string>
    <key>CFBundleDisplayName</key><string>Claude Fleet</string>
    <key>CFBundleIdentifier</key><string>$BUNDLE_ID</string>
    <key>CFBundleExecutable</key><string>claude-fleet</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleShortVersionString</key><string>1.0</string>
    <key>CFBundleVersion</key><string>1</string>
    <key>CFBundleIconFile</key><string>AppIcon</string>
    <key>LSMinimumSystemVersion</key><string>13.0</string>
    <key>NSAppleEventsUsageDescription</key>
    <string>Claude Fleet opens and focuses iTerm2 windows to resume your Claude Code sessions.</string>
</dict>
</plist>
PLIST

# ---- launcher (the app's main executable) ----
# uvicorn runs in the FOREGROUND via exec so this process stays alive as the app
# (Dock icon present, stable Apple Events identity). A backgrounded server would
# be reparented to launchd and lose the Automation grant. trap kills the child on
# quit. The two modes differ only in where the code + python come from.
if [ "$SELF_CONTAINED" = "1" ]; then
    # copy the runnable source tree into the bundle
    for item in app.py core static pyproject.toml; do
        cp -R "$REPO/$item" "$APP/Contents/Resources/"
    done
    # bundled focus shim (terminal.py resolves it at <root>/scripts/focus-tty.sh)
    mkdir -p "$APP/Contents/Resources/scripts"
    cp "$REPO/scripts/focus-tty.sh" "$APP/Contents/Resources/scripts/"
    cat > "$APP/Contents/MacOS/claude-fleet" <<'LAUNCHER'
#!/bin/bash
RES="$(cd "$(dirname "$0")/../Resources" && pwd)"
PORT="${CLAUDE_FLEET_PORT:-7878}"
SUPPORT="$HOME/Library/Application Support/Claude Fleet"
VENV="$SUPPORT/venv"
mkdir -p "$SUPPORT"
if [ ! -x "$VENV/bin/python" ]; then
    /usr/bin/python3 -m venv "$VENV"
    "$VENV/bin/pip" install -q --upgrade pip
    "$VENV/bin/pip" install -q "fastapi>=0.115" "uvicorn[standard]>=0.32" "watchfiles>=0.24" "sse-starlette>=2.1"
fi
cd "$RES" || exit 1
if curl -s -o /dev/null --max-time 1 "http://127.0.0.1:$PORT/"; then open "http://127.0.0.1:$PORT/"; exit 0; fi
( for i in $(seq 1 60); do curl -s -o /dev/null --max-time 1 "http://127.0.0.1:$PORT/" && { open "http://127.0.0.1:$PORT/"; break; }; sleep 0.4; done ) &
trap 'kill 0' EXIT
# Run uvicorn as a CHILD (no exec): this bash process stays alive as the app, so
# macOS keeps attributing the children's Apple Events to Claude Fleet.app.
# --reload picks up edits to app.py/core/* without a manual restart (the
# reloader stays a child of this bash launcher, so the Apple Events identity
# above is preserved).
"$VENV/bin/python" -m uvicorn app:app --host 127.0.0.1 --port "$PORT" --reload
LAUNCHER
else
    # local mode: bake this repo path + use its .venv
    cat > "$APP/Contents/MacOS/claude-fleet" <<LAUNCHER
#!/bin/bash
REPO="$REPO"
PORT="\${CLAUDE_FLEET_PORT:-7878}"
cd "\$REPO" || exit 1
if curl -s -o /dev/null --max-time 1 "http://127.0.0.1:\$PORT/"; then
    open "http://127.0.0.1:\$PORT/"
    exit 0
fi
( for i in \$(seq 1 40); do
    curl -s -o /dev/null --max-time 1 "http://127.0.0.1:\$PORT/" && { open "http://127.0.0.1:\$PORT/"; break; }
    sleep 0.3
  done ) &
trap 'kill 0' EXIT
# Run uvicorn as a CHILD (no exec): this bash process stays alive as the app, so
# macOS keeps attributing the children's Apple Events to Claude Fleet.app.
# --reload picks up edits to app.py/core/* without a manual restart (the
# reloader stays a child of this bash launcher, so the Apple Events identity
# above is preserved).
"\$REPO/.venv/bin/python" -m uvicorn app:app --host 127.0.0.1 --port "\$PORT" --reload
LAUNCHER
fi
chmod +x "$APP/Contents/MacOS/claude-fleet"

# ---- icon (optional) ----
ICON_SRC="$REPO/scripts/app-icon-1024.png"
if [ -f "$ICON_SRC" ] && command -v iconutil >/dev/null 2>&1; then
    echo "[build-app] generating icon from $ICON_SRC"
    ICONSET="$(mktemp -d)/AppIcon.iconset"
    mkdir -p "$ICONSET"
    for sz in 16 32 64 128 256 512; do
        sips -z $sz $sz "$ICON_SRC" --out "$ICONSET/icon_${sz}x${sz}.png" >/dev/null
        sips -z $((sz*2)) $((sz*2)) "$ICON_SRC" --out "$ICONSET/icon_${sz}x${sz}@2x.png" >/dev/null
    done
    iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/AppIcon.icns"
else
    echo "[build-app] no scripts/app-icon-1024.png — using generic icon"
fi

# ---- ad-hoc codesign (stable cdhash so the Automation grant persists) ----
if command -v codesign >/dev/null 2>&1; then
    codesign --force --deep --sign - "$APP"
    echo "[build-app] ad-hoc signed"
fi

echo "[build-app] built: $APP"

if [ "$INSTALL" = "1" ]; then
    DEST="/Applications/Claude Fleet.app"
    rm -rf "$DEST"
    cp -R "$APP" "$DEST"
    echo "[build-app] installed: $DEST"
    echo "[build-app] First launch → click Resume → approve \"Claude Fleet wants to control iTerm.app\"."
fi
