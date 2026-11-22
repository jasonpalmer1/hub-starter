#!/usr/bin/env bash
# Runs the sweep tests in a fresh scratch HOME, alone, with nothing else installed.
#   bash test.sh            run the tests
#   bash test.sh --mutate   also break each rule in a temp copy and check the tests notice
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT
export HOME="$SCRATCH/home"; mkdir -p "$HOME"
unset LEDGER_PATH HUBKIT_HOME SWEEP SWEEP_NOW SWEEP_LIVE_OWNERS
export PYTHONDONTWRITEBYTECODE=1

command -v python3 >/dev/null || { echo "FAIL: python3 not found"; exit 1; }
python3 -W ignore "$HERE/test_sweep.py" || { echo "FAIL: sweep tests"; exit 1; }
echo "sweep: tests PASS"

[ "${1:-}" = "--mutate" ] || exit 0

# Each mutation edits ONE rule in a temp copy of the tool; the suite must then FAIL.
rc=0
mutate() {  # name, old text, new text
  local name="$1" dir="$SCRATCH/mut-$1"
  mkdir -p "$dir/sweep" "$dir/ledger" "$dir/wake-gate"
  cp "$HERE/../wake-gate/wake_gate.py" "$dir/wake-gate/wake_gate.py"
  cp "$HERE/sweep.py" "$dir/sweep/sweep.py"
  cp "$HERE/../ledger/ledger_lib.py" "$dir/ledger/ledger_lib.py"
  python3 - "$dir/sweep/sweep.py" "$2" "$3" <<'PY' || { echo "mutation $name: could not apply (rule text changed?)"; rc=1; return; }
import sys
p, old, new = sys.argv[1:4]
s = open(p).read()
if old not in s:
    sys.exit(1)
open(p, "w").write(s.replace(old, new, 1))
PY
  if SWEEP="$dir/sweep/sweep.py" python3 -W ignore "$HERE/test_sweep.py" >/dev/null 2>&1; then
    echo "mutation $name: SURVIVED (tests did not notice)"; rc=1
  else
    echo "mutation $name: killed"
  fi
}
mutate unknown-is-dead 'if live is None:
        return []' 'if live is None:
        live = (set(), float("inf"))'
mutate orphans-unknown-proceeds 'if live is None:
            say("sweep: liveness unknown' 'if False:
            say("sweep: liveness unknown'
mutate alive-owner-taken 'or owner in names:' ':'
mutate prefix-match 'owner in names:' 'any(owner.startswith(n) or n.startswith(owner) for n in names):'
mutate owner-case 'return str(name or "").strip().lower()' 'return str(name or "")'
mutate no-end-line-check 'or lines[-1] != "# end"' ''
mutate no-header-check 'if not m or' 'if False and'
mutate stale-list-ok 'now - at > LIVE_MAX_AGE_S' 'False'
mutate future-list-ok 'at > now + 60 or' ''
mutate empty-list-ok 'if not names:' 'if False:'
mutate symlink-ok 'if not stat.S_ISREG(os.lstat(path).st_mode):' 'if False:'
mutate protected-owner-taken 'PROTECTED = ("", "unowned", "operator")' 'PROTECTED = ("",)'
mutate needs-operator-taken 'rec.get("status") not in ("open", "active")' 'rec.get("status") in ("done", "dropped")'
mutate hold-ignored 'if held(owner) or' 'if'
mutate hold-never-expires 'return until != until or now_s() < until' 'return True'
mutate bad-hold-file-allows 'except (OSError, ValueError, KeyError, TypeError):
        return True' 'except (OSError, ValueError, KeyError, TypeError):
        return False'
mutate claim-after-list-taken 'or ts_epoch(rec) >= at' ''
mutate young-ticket-taken 'or now - ts_epoch(rec) < MIN_TICKET_AGE_S' ''
mutate bad-ledger-reassigns 'if bad2:' 'if False:'
mutate no-recheck-under-lock 'if (tid, owner) not in find_orphans({tid: fresh.get(tid, {})}, live, read_hold, now):' 'if False:'
mutate no-sweep-lock 'fcntl.flock(lockfh, fcntl.LOCK_EX | fcntl.LOCK_NB)' 'pass'
mutate reassign-without-flag 'if not args.reassign:' 'if False:'
mutate status-not-reopened '"status": "open",
                                         "owner": "unowned"' '"status": "active",
                                         "owner": "unowned"'
mutate warn-wakes 'severity="warn", subject=clean(tid),
                          summary="%s untouched' 'severity="crit", subject=clean(tid),
                          summary="%s untouched'
mutate unknown-wakes 'kind="liveness-unknown", severity="warn"' 'kind="liveness-unknown", severity="crit"'
mutate dead-owner-wakes 'f.append(dict(kind="dead-owner", severity="warn"' 'f.append(dict(kind=DEAD_KIND, severity="warn"'
mutate operator-ask-no-wake 'f.append(dict(kind=DEAD_KIND,' 'f.append(dict(kind="dead-owner",'
mutate done-no-proof-silent 'if rec.get("status") == "done" and not ledger_lib.has_done_gate_proof(rec):' 'if False:'
mutate stale-silent 'elif ledger_lib.is_stale(rec):' 'elif False:'
mutate newline-kept 'return re.sub(r"[\x00-\x1f\x7f\s]+", " ", str(text)).strip()[:n]' 'return str(text)[:n]'
mutate no-event 'emit("orphans.reassigned"' 'emit("orphans.reassignedX"'
mutate hold-no-expiry-check 'if args.minutes is None or not (0 < args.minutes <= 7 * 24 * 60):' 'if args.minutes is None:'
mutate comment-name-read-dead '    for ln in lines[1:-1]:
        if ln.startswith("#"):' '    for ln in []:
        if ln.startswith("#"):'
mutate no-name-check 'if not all(NAME_RE.match(n) for n in names):' 'if False:'
mutate hold-recheck-dropped 'find_orphans({tid: fresh.get(tid, {})}, live, read_hold, now)' 'find_orphans({tid: fresh.get(tid, {})}, live, lambda _: False, now)'
mutate nan-hold-not-held 'return until != until or now_s() < until' 'return now_s() < until'
mutate json-stdout-noisy 'print(text, file=sys.stderr if args.json else sys.stdout)' 'print(text)'
mutate nonstring-id-kept 'if isinstance(v, dict) and "id" in v and not isinstance(v["id"], str):' 'if False:'
mutate summary-id-raw 'summary="%s owned by dead %s" % (clean(tid), clean' 'summary="%s owned by dead %s" % (tid, clean'
[ $rc -eq 0 ] && echo "sweep: mutation check PASS" || echo "sweep: mutation check FAIL"
exit $rc
