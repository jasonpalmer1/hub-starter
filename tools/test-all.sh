#!/usr/bin/env bash
# Runs the kit's existing test suites. Usage: bash tools/test-all.sh --core
# --core = the fast set used by ship-slice (a few seconds; no mutation runs).
# --full = --core plus the slow tier: test-scan-hygiene.sh (drives mutation-check.sh on small subsets; minutes).
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$HERE")"
case "${1:-}" in --core|--full) ;; *) echo "usage: test-all.sh --core|--full"; exit 2;; esac
export PYTHONDONTWRITEBYTECODE=1   # keep the tree clean so prepush sees no stray files
rc=0
for t in tools/test-scan.sh tools/test-install.sh tools/test-ship-slice.sh kit/test.sh docs/test.sh tools/e2e.sh; do
  bash "$ROOT/$t" >/dev/null 2>&1 && echo "core: PASS $t" || { echo "core: FAIL $t"; rc=1; }
done
if [ "${1:-}" = "--full" ]; then
  bash "$ROOT/tools/test-scan-hygiene.sh" >/dev/null 2>&1 && echo "full: PASS tools/test-scan-hygiene.sh" || { echo "full: FAIL tools/test-scan-hygiene.sh"; rc=1; }
fi
[ $rc -eq 0 ] && echo "core: ALL PASS" || echo "core: FAIL"
exit $rc
