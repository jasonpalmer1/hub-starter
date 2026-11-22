#!/usr/bin/env bash
# Runs the doctor tests in a fresh scratch HOME, alone, with nothing else installed.
#   bash test.sh            run the tests
#   bash test.sh --mutate   also break each guard in a temp copy and check the tests notice
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT
export HOME="$SCRATCH/home"; mkdir -p "$HOME"
unset HUBKIT_HOME LEDGER_PATH DOCTOR_SETTINGS DOCTOR_NOW DOCTOR_FREE_GI_OVERRIDE DISK_GUARD_WARN_GI DISK_GUARD_CRIT_GI DOCTOR
export PYTHONDONTWRITEBYTECODE=1

command -v python3 >/dev/null || { echo "FAIL: python3 not found"; exit 1; }
python3 -W ignore "$HERE/test_doctor.py" || { echo "FAIL: doctor tests"; exit 1; }
echo "doctor: tests PASS"

[ "${1:-}" = "--mutate" ] || exit 0

# Each mutation edits ONE guard in a temp copy; the suite must then FAIL.
rc=0
mutate() {  # name, old text, new text
  local name="$1" old="$2" new="$3" dir="$SCRATCH/mut-$1"
  mkdir -p "$dir"
  python3 - "$HERE/doctor.py" "$dir/doctor.py" "$old" "$new" <<'PY' || { echo "mutation $name: could not apply (guard text changed?)"; rc=1; return; }
import sys
src, dst, old, new = sys.argv[1:5]
s = open(src).read()
if old not in s:
    sys.exit(1)
open(dst, "w").write(s.replace(old, new, 1))
PY
  if DOCTOR="$dir/doctor.py" python3 -W ignore "$HERE/test_doctor.py" >/dev/null 2>&1; then
    echo "mutation $name: SURVIVED (tests did not notice)"; rc=1
  else
    echo "mutation $name: killed"
  fi
}
mutate old-python-ok         'if v >= (3, 9):' 'if True:'
mutate home-missing-ok       'if not os.path.isdir(h):' 'if False:'
mutate home-readonly-ok      'elif not os.access(h, os.W_OK):' 'elif False:'
mutate hooks-off-ignored     'if settings.get("disableAllHooks") is True:' 'if False:'
mutate settings-missing-ok   '        r.add("FAIL", "hooks", err, fix)
        return' '        return'
mutate unwired-passes        'if right:' 'if right or True:'
mutate any-event-passes      'right = [c for c in cmds.get(event, []) if base in c]' 'right = [c for cs in cmds.values() for c in cs if base in c]'
mutate missing-file-ok       'if tok.endswith(".py") and ("/" in tok) and not os.path.isfile(expand(tok.strip("\"'"'"'"))):' 'if False:'
mutate agent-absent-ok       'if not os.path.isfile(user):' 'if False:'
mutate agent-uncapped-ok     'if "turn-cap.py" not in text:' 'if False:'
mutate project-agent-quiet   'if os.path.isfile(proj) and proj != user:' 'if False:'
mutate ledger-bad-ok         '    if bad:
        r.add("FAIL", "ledger"' '    if False:
        r.add("FAIL", "ledger"'
mutate ledger-nonobject-ok   'if not isinstance(json.loads(line), dict):' 'if False:'
mutate stale-job-ok          'elif now() - epoch > limit:' 'elif False:'
mutate job-boundary          'elif now() - epoch > limit:' 'elif now() - epoch >= limit:'
mutate corrupt-job-ok        'if epoch is None:
            r.add("FAIL", label, "its heartbeat file is corrupt"' 'if False:
            r.add("FAIL", label, "its heartbeat file is corrupt"'
mutate future-job-ok         'elif epoch > now():' 'elif False:'
mutate nolimit-is-pass       'elif limit is None:' 'elif False:'
mutate alarm-window          'if now() - ts < 86400:' 'if now() - ts < 0:'
mutate disk-crit-ok          'if free < crit:' 'if False:'
mutate disk-warn-ok          'elif free < warn:' 'elif False:'
mutate disk-env-ignored      'warn = int(os.environ.get("DISK_GUARD_WARN_GI") or 20)' 'warn = 20'
mutate paste-keeps-home      'text = re.sub(re.escape(h), "~", text, flags=re.IGNORECASE)' 'pass'
mutate paste-keeps-user      'text = re.sub(r"(?<![A-Za-z0-9])" + re.escape(u) + r"(?![A-Za-z0-9])", "<user>", text, flags=re.IGNORECASE)' 'pass'
mutate paste-user-case       're.escape(u) + r"(?![A-Za-z0-9])", "<user>", text, flags=re.IGNORECASE)' 're.escape(u) + r"(?![A-Za-z0-9])", "<user>", text)'
mutate paste-home-case       'text = re.sub(re.escape(h), "~", text, flags=re.IGNORECASE)' 'text = text.replace(h, "~")'
mutate log-creates-folder    'return 1 if failed else 0           # read-only' 'os.makedirs(os.path.join(home(), "state"), exist_ok=True)  # read-only'
mutate beat-utf8-crash       'except (OSError, ValueError):
        return None, None' 'except OSError:
        return None, None'
mutate beat-unicode-digits   'not all(re.fullmatch(r"[0-9]+", p) for p in parts)' 'not all(p.isdigit() for p in parts)'
mutate registry-ignored      'names |= reg' 'pass'
mutate registered-gone-quiet 'if epoch is None and n in reg and not os.path.exists(os.path.join(d, n)):' 'if False:'
mutate paste-keeps-paths     'return re.sub(r"(?<![\w~$.<>])/(?:[\w.@+-]+/)+([\w.@+-]*)", r"<path>/\1", text)' 'return text'
mutate exit-ignores-fail     'return 1 if failed else 0' 'return 0'
mutate no-log-still-logs     'if not nolog:' 'if True:'
mutate registry-limit-ignored 'limit = registered_limit(n)       # same order' 'pass       # same order'
mutate registry-limit-badbytes '    except (OSError, ValueError):
        return None
    return int(raw) if re.fullmatch' '    except OSError:
        return None
    return int(raw) if re.fullmatch'
mutate registry-limit-zero    'and int(raw) > 0 else None' 'and int(raw) >= 0 else None'
mutate loop-stale-ok          'elif now() - epoch > limit:
                r.add("FAIL", "loop-tick"' 'elif False:
                r.add("FAIL", "loop-tick"'
mutate loop-never-ran-quiet   'if epoch is None and not os.path.exists(os.path.join(home(), "state", "heartbeats", "work-loop-tick")):' 'if False:'
mutate loop-future-ok         'elif epoch > now():
                r.add("FAIL", "loop-tick"' 'elif False:
                r.add("FAIL", "loop-tick"'
mutate loop-tick-double       'names.discard("work-loop-tick")' 'pass'
mutate loop-no-lanes-judged   'elif not names:
            r.add("SKIP", "loop-tick"' 'elif False:
            r.add("SKIP", "loop-tick"'
mutate loop-bad-lanes-quiet   '        if err:
            r.add("FAIL", "loop-lanes"' '        if False:
            r.add("FAIL", "loop-lanes"'
mutate stop-missing-ok        'if missing:
            r.add("FAIL", "stop-arm"' 'if False:
            r.add("FAIL", "stop-arm"'
mutate stop-bare-name-ok      'if not any("/" in t and os.path.isfile(expand(t)) for t in toks):' 'if not any(True for t in toks):'
mutate stop-no-lane-quiet     'elif not names:
            r.add("WARN", "stop-arm"' 'elif False:
            r.add("WARN", "stop-arm"'
mutate stop-other-hook-judged '"stop_arm.py" in c]' 'True]'
mutate loop-feed-last-wrong   'if isinstance(rec, dict) and rec.get("event") == kind:' 'if isinstance(rec, dict):'
mutate register-stale-ok      'if at > now() or now() - at > REGISTER_TICK_MAX_S:' 'if at > now():'
mutate register-boundary      'now() - at > REGISTER_TICK_MAX_S' 'now() - at >= REGISTER_TICK_MAX_S'
mutate register-future-ok     'if at > now() or now() - at' 'if now() - at'
mutate register-never-quiet   'r.add("WARN", "failure-register", "failures are recorded' 'r.add("SKIP", "failure-register", "failures are recorded'
[ $rc -eq 0 ] && echo "doctor: mutation check PASS" || echo "doctor: mutation check FAIL"
exit $rc
