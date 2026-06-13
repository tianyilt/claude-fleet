#!/bin/bash
# Run the test suite. Tests mock all osascript/iTerm calls — they never open real windows.
set -e
cd "$(dirname "$0")/.."
if [ -x .venv/bin/python ]; then
    PY=.venv/bin/python
else
    PY=python3
fi
exec "$PY" -m pytest tests/ "$@"
