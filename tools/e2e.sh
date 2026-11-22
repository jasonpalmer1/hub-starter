#!/usr/bin/env bash
# End-to-end fresh-machine check: the 5-step scenario a stranger would follow, unattended, in a scratch HOME.
# Needs nothing from a live ~/.claude: HOME is pointed at a temp folder and the kit/ledger env overrides are unset;
# the final check fails if the real hubkit folder changed during the run.
#   bash tools/e2e.sh          prints one PASS/FAIL line per step, exit 0 only if every step passed
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$HERE")"
REAL_HOME="$HOME"
T="$(mktemp -d "${TMPDIR:-/tmp}/hubkit-e2e.XXXXXX")" || { echo "e2e: FAIL cannot make a temp folder"; exit 2; }
trap 'rm -rf "$T"' EXIT
export HOME="$T/home"
mkdir -p "$HOME"
unset HUBKIT_HOME HUBKIT_LANE WORK_LOOP_NOW IMPROVE_NOW LEDGER_PATH HUBKIT_NOW_ISO WORK_LOOP_HOLD_S WORK_LOOP_PRESEND_SLEEP_S REPORT_GATE_PROJECTS_ROOT CLAUDE_PID CLAUDE_CODE_SESSION_ID
export PYTHONDONTWRITEBYTECODE=1
# snapshot of the real kit folder, compared at the end (names and sizes only)
snap() { ls -lR "$REAL_HOME/.claude/hubkit" 2>/dev/null | cksum; }
SNAP_BEFORE="$(snap)"
HK="$HOME/.claude/hubkit"
WL="$HK/work-loop/work_loop.py"
LEDGER="$HK/ledger/ledger"
STATE="$HK/state"
fails=0

ok() { echo "e2e: PASS $1"; }
bad() { echo "e2e: FAIL $1"; fails=$((fails + 1)); }
check() { # check "label" command...   (passes when the command exits 0)
  local label="$1"; shift
  if "$@" >"$T/out.txt" 2>&1; then ok "$label"; else bad "$label"; sed 's/^/    /' "$T/out.txt" | head -8; fi
}
has() { grep -q -- "$1" "$2"; }

# ---- step 1: install everything, doctor shows no FAIL
bash "$ROOT/install.sh" --all >"$T/install.txt" 2>&1 && ok "1 install --all" || { bad "1 install --all"; sed 's/^/    /' "$T/install.txt" | head -8; }
cp "$ROOT/kit/settings.snippet.json" "$HOME/.claude/settings.json"    # "merge the snippet by hand" on a fresh machine
python3 "$HK/doctor/doctor.py" >"$T/doctor1.txt" 2>&1
if has "^FAIL" "$T/doctor1.txt" || ! has "^summary: .* 0 fail" "$T/doctor1.txt"; then bad "1 doctor has no FAIL"; grep "^FAIL" "$T/doctor1.txt" | head -5; else ok "1 doctor has no FAIL"; fi

# ---- step 2: three tickets, one lane, an idle lane is fed, close it with proof, the next is fed
for title in "write the hello file" "write the second file" "write the third file"; do
  python3 "$LEDGER" add "$title" >/dev/null 2>&1
done
python3 "$WL" lane add lane-a >/dev/null 2>&1
BASE="$(date +%s)"
WORK_LOOP_NOW="$BASE" python3 "$WL" touch --lane lane-a >/dev/null 2>&1
WORK_LOOP_NOW=$((BASE + 700)) python3 "$WL" tick >/dev/null 2>&1
check "2 first ticket arrives in the lane inbox" has "L-0001" "$STATE/inbox/lane-a.txt"
mkdir -p "$HK/reports"
echo "hello file written" >"$HK/reports/L-0001.md"
python3 "$LEDGER" claim L-0001 --owner lane-a >/dev/null 2>&1
python3 "$LEDGER" proof L-0001 file "$HK/reports/L-0001.md" >/dev/null 2>&1
python3 "$LEDGER" status L-0001 done >/dev/null 2>&1
WORK_LOOP_NOW=$((BASE + 700 + 2400)) python3 "$WL" tick >/dev/null 2>&1
check "2 after closing, the next ticket is fed" has "L-0002" "$STATE/inbox/lane-a.txt"

# ---- step 3: close the rest, then the loop says idle and the heartbeat still beats
for n in 0002 0003; do
  echo "done" >"$HK/reports/L-$n.md"
  python3 "$LEDGER" claim "L-$n" --owner lane-a >/dev/null 2>&1
  python3 "$LEDGER" proof "L-$n" file "$HK/reports/L-$n.md" >/dev/null 2>&1
  python3 "$LEDGER" status "L-$n" done >/dev/null 2>&1
done
python3 "$HK/improve/improve.py" mine >"$T/mine.txt" 2>&1
check "3 empty queue prints the idle line" has "IDLE: nothing measured is failing" "$T/mine.txt"
python3 "$WL" tick >/dev/null 2>&1
python3 "$HK/doctor/doctor.py" >"$T/doctor3.txt" 2>&1
check "3 heartbeat still beats (doctor PASS loop-tick)" has "PASS  *loop-tick" "$T/doctor3.txt"

# ---- step 4: three identical failures -> one candidate with evidence; a note does not close it; a measurement does
NOWISO() { python3 -c 'import sys,datetime; print(datetime.datetime.fromtimestamp(int(sys.argv[1]), datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))' "$1"; }
T0="$(date +%s)"
: >"$STATE/failures.jsonl"
for back in 10800 7200 3600; do
  printf '{"ts": "%s", "job": "nightly", "step": "build", "reason": "timed out", "exit": 124}\n' "$(NOWISO $((T0 - back)))" >>"$STATE/failures.jsonl"
done
python3 "$HK/failure-register/failure_register.py" >/dev/null 2>&1
python3 "$HK/improve/improve.py" mine >"$T/mine2.txt" 2>&1
CAND="$(awk '/^PROPOSE im-/ {print $2; exit}' "$T/mine2.txt")"
if [ -n "$CAND" ]; then ok "4 one candidate proposed"; else bad "4 one candidate proposed"; sed 's/^/    /' "$T/mine2.txt" | head -5; CAND="im-none"; fi
if [ "$(grep -c '^PROPOSE im-' "$T/mine2.txt")" = "1" ]; then ok "4 exactly one candidate"; else bad "4 exactly one candidate"; fi
python3 "$HK/improve/improve.py" close "$CAND" --note "looked into it" >"$T/close1.txt" 2>&1
rc=$?
if [ "$rc" -ne 0 ] && has "REFUSE evidence-not-refreshed" "$T/close1.txt"; then ok "4 a note alone does not close it"; else bad "4 a note alone does not close it"; fi
sleep 2
printf '{"ts": "%s", "job": "nightly", "step": "build", "ok": true}\n' "$(NOWISO "$(date +%s)")" >>"$STATE/failures.jsonl"
python3 "$HK/failure-register/failure_register.py" >/dev/null 2>&1
python3 "$HK/improve/improve.py" close "$CAND" >"$T/close2.txt" 2>&1
check "4 a before/after measurement closes it" has "^CLOSED $CAND before 3 after 0" "$T/close2.txt"

# ---- step 5: Stop arm: a registered lane gets one continuation per stop, up to the cap, then it may stop
for n in 1 2 3 4 5; do python3 "$LEDGER" add "stop arm ticket $n" >/dev/null 2>&1; done
python3 "$WL" lane add lane-b >/dev/null 2>&1
python3 -c 'import json,sys,os; p=os.path.join(os.environ["HOME"],".claude","settings.json"); s=json.load(open(p)); l=json.load(open(sys.argv[1])); s.setdefault("hooks",{}).setdefault("Stop",[]).extend(l["hooks"]["Stop"]); json.dump(s,open(p,"w"))' "$ROOT/kit/settings.snippet.loop.json"
python3 "$HK/doctor/doctor.py" >"$T/doctor5.txt" 2>&1
if has "^FAIL" "$T/doctor5.txt" || ! has "^summary: .* 0 fail" "$T/doctor5.txt"; then bad "5 doctor with the Stop arm wired has no FAIL"; grep "^FAIL" "$T/doctor5.txt" | head -5; else ok "5 doctor with the Stop arm wired has no FAIL"; fi
export HUBKIT_LANE=lane-b
SA="$HK/work-loop/stop_arm.py"
for i in 1 2 3; do
  echo '{}' | python3 "$SA" >"$T/stop$i.txt" 2>&1
  if has '"decision": "block"' "$T/stop$i.txt"; then ok "5 stop $i continues"; else bad "5 stop $i continues"; sed 's/^/    /' "$T/stop$i.txt" | head -3; fi
done
echo '{}' | python3 "$SA" >"$T/stop4.txt" 2>&1
if [ ! -s "$T/stop4.txt" ]; then ok "5 stop 4 is capped (chat may stop)"; else bad "5 stop 4 is capped"; fi
unset HUBKIT_LANE
echo '{}' | python3 "$SA" >"$T/stop5.txt" 2>&1
if [ ! -s "$T/stop5.txt" ]; then ok "5 unregistered chat is never continued"; else bad "5 unregistered chat is never continued"; fi

# ---- nothing here read or wrote the real home's kit
if [ "$(snap)" = "$SNAP_BEFORE" ]; then ok "real home untouched"; else bad "real home untouched"; fi
if [ "$fails" -eq 0 ]; then echo "e2e: ALL PASS"; exit 0; fi
echo "e2e: $fails FAILED"
exit 1
