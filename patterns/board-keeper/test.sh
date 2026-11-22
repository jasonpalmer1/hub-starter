#!/usr/bin/env bash
# Runs the board-keeper tests in a fresh scratch HOME, alone.
#   bash test.sh            run the tests
#   bash test.sh --mutate   also break each guard in a temp copy and check the tests notice
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT
export HOME="$SCRATCH/home"; mkdir -p "$HOME"
unset HUBKIT_HOME BK_BOARD BK_ITEMS BK_STATE_DIR BK_LIVE_OWNERS BK_CAP BK_MOVE_CAP BK_MIN_ITEMS BK_NOW
export PYTHONDONTWRITEBYTECODE=1

command -v python3 >/dev/null || { echo "FAIL: python3 not found"; exit 1; }
python3 "$HERE/test_board_keeper.py" || { echo "FAIL: board-keeper tests"; exit 1; }
echo "board-keeper: tests PASS"

[ "${1:-}" = "--mutate" ] || exit 0

rc=0
mutate() {  # name, file, old text, new text
  local name="$1" file="$2" old="$3" new="$4" dir="$SCRATCH/mut-$1"
  mkdir -p "$dir"
  for f in board_keeper.py test_board_keeper.py; do cp "$HERE/$f" "$dir/$f"; done
  python3 - "$dir/$file" "$old" "$new" <<'PY' || { echo "mutation $name: could not apply (guard text changed?)"; rc=1; return; }
import sys
p, old, new = sys.argv[1:4]
s = open(p).read()
if old not in s:
    sys.exit(1)
open(p, "w").write(s.replace(old, new, 1))
PY
  if python3 "$dir/test_board_keeper.py" >/dev/null 2>&1; then
    echo "mutation $name: SURVIVED (tests did not notice)"; rc=1
  else
    echo "mutation $name: killed"
  fi
}
mutate open-item-ok board_keeper.py ' and all(s in CLOSED for s in known.values()):' ':'
mutate dropped-only-ok board_keeper.py 'and not open_t and done_t and' 'and not open_t and'
mutate unknown-moves board_keeper.py '            flags.append({"card": cid, "stage": stage, "kind": "unknown-item", "items": unknown})
            continue' '            flags.append({"card": cid, "stage": stage, "kind": "unknown-item", "items": unknown})'
mutate kill-ignored board_keeper.py 'return (STATE / KILL).exists()' 'return False'
mutate backward-allowed board_keeper.py 'elif STAGE_ORDER.index(target) > STAGE_ORDER.index(stage):' 'elif True:'
mutate cap-open board_keeper.py 'return 10 ** 9' 'return 0'
mutate hold-ignored board_keeper.py 'fixes = [f for f in fixes if f["card"] not in undone]' 'pass'
mutate move-hold-ignored board_keeper.py 'skip = {f["card"] for f in fixes} | held | undone' 'skip = {f["card"] for f in fixes}'
mutate shared-seat-builds board_keeper.py 'and not GENERIC_OWNER_RE.match(o)' ''
mutate bad-items-trusted board_keeper.py 'if not ok:' 'if False:'
mutate undo-no-hold board_keeper.py '"who": who, "kind": "undo"' '"who": "board-keeper", "kind": "undo"'
mutate id-check-off board_keeper.py 'or not ID_RE.match(cid) or not linked:' 'or not linked:'
mutate fix-attempt-log-ignored board_keeper.py 'if not log({"action": "fix", "card": f["card"]}) or not log_move(' 'log({"action": "fix", "card": f["card"]})
        if not log_move('
mutate move-attempt-log-ignored board_keeper.py 'if not log({"action": "move", "card": m["card"]}) or not log_move(' 'log({"action": "move", "card": m["card"]})
        if not log_move('
mutate fix-move-line-ignored board_keeper.py ' or not log_move({"card": f["card"], "from": f["from"], "to": "done"' ' or False and log_move({"card": f["card"], "from": f["from"], "to": "done"'
mutate forward-move-line-ignored board_keeper.py ' or not log_move({"card": m["card"], "from": m["from"], "to": m["to"]' ' or False and log_move({"card": m["card"], "from": m["from"], "to": m["to"]'
mutate run-start-log-ignored board_keeper.py 'if not log({"action": "run-start"}):' 'if False:'
mutate exit-code-zero-on-error board_keeper.py 'rc = 1 if res.get("error") else 0' 'rc = 0'
mutate lock-off board_keeper.py 'fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)' 'pass'
mutate fix-kill-not-rechecked board_keeper.py 'if kill_on():                                         # re-checked before EVERY action' 'if False:                                         # re-checked before EVERY action'
mutate move-kill-not-rechecked board_keeper.py '        if kill_on():
            flags.append(dict(m, kind="kill-switch"))' '        if False:
            flags.append(dict(m, kind="kill-switch"))'
mutate live-owners-stale-trusted board_keeper.py 'if now() - LIVE_OWNERS.stat().st_mtime > LIVE_OWNER_MAX_AGE:' 'if False:'
mutate live-owners-empty-trusted board_keeper.py 'return names or None' 'return names'
mutate stale-snapshot-overwrite board_keeper.py 'if c.get("stage") != expect_from:' 'if False:'
[ $rc -eq 0 ] && echo "board-keeper: mutation check PASS" || echo "board-keeper: mutation check FAIL"
exit $rc
