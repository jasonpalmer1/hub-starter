#!/usr/bin/env bash
# Checks the docs against the repo (matrix rows, mentioned paths, pattern names).
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
command -v python3 >/dev/null || { echo "FAIL: python3 not found"; exit 1; }
PYTHONDONTWRITEBYTECODE=1 python3 "$HERE/test_docs.py" || { echo "FAIL: docs tests"; exit 1; }
echo "docs: tests PASS"
