#!/usr/bin/env bash
# Runs the gate-agents structure tests in a fresh scratch HOME, alone.
#   bash test.sh            run the tests
#   bash test.sh --mutate   also damage each file in a temp copy and check the tests notice
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT
export HOME="$SCRATCH/home"; mkdir -p "$HOME"
export PYTHONDONTWRITEBYTECODE=1
command -v python3 >/dev/null || { echo "FAIL: python3 not found"; exit 1; }
python3 "$HERE/test_gate_agents.py" || { echo "FAIL: gate-agents tests"; exit 1; }
echo "gate-agents: tests PASS"
[ "${1:-}" = "--mutate" ] || exit 0

rc=0
mutate() {  # name, file, old text, new text
  local name="$1" file="$2" old="$3" new="$4" dir="$SCRATCH/mut-$1"
  mkdir -p "$dir"; cp "$HERE"/*.md "$HERE"/*.json "$HERE"/test_gate_agents.py "$dir"/
  python3 - "$dir/$file" "$old" "$new" <<'PY' || { echo "mutation $name: could not apply"; rc=1; return; }
import sys
p, old, new = sys.argv[1:4]
s = open(p).read()
if old not in s:
    sys.exit(1)
open(p, "w").write(s.replace(old, new, 1))
PY
  if python3 "$dir/test_gate_agents.py" >/dev/null 2>&1; then
    echo "mutation $name: SURVIVED"; rc=1
  else
    echo "mutation $name: killed"
  fi
}
mutate edit-tool-allowed  bug-gate.md   'tools: Read, Grep, Glob, Bash, Write' 'tools: Read, Grep, Glob, Bash, Write, Edit'
mutate abs-path           planner.md    '~/.claude/hubkit/plans/<project>' '/Us''ers/x/.claude/hubkit/plans/<project>'
mutate no-acceptance      playtester.md 'Acceptance check FIRST' 'Acceptance check'
mutate no-mutation        bug-gate.md   'TEMPORARY COPY' 'copy'
mutate no-attacker        bug-gate.md   'held-out attacker' 'reviewer'
mutate no-channel         planner.md    'CHANNEL:' 'AUDIENCE:'
mutate pkill-rule-gone    playtester.md 'Never `pkill`' 'Feel free to pkill'
mutate bad-name           planner.md    'name: planner' 'name: plan'
mutate pattern-file-gone  pattern.json  '"playtester.md"]' '"missing.md"]'
[ $rc -eq 0 ] && echo "gate-agents: mutation check PASS" || echo "gate-agents: mutation check FAIL"
exit $rc
