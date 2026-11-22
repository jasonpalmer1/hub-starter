#!/usr/bin/env bash
# Runs the wake-gate tests in a fresh scratch HOME, alone, with nothing else installed.
#   bash test.sh            run the tests
#   bash test.sh --mutate   also break each rule in a temp copy and check the tests notice
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT
export HOME="$SCRATCH/home"; mkdir -p "$HOME"
unset HUBKIT_HOME WAKE_GATE WAKE_GATE_NOW
export PYTHONDONTWRITEBYTECODE=1

command -v python3 >/dev/null || { echo "FAIL: python3 not found"; exit 1; }
python3 -W ignore "$HERE/test_wake_gate.py" || { echo "FAIL: wake-gate tests"; exit 1; }
echo "wake-gate: tests PASS"

[ "${1:-}" = "--mutate" ] || exit 0

# Each mutation edits ONE rule in a temp copy of the tool; the suite must then FAIL.
rc=0
mutate() {  # name, old text, new text
  local name="$1" dir="$SCRATCH/mut-$1"
  mkdir -p "$dir"
  cp "$HERE/wake_gate.py" "$dir/wake_gate.py"
  python3 - "$dir/wake_gate.py" "$2" "$3" <<'PY' || { echo "mutation $name: could not apply (rule text changed?)"; rc=1; return; }
import sys
p, old, new = sys.argv[1:4]
s = open(p).read()
if old not in s:
    sys.exit(1)
open(p, "w").write(s.replace(old, new, 1))
PY
  if WAKE_GATE="$dir/wake_gate.py" python3 -W ignore "$HERE/test_wake_gate.py" >/dev/null 2>&1; then
    echo "mutation $name: SURVIVED (tests did not notice)"; rc=1
  else
    echo "mutation $name: killed"
  fi
}
mutate repeat-wakes 'if not is_new(seen.get(key), now):' 'if False:'
mutate window-1h 'WINDOW_S = 24 * 3600' 'WINDOW_S = 3600'
mutate window-48h 'WINDOW_S = 24 * 3600' 'WINDOW_S = 48 * 3600'
mutate warn-wakes 'eligible = sev == "crit" or dead' 'eligible = sev in ("crit", "warn", "info") or dead'
mutate dead-lane-ignored 'eligible = sev == "crit" or dead' 'eligible = sev == "crit"'
mutate dead-lane-kind-ignored 'or one_line(finding.get("kind"), 80) == DEAD_LANE_KIND' ''
mutate severity-case 'sev = one_line(finding.get("severity"), 10).lower()' 'sev = one_line(finding.get("severity"), 10)'
mutate wake-not-remembered 'write_seen(path, seen)
                    verdict' 'verdict'
mutate unwritable-still-wakes 'reason = "memory unwritable (%s); not waking"' 'verdict = "WAKE"; reason = "memory unwritable (%s); not waking"'
mutate unwritable-silent 'print("wake-gate: UNRECORDED %s %s: %s" % (sev, key, summary), file=sys.stderr)' 'pass'
mutate corrupt-crashes 'except (OSError, ValueError):
        return {}, True' 'except OSError:
        return {}, True'
mutate newline-kept 'return re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f\x7f]", " ", str(text if text is not None else ""))).strip()[:limit]' 'return str(text if text is not None else "")[:limit]'
mutate clock-back-wakes 'return -age > WINDOW_S' 'return True'
mutate future-stamp-blocks-forever 'return -age > WINDOW_S' 'return False'
mutate subject-ignored 'if finding.get("subject"):' 'if False:'
mutate no-event 'emit("wake.decision",' 'emit("wake.decisionX",'
mutate non-atomic-write 'os.replace(tmp, path)' 'open(path, "w").write(open(tmp).read())'
mutate no-prune 'seen = {k: v for k, v in seen.items() if -KEEP_S < now - v < KEEP_S}' 'pass'
[ $rc -eq 0 ] && echo "wake-gate: mutation check PASS" || echo "wake-gate: mutation check FAIL"
exit $rc
