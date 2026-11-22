#!/usr/bin/env bash
# Runs the kit file checks (hub.md, templates, settings snippet) in a scratch HOME.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT
export HOME="$SCRATCH/home"; mkdir -p "$HOME"
export PYTHONDONTWRITEBYTECODE=1
command -v python3 >/dev/null || { echo "FAIL: python3 not found"; exit 1; }
python3 "$HERE/test_kit.py" || { echo "FAIL: kit tests"; exit 1; }
echo "kit: tests PASS"
