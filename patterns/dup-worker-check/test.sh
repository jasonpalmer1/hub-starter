#!/usr/bin/env bash
# Runs the dup-worker-check tests in a fresh scratch HOME, alone, with nothing else installed.
#   bash test.sh            run the tests
#   bash test.sh --mutate   also break each guard in a temp copy and check the tests notice
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT
export HOME="$SCRATCH/home"; mkdir -p "$HOME"
unset HUBKIT_HOME DUP_PROJECTS_ROOT DUP_LIVE_MIN DUP_TICKET_RE DUP_CHECK_BIN CHECK HOOK
export PYTHONDONTWRITEBYTECODE=1

command -v python3 >/dev/null || { echo "FAIL: python3 not found"; exit 1; }
python3 "$HERE/test_dup_worker.py" || { echo "FAIL: dup-worker-check tests"; exit 1; }
echo "dup-worker-check: tests PASS"

[ "${1:-}" = "--mutate" ] || exit 0

# Each mutation edits ONE guard in a temp copy; the suite must then FAIL.
# file is check (dup-worker-check.py) or hook (dup-worker-hook.py).
rc=0
mutate() {  # name, file(check|hook), old text, new text
  local name="$1" which="$2" old="$3" new="$4" dir="$SCRATCH/mut-$1" src
  mkdir -p "$dir"
  cp "$HERE/dup-worker-check.py" "$dir/dup-worker-check.py"
  cp "$HERE/dup-worker-hook.py" "$dir/dup-worker-hook.py"
  src="$dir/dup-worker-$which.py"
  python3 - "$src" "$old" "$new" <<'PY' || { echo "mutation $name: could not apply (guard text changed?)"; rc=1; return; }
import sys
p, old, new = sys.argv[1:4]
s = open(p).read()
if old not in s:
    sys.exit(1)
open(p, "w").write(s.replace(old, new, 1))
PY
  if CHECK="$dir/dup-worker-check.py" HOOK="$dir/dup-worker-hook.py" python3 "$HERE/test_dup_worker.py" >/dev/null 2>&1; then
    echo "mutation $name: SURVIVED (tests did not notice)"; rc=1
  else
    echo "mutation $name: killed"
  fi
}
mutate finished-still-holds  check 'if mt < cutoff or is_finished(path):' 'if mt < cutoff:'
mutate stale-still-holds     check 'if mt < cutoff or is_finished(path):' 'if is_finished(path):'
mutate head-limit-off        check 'return set(ticket_rx().findall(f"{description or '"''"'} {(prompt or '"''"')[:HEAD_CHARS]}"))' 'return set(ticket_rx().findall(f"{description or '"''"'} {(prompt or '"''"')}"))'
mutate description-ignored   check 'f"{description or '"''"'} {(prompt or '"''"')[:HEAD_CHARS]}"' 'f"{(prompt or '"''"')[:HEAD_CHARS]}"'
mutate no-overlap-needed     check 'for t in sorted(new & w["ids"]):' 'for t in sorted(new):'
mutate exit-code             check 'return 3 if lines else 0' 'return 1 if lines else 0'
mutate check-fails-closed    check '    except Exception:
        return 0
    for line in lines:' '    except Exception:
        return 3
    for line in lines:'
mutate live-window-fixed     check 'cutoff = now - live_minutes() * 60' 'cutoff = now - 15 * 60'
mutate ticket-shape-fixed    check 'return re.compile(os.environ.get("DUP_TICKET_RE") or DEFAULT_TICKET_RE)' 'return re.compile(DEFAULT_TICKET_RE)'
mutate bad-shape-crashes     check '    except re.error:
        return re.compile(DEFAULT_TICKET_RE)' '    except re.error:
        raise'
mutate user-turn-is-finished check 'return rec.get("type") == "assistant" and m.get("stop_reason") == "end_turn"' 'return m.get("stop_reason") == "end_turn" or rec.get("type") == "user"'
mutate dup-ok-ignored        hook 'if OVERRIDE_RX.search(outside_fences(prompt)):
            return 0' 'if False:
            return 0'
mutate dup-ok-substring      hook 'if OVERRIDE_RX.search(outside_fences(prompt)):' 'if "DUP-OK" in prompt:'
mutate dup-ok-no-reason      hook 'DUP-OK:[ \t]*(?!<)\S"' 'DUP-OK:"'
mutate dup-ok-indent         hook '^DUP-OK:' '^[ \t]*DUP-OK:'
mutate dup-ok-fence          hook "OVERRIDE_RX.search(outside_fences(prompt))" 'OVERRIDE_RX.search(prompt)'
mutate fence-length-ignored  hook 'and len(m.group(1)) >= len(fence)' 'and True'
mutate fence-char-ignored    hook "m.group(1)[0] == fence[0] and " ''
mutate fence-close-needs-bare hook '(`{3,}|~{3,})[ \t\r]*$")' '(`{3,}|~{3,})")'
mutate fence-open-needs-three hook '(`{3,}|~{3,})")
FENCE_CLOSE' '(`{2,}|~{2,})")
FENCE_CLOSE'
mutate dup-ok-placeholder    hook '[ \t]*(?!<)\S' '[ \t]*\S'
mutate hook-ignores-task     hook 'if data.get("tool_name") not in ("Agent", "Task"):' 'if data.get("tool_name") not in ("Agent",):'
mutate hook-never-blocks     hook '            return 2
    except Exception:' '            return 0
    except Exception:'
mutate hook-fails-closed     hook '    except Exception:
        return 0
    return 0' '    except Exception:
        return 2
    return 0'
mutate hook-no-timeout       hook 'capture_output=True, text=True, timeout=3)' 'capture_output=True, text=True, timeout=30)'
mutate hook-blocks-on-exit-3-only-with-output hook 'if r.returncode == 3 and r.stdout.strip():' 'if r.returncode == 3:'
[ $rc -eq 0 ] && echo "dup-worker-check: mutation check PASS" || echo "dup-worker-check: mutation check FAIL"
exit $rc
