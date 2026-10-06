#!/usr/bin/env bash
set -euo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"

uv run ruff check src tests scripts
uv run ruff format --check src tests scripts
