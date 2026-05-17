#!/usr/bin/env bash
# Meeting Debrief Assistant — Mac / Linux launcher
# Double-click this file or run: ./launch.sh

set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Prefer python3, fall back to python
PYTHON=""
for cmd in python3 python; do
    if command -v "$cmd" &>/dev/null; then
        PYTHON="$cmd"
        break
    fi
done

if [ -z "$PYTHON" ]; then
    echo "Python not found. Install it from https://python.org" >&2
    exit 1
fi

exec "$PYTHON" "$DIR/launch.py" "$@"