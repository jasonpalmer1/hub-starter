#!/usr/bin/env bash
# Runs the gate-metrics tests in a fresh scratch HOME, alone, with nothing else installed.
#   bash test.sh            run the tests
#   bash test.sh --mutate   also break each rule in a temp copy and check the tests notice
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT
export HOME="$SCRATCH/home"; mkdir -p "$HOME"
unset HUBKIT_HOME GATE_SPAWNS_LOG GATE_METRICS
export PYTHONDONTWRITEBYTECODE=1

command -v python3 >/dev/null || { echo "FAIL: python3 not found"; exit 1; }
python3 -W ignore "$HERE/test_gate_metrics.py" || { echo "FAIL: gate-metrics tests"; exit 1; }
echo "gate-metrics: tests PASS"

[ "${1:-}" = "--mutate" ] || exit 0

rc=0
mutate() {  # name, old text, new text
  local name="$1" dir="$SCRATCH/mut-$1"
  mkdir -p "$dir"
  cp "$HERE/gate_metrics.py" "$dir/gate_metrics.py"
  python3 - "$dir/gate_metrics.py" "$2" "$3" <<'PY' || { echo "mutation $name: could not apply (rule text changed?)"; rc=1; return; }
import sys
p, old, new = sys.argv[1:4]
s = open(p).read()
if old not in s:
    sys.exit(1)
open(p, "w").write(s.replace(old, new, 1))
PY
  if GATE_METRICS="$dir/gate_metrics.py" python3 -W ignore "$HERE/test_gate_metrics.py" >/dev/null 2>&1; then
    echo "mutation $name: SURVIVED (tests did not notice)"; rc=1
  else
    echo "mutation $name: killed"
  fi
}
mutate counts-pending 'COUNTED = ("PASS", "PASS WITH NOTES", "FAIL")' 'COUNTED = ("PASS", "PASS WITH NOTES", "FAIL", "PENDING", "MAYBE")'
mutate last-gate-not-first 'min(c, key=lambda g: (g["round"], g["mtime"]))' 'max(c, key=lambda g: (g["round"], g["mtime"]))'
mutate round-ignored 'min(c, key=lambda g: (g["round"], g["mtime"]))' 'min(c, key=lambda g: g["mtime"])'
mutate mtime-tiebreak-newest 'min(c, key=lambda g: (g["round"], g["mtime"]))' 'min(c, key=lambda g: (g["round"], -g["mtime"]))'
mutate slice-ignored 'chains.setdefault((g["ticket"], g["slice"]), [])' 'chains.setdefault((g["ticket"],), [])'
mutate each-gate-a-chain 'chains.setdefault((g["ticket"], g["slice"]), [])' 'chains.setdefault((g["ticket"], g["slice"], g["round"]), [])'
mutate symlinks-followed 'and not os.path.islink(p)' ''
mutate any-md-counts 'if (f == "VERDICT.md" or f.endswith("-VERDICT.md"))' 'if (f.endswith(".md"))'
mutate ticket-not-checked 'if v not in COUNTED or not TICKET.match(t):' 'if v not in COUNTED:'
mutate target-ignored 'return 1 if over else 0' 'return 0'
mutate target-inclusive 'pct > a.target' 'pct >= a.target'
mutate corrupt-crashes 'except (ValueError, AttributeError):' 'except ValueError:'
mutate nonstring-type-crashes '                if t is not None and not isinstance(t, str):' '                if False:'
mutate unreadable-index-silent '    except OSError as e:' '    except OSError as e:
        return n'
mutate unreadable-root-silent 'if not (os.path.isdir(root) and os.access(root, os.R_OK | os.X_OK)):' 'if False:'
mutate corrupt-counted-as-spawn 'n["corrupt"] += 1' 'n["builder"] += 1'
mutate share-wrong-base '/ len(firsts), 1)' '/ len(counted), 1)'
mutate spawn-type-any 'if t in n:' 'if t:'
mutate depth-unbounded 'if dp.count(os.sep) - base >= 4:' 'if False:'
[ $rc -eq 0 ] && echo "gate-metrics: mutation check PASS" || echo "gate-metrics: mutation check FAIL"
exit $rc
