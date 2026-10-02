#!/usr/bin/env bash
# Replay recorded orch author stages through this checkout, offline, and
# compare every output file. Exits non-zero if any item differs.
#
#   ./scripts/replay-check.sh [--jobs N] [--item ID] STAGE_DIR [STAGE_DIR ...]
#
# STAGE_DIR is an orch run's stages/author directory. Detector controls run in
# Docker. See docs/development/replay-gate.md.
set -uo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"

if [ "$#" -eq 0 ]; then
  echo "usage: $0 [--jobs N] [--item ID] STAGE_DIR [STAGE_DIR ...]" >&2
  exit 2
fi

exec env -u FORCE_COLOR uv run --no-sync python -m asago_artifact_generator.replay_gate \
  check "$@"
