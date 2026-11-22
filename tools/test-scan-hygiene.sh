#!/usr/bin/env bash
# Tests two scanner-tooling guards in a scratch copy: (1) test-scan runs leave no __pycache__;
# (2) a mutant whose layout() fails, or a lost/duplicated worker, fails the whole run.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(dirname "$HERE")"
TMP="$(mktemp -d)"; trap 'chmod -R u+w "$TMP" 2>/dev/null; rm -rf "$TMP"' EXIT
FAILS=0
ok()  { echo "ok   $1"; }
bad() { FAILS=$((FAILS+1)); echo "FAIL $1"; }
unset PYTHONDONTWRITEBYTECODE HUBSTARTER_DENYLIST
mk() { mkdir -p "$TMP/$1"; git -C "$REPO" ls-files | while IFS= read -r f; do mkdir -p "$TMP/$1/$(dirname "$f")"; cp -p "$REPO/$f" "$TMP/$1/$f"; done; }

# 1. test-scan.sh (env cleared by us) and a direct test_scan.py run both leave the tree free of __pycache__.
mk a
bash "$TMP/a/tools/test-scan.sh" >/dev/null 2>&1
[ -z "$(find "$TMP/a" -name __pycache__)" ] && ok "test-scan.sh leaves no __pycache__" || bad "test-scan.sh left __pycache__"
python3 -W ignore "$TMP/a/tools/test_scan.py" >/dev/null 2>&1
[ -z "$(find "$TMP/a" -name __pycache__)" ] && ok "direct test_scan.py leaves no __pycache__" || bad "direct test_scan.py left __pycache__"

# 2. Fake cp that fails only for one mutant's README copy (the second copy in layout(); baseline and the other
# mutants copy normally). MUT_ONLY keeps these runs to two mutants so the test takes seconds, not minutes.
mk b
mkdir -p "$TMP/bin"
printf '%s\n' '#!/bin/sh' 'case "$*" in *README.md*"/ticket-range/"*) exit 1;; esac' 'exec /bin/cp "$@"' > "$TMP/bin/cp"
chmod +x "$TMP/bin/cp"
SUB="ticket-range,rule-ip10"
OUT="$(PATH="$TMP/bin:$PATH" MUT_ONLY=$SUB MUT_SHARD= bash "$TMP/b/tools/mutation-check.sh" 2>&1)"; rc=$?
echo "$OUT" | grep -q '^ERROR   ticket-range (layout failed)' && ok "failed README copy reports ERROR" || bad "failed README copy not reported ERROR"
echo "$OUT" | grep -q '^KILLED   ticket-range' && bad "failed-layout mutant counted KILLED" || ok "failed-layout mutant not KILLED"
[ "$rc" -ne 0 ] && echo "$OUT" | grep -q 'mutation-check: FAIL' && ok "run fails (rc=$rc)" || bad "run did not fail"

# 2b. A lost worker (its shell is killed mid-run, no result line) must fail the run, never PASS on fewer results.
printf '%s\n' '#!/bin/sh' 'case "$*" in *README.md*"/ticket-range/"*) kill -9 $PPID; exit 1;; esac' 'exec /bin/cp "$@"' > "$TMP/bin/cp"
OUT="$(PATH="$TMP/bin:$PATH" MUT_ONLY=$SUB MUT_SHARD= bash "$TMP/b/tools/mutation-check.sh" 2>&1)"; rc=$?
[ "$rc" -ne 0 ] && echo "$OUT" | grep -q 'mutation-check: FAIL (expected 2 results, got 1' && ok "lost worker fails the run (rc=$rc)" || bad "lost worker did not fail the run (rc=$rc)"
echo "$OUT" | grep -q 'mutation-check: PASS' && bad "lost worker printed PASS" || ok "lost worker never prints PASS"

# 2c. Every worker lost (zero results) fails too; the baseline copy still works because it is not under a mutant dir.
printf '%s\n' '#!/bin/sh' 'case "$*" in *README.md*"/baseline/"*) ;; *README.md*) kill -9 $PPID; exit 1;; esac' 'exec /bin/cp "$@"' > "$TMP/bin/cp"
OUT="$(PATH="$TMP/bin:$PATH" MUT_ONLY=$SUB MUT_SHARD= bash "$TMP/b/tools/mutation-check.sh" 2>&1)"; rc=$?
[ "$rc" -ne 0 ] && echo "$OUT" | grep -q 'got 0' && ok "zero results fails the run (rc=$rc)" || bad "zero results did not fail the run (rc=$rc)"

# 2d. A duplicate result line (one mutant reporting twice) fails the run.
printf '%s\n' '#!/bin/sh' 'case "$*" in *README.md*"/ticket-range/"*) echo "KILLED   ticket-range";; esac' 'exec /bin/cp "$@"' > "$TMP/bin/cp"
OUT="$(PATH="$TMP/bin:$PATH" MUT_ONLY=$SUB MUT_SHARD= bash "$TMP/b/tools/mutation-check.sh" 2>&1)"; rc=$?
[ "$rc" -ne 0 ] && echo "$OUT" | grep -q 'expected 2 results, got 3' && ok "duplicate result fails the run (rc=$rc)" || bad "duplicate result did not fail the run (rc=$rc)"

# 2e. Nothing selected is a failure, not a vacuous PASS.
OUT="$(MUT_ONLY=no-such-mutant MUT_SHARD= bash "$TMP/b/tools/mutation-check.sh" 2>&1)"; rc=$?
[ "$rc" -ne 0 ] && echo "$OUT" | grep -q 'no mutations selected' && ok "empty selection fails the run" || bad "empty selection did not fail"

# 2f. Unshimmed two-mutant subset passes with the right count.
OUT="$(MUT_ONLY=$SUB MUT_SHARD= bash "$TMP/b/tools/mutation-check.sh" 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && echo "$OUT" | grep -q 'PASS (subset: 2 of [0-9]* mutations killed' && ! echo "$OUT" | grep -q 'PASS (all' && ok "clean 2-mutant subset passes and is labelled subset" || bad "clean subset did not pass (rc=$rc)"

# 2g. A mutant whose test run dies (SIGKILL -> 137, timeout 124, not executable 126, not found 127) is ERROR, never KILLED/PASS.
# A python3 shim fails only for the ticket-range mutant's test_scan.py; a normal failure (exit 1) is still KILLED.
REALPY="$(command -v python3)"
mkdir -p "$TMP/pybin"
for mode in sigkill 124 125 126 127 bare1 1; do
  case "$mode" in
    sigkill) act='kill -9 $$' ;;
    bare1) act='exit 1' ;;   # exits 1 without running any assertion: must NOT read as a kill
    1) act='echo "Ran 3 tests in 0.001s" >&2; echo "FAILED (failures=1)" >&2; exit 1' ;;   # a genuine unittest failure
    *) act="exit $mode" ;;
  esac
  printf '%s\n' '#!/bin/sh' 'case "$*" in */ticket-range/*test_scan.py*) '"$act"';; esac' "exec $REALPY \"\$@\"" > "$TMP/pybin/python3"
  chmod +x "$TMP/pybin/python3"
  OUT="$(PATH="$TMP/pybin:$PATH" MUT_ONLY=$SUB MUT_SHARD= bash "$TMP/b/tools/mutation-check.sh" 2>&1)"; rc=$?
  if [ "$mode" = 1 ]; then
    [ "$rc" -eq 0 ] && echo "$OUT" | grep -q '^KILLED   ticket-range' && ok "normal test failure (exit 1) is KILLED, run passes" || bad "exit 1 not treated as a normal kill (rc=$rc)"
  else
    [ "$rc" -ne 0 ] && echo "$OUT" | grep -q '^ERROR    ticket-range' && ! echo "$OUT" | grep -q '^KILLED   ticket-range' && ! echo "$OUT" | grep -q 'mutation-check: PASS' && ok "test run dying ($mode) is ERROR and fails the run" || bad "test run dying ($mode) not ERROR/FAIL (rc=$rc)"
  fi
done

# 3. Read-only tools dir: no __pycache__ attempt and no crash.
mk c
chmod -R a-w "$TMP/c/tools"
python3 -W ignore "$TMP/c/tools/test_scan.py" >/dev/null 2>&1; r=$?
chmod -R u+w "$TMP/c/tools"
[ -z "$(find "$TMP/c" -name __pycache__)" ] && ok "read-only tools dir: no __pycache__ (test rc=$r)" || bad "read-only: __pycache__ made"

echo "test-scan-hygiene: $FAILS failure(s)"; [ "$FAILS" -eq 0 ]
