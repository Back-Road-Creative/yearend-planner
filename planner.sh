#!/usr/bin/env bash
# Developer launcher (Linux/macOS/WSL). The product is planner.cmd on Windows.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PLANNER_HOME="$HERE" PYTHONUTF8=1 PYTHONDONTWRITEBYTECODE=1
if [ ! -x "$HERE/.venv/bin/python" ]; then uv sync --frozen --directory "$HERE"; fi
exec "$HERE/.venv/bin/python" -m planner "$@"
