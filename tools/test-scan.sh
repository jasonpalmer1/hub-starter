#!/usr/bin/env bash
# Runs the leak-scanner tests (Attack list) under plain python3; add --mutate to also run the mutation check.
set -eu
export PYTHONDONTWRITEBYTECODE=1   # no __pycache__ in the tree: a stray folder fails prepush
HERE="$(cd "$(dirname "$0")" && pwd)"
python3 -W ignore "$HERE/test_scan.py"
[ "${1:-}" = "--mutate" ] && bash "$HERE/mutation-check.sh"
echo "test-scan: ALL PASS"
