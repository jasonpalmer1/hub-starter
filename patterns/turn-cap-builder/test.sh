#!/usr/bin/env bash
# Runs the turn-cap tests in a fresh scratch HOME, alone, with nothing else installed.
#   bash test.sh            run the tests
#   bash test.sh --mutate   also break each guard in a temp copy and check the tests notice
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT
export HOME="$SCRATCH/home"; mkdir -p "$HOME"
unset HUBKIT_HOME TURN_CAP_STATE_DIR TURN_CAP_REPORTS_DIR TURN_CAP_NUDGE TURN_CAP_BLOCK TURN_CAP_EXEMPT HOOK
export PYTHONDONTWRITEBYTECODE=1

command -v python3 >/dev/null || { echo "FAIL: python3 not found"; exit 1; }
python3 "$HERE/test_turn_cap.py" || { echo "FAIL: turn-cap tests"; exit 1; }
echo "turn-cap-builder: tests PASS"

[ "${1:-}" = "--mutate" ] || exit 0

# Each mutation edits ONE guard in a temp copy of the hook; the suite must then FAIL.
rc=0
mutate() {  # name, old text, new text
  local name="$1" old="$2" new="$3" dir="$SCRATCH/mut-$1"
  mkdir -p "$dir"
  cp "$HERE/builder.md" "$dir/builder.md"
  python3 - "$HERE/turn-cap.py" "$dir/turn-cap.py" "$old" "$new" <<'PY' || { echo "mutation $name: could not apply (guard text changed?)"; rc=1; return; }
import sys
src, dst, old, new = sys.argv[1:5]
s = open(src).read()
if old not in s:
    sys.exit(1)
open(dst, "w").write(s.replace(old, new, 1))
PY
  if HOOK="$dir/turn-cap.py" python3 "$HERE/test_turn_cap.py" >/dev/null 2>&1; then
    echo "mutation $name: SURVIVED (tests did not notice)"; rc=1
  else
    echo "mutation $name: killed"
  fi
}
mutate block-too-late       'CAP_BLOCK = _int_env("TURN_CAP_BLOCK", 115)' 'CAP_BLOCK = _int_env("TURN_CAP_BLOCK", 130)'
mutate nudge-too-late       'CAP_NUDGE = _int_env("TURN_CAP_NUDGE", 90)' 'CAP_NUDGE = _int_env("TURN_CAP_NUDGE", 100)'
mutate no-block             'if n >= CAP_BLOCK and not allowed_write' 'if False and not allowed_write'
mutate block-everything     'if n >= CAP_BLOCK and not allowed_write(ev.get("tool_name"), ev.get("tool_input")):' 'if n >= CAP_BLOCK:'
mutate any-write-allowed    'if tool not in ("Write", "Edit"):
        return False' 'if tool not in ("Write", "Edit", "Bash", "Read"):
        return False'
mutate no-realpath          'fp = os.path.realpath(os.path.expanduser(fp)) if fp else ""' 'fp = os.path.abspath(os.path.expanduser(fp)) if fp else ""'
mutate prefix-match-only    'return fp.startswith(os.path.realpath(reports_dir()) + os.sep)' 'return fp.startswith(os.path.realpath(reports_dir()))'
mutate exempt-off           'if not aid or ev.get("agent_type") in EXEMPT:' 'if not aid:'
mutate main-chat-counted    'if not aid or ev.get("agent_type") in EXEMPT:' 'aid = aid or "main"
    if ev.get("agent_type") in EXEMPT:'
mutate id-not-sanitised     'aid = "".join(c for c in str(aid) if c.isalnum() or c in "-_")[:80]' 'aid = str(aid)'
mutate nudge-every-time     'except FileExistsError:
                return 0' 'except FileExistsError:
                pass'
mutate fail-closed          '        rc = 0  # fail open' '        rc = 2'
mutate no-prune             'if n != ".pruned" and now - os.stat(p).st_mtime > 86400:' 'if False:'
mutate prune-every-call     'if now - os.stat(marker).st_mtime < 3600:' 'if False:'
mutate credit-lifts-block   'if n >= CAP_BLOCK and not allowed_write' 'if n - credits(path) >= CAP_BLOCK and not allowed_write'
mutate credit-no-ceiling    'if credits(path) < CREDIT_CEILING and is_poll(ev):' 'if is_poll(ev):'
mutate credit-any-bash      'return isinstance(cmd, str) and bool(_POLL_RE.match(cmd))' 'return isinstance(cmd, str)'
mutate credit-no-timeout    'if isinstance(t, bool) or not isinstance(t, (int, float)) or not 0 < t <= MAX_POLL_TIMEOUT_MS:' 'if False:'
mutate no-nudge-floor       'or n >= max(CAP_NUDGE, CAP_BLOCK - 10):' ':'
[ $rc -eq 0 ] && echo "turn-cap-builder: mutation check PASS" || echo "turn-cap-builder: mutation check FAIL"
exit $rc
