#!/usr/bin/env bash
# Runs the card-guard tests in a fresh scratch HOME, alone.
#   bash test.sh            run the tests
#   bash test.sh --mutate   also break each guard in a temp copy and check the tests notice
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT
export HOME="$SCRATCH/home"; mkdir -p "$HOME"
unset HUBKIT_HOME WORKER_CARD_STATE WORKER_CARD_ANSWERS WORKER_CARD_QUEUE WORKER_CARD_RUN CLAUDE_CODE_SESSION_ID
export PYTHONDONTWRITEBYTECODE=1

command -v python3 >/dev/null || { echo "FAIL: python3 not found"; exit 1; }
python3 "$HERE/test_card_guard.py" || { echo "FAIL: card-guard tests"; exit 1; }
echo "card-guard: tests PASS"

[ "${1:-}" = "--mutate" ] || exit 0

rc=0
mutate() {  # name, file, old text, new text
  local name="$1" file="$2" old="$3" new="$4" dir="$SCRATCH/mut-$1"
  mkdir -p "$dir"
  for f in card_guard.py worker_card.py test_card_guard.py; do cp "$HERE/$f" "$dir/$f"; done
  python3 - "$dir/$file" "$old" "$new" <<'PY' || { echo "mutation $name: could not apply (guard text changed?)"; rc=1; return; }
import sys
p, old, new = sys.argv[1:4]
s = open(p).read()
if old not in s:
    sys.exit(1)
open(p, "w").write(s.replace(old, new, 1))
PY
  if python3 "$dir/test_card_guard.py" >/dev/null 2>&1; then
    echo "mutation $name: SURVIVED (tests did not notice)"; rc=1
  else
    echo "mutation $name: killed"
  fi
}
mutate ticket-id-rule card_guard.py '(r"(?i)\bL-\d{2,}\b", "a ticket id like L-0002"),' ''
mutate proof-not-required card_guard.py '    if require_proof:' '    if False:'
mutate why-not-required card_guard.py '    if require_why:' '    if False:'
mutate option-desc-optional card_guard.py 'if not lab or not desc:' 'if not lab:'
mutate cap-off worker_card.py 'if len(ids) >= CAP:' 'if False:'
mutate answered-check-off worker_card.py 'if answered_duplicate(a.header):' 'if False:'
mutate recommended-optional worker_card.py 'if a.recommended is None:' 'if False:'
mutate guard-skipped worker_card.py 'problem = card_guard.check(a.header, ctx, options, require_why=True, require_proof=True)' 'problem = None'
mutate corrupt-ledger-resets worker_card.py 'be counted)" % os.path.basename(ledger), file=sys.stderr)
            return 1' 'be counted)" % os.path.basename(ledger), file=sys.stderr)
            rows = []'
mutate nan-time-resets worker_card.py ' or not math.isfinite(float(r.get("t") or 0))' ''
mutate nondict-row-skipped worker_card.py 'if not isinstance(r, dict) or' 'if False and'
mutate unwritable-ledger-ignored worker_card.py '        except OSError:
            print("worker-card: refused: cannot write the cap ledger", file=sys.stderr)
            return 1' '        except OSError:
            pass'
mutate window-huge worker_card.py 'WINDOW_S = 12 * 3600' 'WINDOW_S = 12 * 3600 * 1000'
mutate answered-at-optional worker_card.py 'if not isinstance(r, dict) or not r.get("answered_at"):' 'if not isinstance(r, dict):'
mutate exact-text-dup-off worker_card.py 'if norm(h) == norm(header):' 'if h == header:'
mutate option-count-open worker_card.py 'if not 2 <= len(a.option) <= 3:' 'if not 2 <= len(a.option) <= 9:'
mutate option-label-len worker_card.py 'len(lab) > 60' 'len(lab) > 600'
mutate field-len-open worker_card.py 'if len(v) > MAX_FIELD:' 'if False:'
mutate total-len-open worker_card.py 'MAX_TOTAL = 100, 400, 1200' 'MAX_TOTAL = 100, 400, 12000'
[ $rc -eq 0 ] && echo "card-guard: mutation check PASS" || echo "card-guard: mutation check FAIL"
exit $rc
