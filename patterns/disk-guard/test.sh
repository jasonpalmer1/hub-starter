#!/usr/bin/env bash
# Runs the disk-guard tests in a fresh scratch HOME, alone, with nothing else installed.
#   bash test.sh            run the tests
#   bash test.sh --mutate   also break each guard in a temp copy and check the tests notice
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT
export HOME="$SCRATCH/home"; mkdir -p "$HOME"
unset HUBKIT_HOME DISK_GUARD_LOG DISK_GUARD_PATH DISK_GUARD_WARN_GI DISK_GUARD_CRIT_GI DISK_GUARD_FREE_GI_OVERRIDE DISK_GUARD_PS_FILE GUARD
export PYTHONDONTWRITEBYTECODE=1

command -v python3 >/dev/null || { echo "FAIL: python3 not found"; exit 1; }
python3 -W ignore "$HERE/test_disk_guard.py" || { echo "FAIL: disk-guard tests"; exit 1; }
echo "disk-guard: tests PASS"

[ "${1:-}" = "--mutate" ] || exit 0

# Each mutation edits ONE guard in a temp copy; the suite must then FAIL.
rc=0
mutate() {  # name, old text, new text
  local name="$1" old="$2" new="$3" dir="$SCRATCH/mut-$1"
  mkdir -p "$dir"
  python3 - "$HERE/disk-guard.py" "$dir/disk-guard.py" "$old" "$new" <<'PY' || { echo "mutation $name: could not apply (guard text changed?)"; rc=1; return; }
import sys
src, dst, old, new = sys.argv[1:5]
s = open(src).read()
if old not in s:
    sys.exit(1)
open(dst, "w").write(s.replace(old, new, 1))
PY
  if GUARD="$dir/disk-guard.py" python3 -W ignore "$HERE/test_disk_guard.py" >/dev/null 2>&1; then
    echo "mutation $name: SURVIVED (tests did not notice)"; rc=1
  else
    echo "mutation $name: killed"
  fi
}
mutate warn-never            'elif free < warn:' 'elif False:'
mutate crit-never            'if free < crit:' 'if False:'
mutate warn-boundary         'elif free < warn:' 'elif free <= warn:'
mutate crit-boundary         'if free < crit:' 'if free <= crit:'
mutate unreadable-is-ok      'print(line)
        log(line)
        return 2' 'print(line)
        log(line)
        return 0'
mutate bad-setting-passes    'print("disk-guard: bad setting: %s" % e)
        return 2' 'print("disk-guard: bad setting: %s" % e)
        return 0'
mutate crit-above-warn-ok    'if crit > warn:' 'if False:'
mutate override-parse-opens  'return int(ov) if ov.isdigit() else None' 'return int(ov) if ov.isdigit() else 999'
mutate count-by-substring    'if any(os.path.basename(w) in BUILD_TOOLS for w in words):' 'if any(t in ln for t in BUILD_TOOLS):'
mutate count-all-words       'words = ln.split()[:3]' 'words = ln.split()'
mutate count-changes-exit    'verdict, code, note = "OK", 0, "builds allowed"' 'verdict, code, note = "OK", (1 if builds > 3 else 0), "builds allowed"'
mutate no-log                'f.write("%s %s\n" % (datetime.datetime.now().isoformat(timespec="seconds"), line))' 'pass'
mutate usage-passes          '            print(__doc__)
            return 2' '            print(__doc__)
            return 0'
[ $rc -eq 0 ] && echo "disk-guard: mutation check PASS" || echo "disk-guard: mutation check FAIL"
exit $rc
