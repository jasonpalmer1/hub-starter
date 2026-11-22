#!/usr/bin/env bash
# Runs the work-loop tests in a fresh scratch HOME, alone, with nothing else installed.
#   bash test.sh            run the tests
#   bash test.sh --mutate   also break each rule in a temp copy and check the tests notice
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT
export HOME="$SCRATCH/home"; mkdir -p "$HOME"
unset LEDGER_PATH HUBKIT_HOME WORK_LOOP WORK_LOOP_NOW WORK_LOOP_HOLD_S STOP_ARM HUBKIT_LANE
export PYTHONDONTWRITEBYTECODE=1

command -v python3 >/dev/null || { echo "FAIL: python3 not found"; exit 1; }
python3 -W ignore "$HERE/test_work_loop.py" || { echo "FAIL: work-loop tests"; exit 1; }
python3 -W ignore "$HERE/test_tick.py" || { echo "FAIL: work-loop tick tests"; exit 1; }
python3 -W ignore "$HERE/test_stop_arm.py" || { echo "FAIL: work-loop stop-arm tests"; exit 1; }
echo "work-loop: tests PASS"

[ "${1:-}" = "--mutate" ] || exit 0

# Each mutation edits ONE rule in a temp copy of the tool; the suite must then FAIL.
rc=0
mutate() {  # name, old text, new text
  local name="$1" dir="$SCRATCH/mut-$1"
  mkdir -p "$dir/work-loop" "$dir/ledger" "$dir/wake-gate"
  cp "$HERE/../wake-gate/wake_gate.py" "$dir/wake-gate/wake_gate.py"
  cp "$HERE/work_loop.py" "$dir/work-loop/work_loop.py"
  cp "$HERE/../ledger/ledger_lib.py" "$dir/ledger/ledger_lib.py"
  python3 - "$dir/work-loop/work_loop.py" "$2" "$3" <<'PY' || { echo "mutation $name: could not apply (rule text changed?)"; rc=1; return; }
import sys
p, old, new = sys.argv[1:4]
s = open(p).read()
if old not in s:
    sys.exit(1)
open(p, "w").write(s.replace(old, new, 1))
PY
  if WORK_LOOP="$dir/work-loop/work_loop.py" python3 -W ignore "$HERE/test_work_loop.py" >/dev/null 2>&1 \
     && WORK_LOOP="$dir/work-loop/work_loop.py" python3 -W ignore "$HERE/test_tick.py" >/dev/null 2>&1; then
    echo "mutation $name: SURVIVED (tests did not notice)"; rc=1
  else
    echo "mutation $name: killed"
  fi
}
mutate allow-other-owner 'if not own and owner not in ("", "unowned"):' 'if False:'
mutate allow-active 'if rec.get("status") != "open":' 'if rec.get("status") not in ("open", "active"):'
mutate allow-needs-operator 'if rec.get("status") != "open":' 'if rec.get("status") not in ("open", "needs-operator"):'
mutate allow-trigger 'if rec.get("trigger"):' 'if False:'
mutate offer-window-off 'now - float(off.get("at", 0) or 0) < OFFER_WINDOW_S' 'False'
mutate offer-window-48h 'OFFER_WINDOW_S = 24 * 3600' 'OFFER_WINDOW_S = 1'
mutate no-flock 'fcntl.flock(lockfh, fcntl.LOCK_EX)' 'pass'
mutate owner-case 'return str(name or "").strip().lower()' 'return str(name or "")'
mutate id-sort-text 'return (0, int(m.group(1)), tid) if m else (1, 0, tid)' 'return (0, 0, tid)'
mutate operator-not-first '0 if rec.get("asked_by") == "operator" else 1' '1'
mutate own-lane-not-first '0 if own else 1, 0 if rec' '1, 0 if rec'
mutate corrupt-line-crash '                bad.append(n)
                continue
            if isinstance' '                raise
            if isinstance'
mutate peek-records 'if tid and not args.peek:' 'if tid:'
mutate registry-unknown-lane 'if lane not in registry:' 'if False:'
mutate idle-guard-off 'if now - touch_at < FEED_MIN_IDLE_S:' 'if False:'
mutate idle-guard-5min 'FEED_MIN_IDLE_S = 10 * 60' 'FEED_MIN_IDLE_S = 5 * 60'
mutate rate-limit-off 'if last_feed_at is not None and now - last_feed_at < FEED_RATE_S:' 'if False:'
mutate rate-limit-10min 'FEED_RATE_S = 30 * 60' 'FEED_RATE_S = 10 * 60'
mutate hold-ignored 'if hold_bad or (hold_until is not None and now < hold_until):' 'if False:'
mutate hold-never-expires 'now < hold_until' 'True'
mutate hold-bad-file-allows 'if hold_bad or (' 'if (hold_bad and False) or ('
mutate future-touch-is-idle 'if touch_at > now:' 'if False:'
mutate never-touched-is-idle 'if touch_at is None:' 'if False:'
mutate no-claim-recheck 'if eligible(tid, rec2, mine, projects, offers, now) is None:' 'if False:'
mutate lane-name-unchecked 'return bool(LANE_RE.match(name or ""))' 'return bool(name)'
mutate argv-unchecked 'and all(isinstance(a, str) and ARG_RE.match(a) for a in argv))' 'and all(isinstance(a, str) for a in argv))'
mutate dry-run-writes 'if dry:    # read-only: no locks, no files, no events' 'if False:'
mutate dry-run-feeds-inside 'if dry:
            return False, tid, "would feed", open_exists' 'if False:
            return False, tid, "would feed", open_exists'
mutate starve-every-tick 'if isinstance(seen, (int, float)) and now - seen < STARVE_ONCE_S:' 'if False:'
mutate starve-under-30 'elif now - touch_at >= STARVED_S:' 'elif True:'
mutate tick-no-flock 'locked("tick.lock", blocking=False)' 'locked("tick.lock.x" + str(os.getpid()), blocking=False)'
mutate cmd-through-shell 'shell=False' 'shell=True'
mutate skip-bad-registry-name 'if not valid_lane(lane):
            print("skip' 'if False:
            print("skip'
mutate failed-delivery-recorded 'if not ok:
            emit("loop.feed_failed"' 'if False:
            emit("loop.feed_failed"'
mutate starvation-skips-wake-gate 'wg = load_wake_gate()' 'wg = None'
mutate no-heartbeat 'for sub, text in (("heartbeats"' 'for sub, text in (("heartbeatsX"'
# Stop arm: the same idea on stop_arm.py (a temp copy next to copies of work_loop.py and the ledger pattern).
mutate_stop() {  # name, old text, new text
  local name="$1" dir="$SCRATCH/mutstop-$1"
  mkdir -p "$dir/work-loop" "$dir/ledger" "$dir/wake-gate"
  cp "$HERE/../wake-gate/wake_gate.py" "$dir/wake-gate/wake_gate.py"
  cp "$HERE/work_loop.py" "$HERE/stop_arm.py" "$dir/work-loop/"
  cp "$HERE/../ledger/ledger_lib.py" "$dir/ledger/ledger_lib.py"
  python3 - "$dir/work-loop/${4:-stop_arm.py}" "$2" "$3" <<'PY' || { echo "mutation $name: could not apply (rule text changed?)"; rc=1; return; }
import sys
p, old, new = sys.argv[1:4]
s = open(p).read()
if old not in s:
    sys.exit(1)
open(p, "w").write(s.replace(old, new, 1))
PY
  if STOP_ARM="$dir/work-loop/stop_arm.py" python3 -W ignore "$HERE/test_stop_arm.py" >/dev/null 2>&1; then
    echo "mutation stop-$name: SURVIVED (tests did not notice)"; rc=1
  else
    echo "mutation stop-$name: killed"
  fi
}
mutate_stop cap-off 'if len(picked) >= CAP:' 'if False:'
mutate_stop cap-4 'CAP = 3 ' 'CAP = 4 '
mutate_stop claim-ignored 'owner = wl.norm(rec.get("owner"))' 'continue; owner = wl.norm(rec.get("owner"))'
mutate_stop pause-ignored 'if os.path.exists(os.path.join(sd, "loop-pause")):' 'if False:'
mutate_stop unregistered-continues 'if registry is None or lane not in registry:
        return None
    entry = registry[lane]' 'if registry is None:
        return None
    entry = registry.get(lane, {})'
mutate_stop no-registry-continues 'if registry is None or lane not in registry:
        return None
    entry = registry[lane]' 'entry = (registry or {}).get(lane, {})'
mutate_stop subagent-continues 'or payload.get("agent_id"):' 'or False:'
mutate_stop no-reset-on-close 'if any(records.get(t, {}).get("status") == "done" for t in picked):' 'if False:'
mutate_stop error-blocks 'except Exception:
        return 0                 # fail open: any error lets the chat stop' 'except Exception:
        sys.stdout.write(json.dumps({"decision": "block", "reason": "error"}))
        return 0'
mutate_stop offer-not-recorded 'wl.write_atomic(feed, {"sent": offers, "last_feed": last_feed})' 'pass'
mutate_stop own-lock-only 'with wl.locked("loop-feed.json.lock"):' 'with wl.locked("stop-arm.lock"):'
mutate_stop no-lock 'with wl.locked("loop-feed.json.lock"):' 'with wl.locked("stop-arm.lock" + str(os.getpid())):'
mutate_stop hold-ignored 'if hold_bad or (hold_until is not None and wl.now_s() < hold_until):' 'if False:'
mutate_stop hold-bad-file-allows 'if hold_bad or (' 'if (hold_bad and False) or ('
mutate_stop hold-never-expires 'wl.now_s() < hold_until' 'True'
mutate_stop owner-claim-ignored 'if (owner in mine and status in ("open", "active")) or (' 'if (False) or ('
mutate_stop active-unowned-any-lane-blocks 'and (tid0 in picked or (isinstance(offers.get(tid0), dict) and offers[tid0].get("lane") == lane))):' '):'
mutate_stop nan-hold-allows 'if until != until or until in' 'if False and until in' work_loop.py
mutate_stop active-unowned-ignored 'status == "active" and owner in ("", "unowned")' 'False and owner in ("", "unowned")'
mutate_stop done-owned-blocks 'status in ("open", "active")) or' 'True) or'
[ $rc -eq 0 ] && echo "work-loop: mutation check PASS" || echo "work-loop: mutation check FAIL"
exit $rc
