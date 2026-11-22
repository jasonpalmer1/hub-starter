#!/usr/bin/env bash
# Runs the diff-triggers tests in a scratch HOME, alone, with nothing else installed.
#   bash test.sh            run the tests
#   bash test.sh --mutate   also break each rule in a temp copy and check the tests notice
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT
export HOME="$SCRATCH/home"; mkdir -p "$HOME"
unset HUBKIT_HOME DT_DIR
export PYTHONDONTWRITEBYTECODE=1

command -v python3 >/dev/null || { echo "FAIL: python3 not found"; exit 1; }
command -v git >/dev/null || { echo "FAIL: git not found"; exit 1; }
python3 -W ignore "$HERE/test_diff_triggers.py" || { echo "FAIL: diff-triggers tests"; exit 1; }
echo "diff-triggers: tests PASS"

[ "${1:-}" = "--mutate" ] || exit 0

rc=0
mutate() {  # name, file, old text, new text
  local name="$1" file="$2" dir="$SCRATCH/mut-$1"
  mkdir -p "$dir"
  cp "$HERE/trigger_check.py" "$HERE/base_read.py" "$dir/"
  python3 - "$dir/$file" "$3" "$4" <<'PY' || { echo "mutation $name: could not apply (rule text changed?)"; rc=1; return; }
import sys
p, old, new = sys.argv[1:4]
s = open(p).read()
if old not in s:
    sys.exit(1)
open(p, "w").write(s.replace(old, new, 1))
PY
  if DT_DIR="$dir" python3 -W ignore "$HERE/test_diff_triggers.py" >/dev/null 2>&1; then
    echo "mutation $name: SURVIVED (tests did not notice)"; rc=1
  else
    echo "mutation $name: killed"
  fi
}
mutate empty-replaces-allowed trigger_check.py 'if not rep or (rep == "-" and trig != "none"):' 'if False:'
mutate dash-allowed-everywhere trigger_check.py 'or (rep == "-" and trig != "none")' ''
mutate unchanged-inverted trigger_check.py 'return not any(fnmatch.fnmatchcase(p, arg) for p in changed)' 'return any(fnmatch.fnmatchcase(p, arg) for p in changed)'
mutate none-row-matches trigger_check.py '    if t == "none":
        return False' '    if t == "none":
        return True'
mutate off-rows-run trigger_check.py 'if mode != "observe":' 'if False:'
mutate observe-refuses trigger_check.py '    print("TRIGGER-OBSERVE: %d rows matched (observe only, never blocks)" % len(hits))' '    print("TRIGGER-OBSERVE: %d rows matched (observe only, never blocks)" % len(hits))
    if hits:
        return 1'
mutate rules-edit-silent trigger_check.py 'if os.path.basename(p) in RULE_FILES or p in RULE_PATHS:' 'if False:'
mutate tip-fallback base_read.py '        err("MISS: %s does not exist at base %s (no fallback to the tip copy)" % (p, sha[:12]))
        return 3' '        sys.stdout.write(open(repo + "/" + p).read())
        return 0'
mutate symlink-allowed base_read.py 'if mode not in ("100644", "100755"):' 'if False:'
mutate dotdot-allowed base_read.py '".." in parts or ' ''
mutate absolute-allowed base_read.py 'p.startswith("/") or ' ''
mutate root-ui-anywhere trigger_check.py 'any(re.search(arg, p) for p in added)' 'any(re.search(arg, p) for p in changed)'
[ $rc -eq 0 ] && echo "diff-triggers: mutation check PASS" || echo "diff-triggers: mutation check FAIL"
exit $rc
