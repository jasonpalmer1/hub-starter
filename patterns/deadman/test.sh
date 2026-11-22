#!/usr/bin/env bash
# Runs the deadman tests in a fresh scratch HOME, alone, with nothing else installed.
#   bash test.sh            run the tests
#   bash test.sh --mutate   also break each guard in a temp copy and check the tests notice
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT
export HOME="$SCRATCH/home"; mkdir -p "$HOME"
unset HUBKIT_HOME HUBKIT_NOTIFY_CMD DEADMAN_NOW DEADMAN
export PYTHONDONTWRITEBYTECODE=1

command -v python3 >/dev/null || { echo "FAIL: python3 not found"; exit 1; }
python3 -W ignore "$HERE/test_deadman.py" || { echo "FAIL: deadman tests"; exit 1; }
echo "deadman: tests PASS"

[ "${1:-}" = "--mutate" ] || exit 0

# Each mutation edits ONE guard in a temp copy; the suite must then FAIL.
rc=0
mutate() {  # name, old text, new text
  local name="$1" old="$2" new="$3" dir="$SCRATCH/mut-$1"
  mkdir -p "$dir"
  python3 - "$HERE/deadman.py" "$dir/deadman.py" "$old" "$new" <<'PY' || { echo "mutation $name: could not apply (guard text changed?)"; rc=1; return; }
import sys
src, dst, old, new = sys.argv[1:5]
s = open(src).read()
if old not in s:
    sys.exit(1)
open(dst, "w").write(s.replace(old, new, 1))
PY
  if DEADMAN="$dir/deadman.py" python3 -W ignore "$HERE/test_deadman.py" >/dev/null 2>&1; then
    echo "mutation $name: SURVIVED (tests did not notice)"; rc=1
  else
    echo "mutation $name: killed"
  fi
}
mutate stale-never          'if age > limit:' 'if False:'
mutate boundary-is-stale    'if age > limit:' 'if age >= limit:'
mutate missing-is-ok        'return None, "no heartbeat recorded (never beat, or the file was deleted)"' 'return int(time.time()), None'
mutate corrupt-is-ok        'if not parts or len(parts) > 2 or not all(re.fullmatch(r"[0-9]+", p) for p in parts):' 'if False:'
mutate future-is-ok         'if epoch > t:' 'if False:'
mutate no-limit-is-ok       'if not limit:' 'if False:'
mutate always-realarm       'if last.isdigit() and 0 <= now() - int(last) < realarm:' 'if False:'
mutate never-realarm        'if last.isdigit() and 0 <= now() - int(last) < realarm:' 'if last.isdigit():'
mutate no-recovery-reset    '            clear_alarm(name)
        else:' '            pass
        else:'
mutate shared-alarm-window  'stamp = os.path.join(alarm_dir(), name + ".alarm")' 'stamp = os.path.join(alarm_dir(), "all.alarm")'
mutate no-notify            'if cmd:' 'if False:'
mutate notify-crash-hides   '        except (OSError, ValueError, subprocess.SubprocessError) as e:' '        except ValueError as e:'
mutate stale-exit-zero      '            bad = 1' '            bad = 0'
mutate all-skips-corrupt    'if epoch is None or isinstance(extra, int):' 'if isinstance(extra, int):'
mutate all-empty-is-ok      '                  alarm("deadman-watcher", reason, realarm, "deadman: the watcher has nothing to watch"))
            return 1' '                  alarm("deadman-watcher", reason, realarm, "deadman: the watcher has nothing to watch"))
            return 0'
mutate name-traversal       'NAME_RX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")' 'NAME_RX = re.compile(r"^.{1,64}$")'
mutate beat-not-atomic-append 'with open(tmp, "w") as f:' 'with open(final, "a") as f:'
mutate flag-ignored         'limit = max_age or extra' 'limit = extra or max_age'
mutate age-zero-ok          'if not m or int(m.group(1)) <= 0:' 'if not m:'
mutate all-empty-silent     '        if names:
            clear_alarm("deadman-watcher")
        else:' '        if True:
            clear_alarm("deadman-watcher")
        else:'
mutate all-ignores-registry 'names = set(reg)' 'names = set()'
mutate utf8-crash           'except ValueError:
        return None, "heartbeat file is corrupt (not text)"' 'except KeyError:
        return None, "heartbeat file is corrupt (not text)"'
mutate unicode-digits       'not all(re.fullmatch(r"[0-9]+", p) for p in parts)' 'not all(p.isdigit() for p in parts)'
mutate stamp-utf8-crash     '    except (OSError, ValueError):
        pass
    try:
        os.makedirs(alarm_dir()' '    except OSError:
        pass
    try:
        os.makedirs(alarm_dir()'
mutate list-ignores-registry 'names = sorted(set(found) | set(registered()))' 'names = sorted(set(found))'
mutate beat-never-registers 'if max_age:
        os.makedirs(registry_dir()' 'if False:
        os.makedirs(registry_dir()'
mutate registry-limit-ignored 'limit = max_age or extra or registered_limit(name)' 'limit = max_age or extra'
mutate registry-tmp-listed   'if NAME_RX.match(n) and ".tmp-" not in n)
    except OSError:
        return []' 'if NAME_RX.match(n))
    except OSError:
        return []'
mutate retire-keeps-registry 'os.path.join(registry_dir(), name),
                 os.path.join(alarm_dir()' 'os.path.join(registry_dir(), name + ".none"),
                 os.path.join(alarm_dir()'
mutate retire-keeps-stamp    'os.path.join(alarm_dir(), name + ".alarm")):
        try:
            os.remove(path)' 'os.path.join(alarm_dir(), name + ".none")):
        try:
            os.remove(path)'
mutate retire-unknown-ok     '        print("deadman: nothing to retire, no job named %s is known" % name, file=sys.stderr)
        return 1' '        print("deadman: nothing to retire, no job named %s is known" % name, file=sys.stderr)
        return 0'
mutate no-retire-hint        'msg += " (switched off on purpose? run: deadman.py retire %s)" % name' 'pass'
mutate watcher-never-clears  '        if names:
            clear_alarm("deadman-watcher")' '        if names:
            pass'
mutate watcher-title-lost    'alarm("deadman-watcher", reason, realarm, "deadman: the watcher has nothing to watch")' 'alarm("deadman-watcher", reason, realarm)'
mutate registry-badbytes-crash '    except (OSError, ValueError):
        return None
    return int(raw)' '    except OSError:
        return None
    return int(raw)'
[ $rc -eq 0 ] && echo "deadman: mutation check PASS" || echo "deadman: mutation check FAIL"
exit $rc
