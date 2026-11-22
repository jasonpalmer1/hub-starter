#!/usr/bin/env bash
# Runs the ledger tests in a fresh scratch HOME, alone, with nothing else installed.
#   bash test.sh            run the tests
#   bash test.sh --mutate   also break each guard in a temp copy and check the tests notice
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT
export HOME="$SCRATCH/home"; mkdir -p "$HOME"
unset LEDGER_PATH HUBKIT_HOME CLAUDE_PID CLAUDE_CODE_SESSION_ID
export PYTHONDONTWRITEBYTECODE=1

command -v python3 >/dev/null || { echo "FAIL: python3 not found"; exit 1; }
python3 "$HERE/test_ledger.py" || { echo "FAIL: ledger tests"; exit 1; }
echo "ledger: tests PASS"

[ "${1:-}" = "--mutate" ] || exit 0

# Each mutation edits ONE guard in a temp copy; the suite must then FAIL.
rc=0
mutate() {  # name, file, old text, new text
  local name="$1" file="$2" old="$3" new="$4" dir="$SCRATCH/mut-$1"
  mkdir -p "$dir"
  for f in ledger ledger_lib.py ledger-banner-hook.py test_ledger.py; do cp "$HERE/$f" "$dir/$f"; done
  python3 - "$dir/$file" "$old" "$new" <<'PY' || { echo "mutation $name: could not apply (guard text changed?)"; rc=1; return; }
import sys
p, old, new = sys.argv[1:4]
s = open(p).read()
if old not in s:
    sys.exit(1)
open(p, "w").write(s.replace(old, new, 1))
PY
  if python3 "$dir/test_ledger.py" >/dev/null 2>&1; then
    echo "mutation $name: SURVIVED (tests did not notice)"; rc=1
  else
    echo "mutation $name: killed"
  fi
}
mutate done-gate-off ledger 'args.new_status == "done" and not has_done_gate_proof(rec)' 'False'
mutate metric-claim-counts ledger_lib.py 'frozenset({"metric-claim", "metric-claim-verdict"})' 'frozenset()'
mutate highwater-off ledger_lib.py 'max_n = max(max_n, _read_highwater())' 'pass'
mutate rubber-stamp ledger_lib.py 'return rec["status"] == "needs-operator"' 'return rec["status"] == "needs-operator" or (rec["status"] == "done" and rec["asked_by"] == "operator" and not rec["operator_ack"])'
mutate stale-hours ledger_lib.py 'STALE_HOURS = 48' 'STALE_HOURS = 480'
mutate reviewed-mark ledger_lib.py 'rec["reviewed_upto"] = len(rec["schema_issues"])' 'rec["reviewed_upto"] = 0'
mutate mix-drops-count ledger_lib.py 'counts[normalize_class(rec.get("triage_class"))] += 1' 'pass'
mutate mix-unknown-class ledger_lib.py 'return v if v in MIX_CLASSES else MIX_UNCLASSIFIED' 'return v or MIX_UNCLASSIFIED'
mutate generic-allowlist-open ledger_lib.py 'return sorted(k for k in row if k not in GENERIC_ROW_FIELDS)' 'return []'
mutate triage-first-wins ledger_lib.py 'rec["triage_class"] = e.get("class")' 'rec["triage_class"] = rec["triage_class"] or e.get("class")'
mutate triage-no-class-check ledger_lib.py 'if kind == "triage" and not str(event.get("class") or "").strip():' 'if False:'
mutate hook-fails-closed ledger-banner-hook.py '    except Exception:
        return []
    if not records:' '    except Exception:
        raise
    if not records:'
mutate extras-exit-leaks ledger-banner-hook.py '    except BaseException:  # incl. SystemExit' '    except Exception:  # incl. SystemExit'
mutate extras-not-printed ledger-banner-hook.py '*content, *extras, "</ledger-status>"' '*content, "</ledger-status>"'
mutate extras-default-path ledger-banner-hook.py 'HERE / "banner_extras.py"' 'HERE / "banner_extra.py"'
[ $rc -eq 0 ] && echo "ledger: mutation check PASS" || echo "ledger: mutation check FAIL"
exit $rc
