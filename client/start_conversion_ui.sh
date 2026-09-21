#!/usr/bin/env bash
# Launches the desktop pre-converter GUI (conversion_ui.py) for the Qwen Video
# Classifier. Just double-click this (or run it from a terminal) — it checks
# for python3, tkinter and ffmpeg/ffprobe first and gives a clear error if
# any are missing, instead of letting the GUI crash with a stack trace.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PYTHON_BIN="${PYTHON_BIN:-}"
if [ -z "$PYTHON_BIN" ]; then
    if command -v python3 >/dev/null 2>&1; then
        PYTHON_BIN="python3"
    elif command -v python >/dev/null 2>&1; then
        PYTHON_BIN="python"
    else
        echo "error: python3 not found on PATH. Install Python 3.8+ and try again." >&2
        exit 1
    fi
fi

if ! "$PYTHON_BIN" -c "import tkinter" >/dev/null 2>&1; then
    echo "error: the 'tkinter' module isn't available for $PYTHON_BIN." >&2
    echo "  - Debian/Ubuntu: sudo apt install python3-tk" >&2
    echo "  - Fedora:        sudo dnf install python3-tkinter" >&2
    echo "  - Arch:          sudo pacman -S tk" >&2
    echo "  - macOS (brew):  brew install python-tk" >&2
    exit 1
fi

for tool in ffmpeg ffprobe; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "error: '$tool' not found on PATH. Install ffmpeg and try again." >&2
        exit 1
    fi
done

exec "$PYTHON_BIN" "$SCRIPT_DIR/conversion_ui.py" "$@"
