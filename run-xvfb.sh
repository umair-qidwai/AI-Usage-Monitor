#!/bin/sh
set -eu

# Resolve relative to this launcher, not a particular user's home directory.
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$ROOT"
umask 077
export HEADLESS=0
export DISCOVER="${DISCOVER:-0}"
export CODEX_POLL_SECONDS="${CODEX_POLL_SECONDS:-5}"
export CLAUDE_REFRESH_SECONDS="${CLAUDE_REFRESH_SECONDS:-5}"
export PROVIDER_FALLBACK_SECONDS="${PROVIDER_FALLBACK_SECONDS:-300}"

# xvfb-run allocates a free display, handles Xauthority, and cleans up on exit.
exec xvfb-run -a -s "-screen 0 1280x900x24 -nolisten tcp" \
    "${PYTHON_BIN:-$ROOT/.venv/bin/python}" "$ROOT/app.py"
