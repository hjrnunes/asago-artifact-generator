#!/usr/bin/env bash
# Replay recorded author stages through this checkout, offline, and
# compare every output file. Exits non-zero if any item differs.
#
#   ./scripts/replay-check.sh [--jobs N] [--item ID] STAGE_DIR [STAGE_DIR ...]
#
# STAGE_DIR is the author stage directory of an orchestrator run. The module
# docstring of scripts/replay_gate.py describes the comparison and its options.
set -uo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"

# The corpus lives outside every checkout. A reader given a missing root must
# fail and name it, never fall through to a usage message or a silent pass.
corpus="${ASAGO_REPLAY_CORPUS:-}"
if [ -n "$corpus" ] && [ ! -d "$corpus" ]; then
  echo "error: ASAGO_REPLAY_CORPUS points at a missing corpus folder: $corpus" >&2
  exit 2
fi

if [ "$#" -eq 0 ]; then
  echo "usage: $0 [--jobs N] [--item ID] STAGE_DIR [STAGE_DIR ...]" >&2
  exit 2
fi

exec env -u FORCE_COLOR uv run --no-sync python scripts/replay_gate.py check "$@"
