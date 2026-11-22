#!/usr/bin/env bash
# Runs the report-gate tests in a fresh scratch HOME, alone, with nothing else installed.
#   bash test.sh            run the tests
#   bash test.sh --mutate   also break each guard in a temp copy and check the tests notice
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT
export HOME="$SCRATCH/home"; mkdir -p "$HOME"
unset HUBKIT_HOME REPORT_GATE_PROJECTS_ROOT REPORT_GATE_LOG_ALL GATE
export PYTHONDONTWRITEBYTECODE=1

command -v python3 >/dev/null || { echo "FAIL: python3 not found"; exit 1; }
python3 "$HERE/test_report_gate.py" || { echo "FAIL: report-gate tests"; exit 1; }
echo "report-gate: tests PASS"

[ "${1:-}" = "--mutate" ] || exit 0

# Each mutation edits ONE guard in a temp copy of the hook; the suite must then FAIL.
rc=0
mutate() {  # name, old text, new text
  local name="$1" old="$2" new="$3" dir="$SCRATCH/mut-$1"
  mkdir -p "$dir"
  python3 - "$HERE/report-gate.py" "$dir/report-gate.py" "$old" "$new" <<'PY' || { echo "mutation $name: could not apply (guard text changed?)"; rc=1; return; }
import sys
src, dst, old, new = sys.argv[1:5]
s = open(src).read()
if old not in s:
    sys.exit(1)
open(dst, "w").write(s.replace(old, new, 1))
PY
  if GATE="$dir/report-gate.py" python3 "$HERE/test_report_gate.py" >/dev/null 2>&1; then
    echo "mutation $name: SURVIVED (tests did not notice)"; rc=1
  else
    echo "mutation $name: killed"
  fi
}
mutate release-wiring-removed   'ticket_lock.release_and_audit(pt, tp, aid)' 'pass'
mutate release-on-refused-stop   '    if rc == 0:
        release_ticket_locks(raw)' '    release_ticket_locks(raw)'
mutate empty-report-passes        'return bool(f.read().strip())' 'return True'
mutate preexisting-passes         'return birth_ms >= floor and st.st_mtime * 1000 >= floor' 'return True'
mutate birth-time-ignored         'return birth_ms >= floor and st.st_mtime * 1000 >= floor' 'return st.st_mtime * 1000 >= floor'
mutate pick-first-not-deliverable 'return cued[-1].group(0), "cued"' 'return matches[0].group(0), "cued"'
mutate last-path-rule             'return matches[-1].group(0), "last"' 'return matches[0].group(0), "last"'
mutate refuse-forever             'if strike >= 2:' 'if strike >= 999:'
mutate refuse-once-off            'if strike >= 2:' 'if strike >= 1:'
mutate fail-open-on-bug           'print("report-gate: internal error, failing closed - see state/report-gate.log", file=sys.stderr)
        return 2' 'return 0'
mutate quoted-exempt              'stripped = re.sub(r'"'"'"""[\s\S]*?"""'"'"', "", text)' 'stripped = text'
mutate exempt-anywhere            'EXEMPT_RX = re.compile(r"^\s*report\s*:\s*none\.?\s*$", re.I | re.M)' 'EXEMPT_RX = re.compile(r"report\s*:\s*none", re.I)'
mutate shared-fallback-file       'safe = re.sub(r"[^\w\-]", "_", agent_id or "unknown-agent")' 'safe = "shared"'
mutate counter-failure-opens      'treating as first: {e}")
        return 1' 'treating as first: {e}")
        return 99'
mutate no-type-allows-typed       'if not agent_type and not tpath:' 'if not tpath:'
mutate id-traversal               'if not re.fullmatch(r"[\w\-]+", session_id or "") or not re.fullmatch(r"[\w\-]+", agent_id or ""):
        return None' 'pass'
[ $rc -eq 0 ] && echo "report-gate: mutation check PASS" || echo "report-gate: mutation check FAIL"
exit $rc
