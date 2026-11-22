#!/usr/bin/env bash
# Runs the groomer tests in a fresh scratch HOME, alone.
#   bash test.sh            run the tests
#   bash test.sh --mutate   also break each guard in a temp copy and check the tests notice
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT
export HOME="$SCRATCH/home"; mkdir -p "$HOME"
unset HUBKIT_HOME GROOM_STATE_DIR MACHINE_SOURCES PROTECTED_SOURCES
export PYTHONDONTWRITEBYTECODE=1

command -v python3 >/dev/null || { echo "FAIL: python3 not found"; exit 1; }
python3 "$HERE/test_groomer.py" || { echo "FAIL: groomer tests"; exit 1; }
echo "groomer: tests PASS"

[ "${1:-}" = "--mutate" ] || exit 0

rc=0
mutate() {  # name, file, old text, new text
  local name="$1" file="$2" old="$3" new="$4" dir="$SCRATCH/mut-$1"
  mkdir -p "$dir"
  for f in groomer.py test_groomer.py; do cp "$HERE/$f" "$dir/$f"; done
  python3 - "$dir/$file" "$old" "$new" <<'PY' || { echo "mutation $name: could not apply (guard text changed?)"; rc=1; return; }
import sys
p, old, new = sys.argv[1:4]
s = open(p).read()
if old not in s:
    sys.exit(1)
open(p, "w").write(s.replace(old, new, 1))
PY
  if python3 "$dir/test_groomer.py" >/dev/null 2>&1; then
    echo "mutation $name: SURVIVED (tests did not notice)"; rc=1
  else
    echo "mutation $name: killed"
  fi
}
mutate missing-dir-live groomer.py 'if not state.is_dir():
            return "shadow"' 'if not state.is_dir():
            return "live"'
mutate shadow-writes groomer.py 'if m == "live":
            res["kind"] = kind' 'if True:
            res["kind"] = kind'
mutate id-check-off groomer.py 'or ident_tokens(t) != ident_tokens(rt)' ''
mutate person-folded groomer.py 'and source in machine_sources() and not person and' 'and source in machine_sources() and'
mutate done-items-match groomer.py 'r.get("status") not in ("open", "active")' 'False'
mutate short-title-ok groomer.py 'MIN_TOKENS = 3' 'MIN_TOKENS = 1'
mutate grade-guard-off groomer.py 'return int(grade) <= 8' 'return int(grade) <= 10'
mutate bad-grade-folds groomer.py '    except (TypeError, ValueError):
        return False' '    except (TypeError, ValueError):
        return True'
mutate own-ticket-folds groomer.py 'and not own_ticket ' ''
mutate gate-note-folds groomer.py 'and not gate_note ' ''
mutate origin-folds groomer.py 'and not origin
' '
'
mutate asked-by-ignored groomer.py 'person = is_protected(source, asked_by)' 'person = source in protected_sources()'
mutate candidate-asked-by-ignored groomer.py ' or is_protected(r.get("source"), r.get("asked_by"))):' '):'
mutate threshold-low groomer.py 'FOLD_THRESHOLD = 0.7' 'FOLD_THRESHOLD = 0.3'
mutate shadow-folds groomer.py '            if m == "live":
                res["fold_to"] = sim[0][0]' '            if True:
                res["fold_to"] = sim[0][0]'
mutate grade-table groomer.py '"operator-chat": 7, "operator-tap": 7' '"operator-chat": 5, "operator-tap": 7'
mutate sibling-ignored groomer.py 'return m.suggest(title, done_means, source) if m else None' 'return None'
mutate similar-traceback groomer.py 'print("groomer: cannot read --items (need a readable JSON file)", file=sys.stderr)
        return 2' 'raise'
[ $rc -eq 0 ] && echo "groomer: mutation check PASS" || echo "groomer: mutation check FAIL"
exit $rc
