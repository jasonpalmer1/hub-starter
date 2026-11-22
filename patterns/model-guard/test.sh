#!/usr/bin/env bash
# Runs the model-guard tests in a fresh scratch HOME, alone, with nothing else installed.
#   bash test.sh            run the tests
#   bash test.sh --mutate   also break each guard in a temp copy and check the tests notice
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT
export HOME="$SCRATCH/home"; mkdir -p "$HOME"
unset HUBKIT_HOME MODEL_GUARD_LOG MODEL_GUARD_ALLOWED GATE_SPAWNS_LOG HOOK
export PYTHONDONTWRITEBYTECODE=1

command -v python3 >/dev/null || { echo "FAIL: python3 not found"; exit 1; }
python3 "$HERE/test_model_guard.py" || { echo "FAIL: model-guard tests"; exit 1; }
python3 "$HERE/test_selfcheck_admit.py" || { echo "FAIL: selfcheck_admit tests"; exit 1; }
echo "model-guard: tests PASS"

[ "${1:-}" = "--mutate" ] || exit 0

# Each mutation edits ONE guard in a copy of the WHOLE patterns tree (so the hook sees its real siblings: ticket-lock,
# report-gate), and the suite run from that copy must then FAIL. A lone hook copy would fail the ticket-lock tests for
# any copy at all and make every mutant look killed. A no-op control mutant runs the same way and MUST survive:
# if it is "killed", the harness itself is broken and the run fails loudly.
PATTERNS="$(cd "$HERE/.." && pwd)"
rc=0
killed=0; survived=0; control_ok=0
# Mutants the suite is allowed to miss, each with the reason. Both are message-only: the spawn is still refused with
# exit 2 either way, only the wording of the refusal differs, so no behaviour test can tell them apart.
#   tool-input-type-ignored: a non-object tool_input is replaced by {} and refused anyway as "no model"; only the text differs.
#   report-empty-hint-lost:  an empty `Report:` value is still refused as "no report path anywhere"; only the text differs.
ACCEPTED_SURVIVORS="tool-input-type-ignored report-empty-hint-lost"
accepted_survivor() { case " $ACCEPTED_SURVIVORS " in *" $1 "*) return 0;; esac; return 1; }
mutate() {  # name, old text, new text
  local name="$1" old="$2" new="$3" dir="$SCRATCH/mut-$1"
  mkdir -p "$dir"
  cp -R "$PATTERNS" "$dir/patterns" || { echo "mutation $name: could not copy the patterns tree"; rc=1; return; }
  python3 - "$dir/patterns/model-guard/model-guard.py" "$old" "$new" <<'PY' || { echo "mutation $name: could not apply (guard text changed?)"; rc=1; return; }
import sys
dst, old, new = sys.argv[1:4]
s = open(dst).read()
if old not in s:
    sys.exit(1)
open(dst, "w").write(s.replace(old, new, 1))
PY
  if (unset HOOK; python3 "$dir/patterns/model-guard/test_model_guard.py" >/dev/null 2>&1); then
    if [ "$name" = "no-op-control" ]; then
      echo "mutation $name: survived (expected: the control changes nothing)"; control_ok=1
    elif accepted_survivor "$name"; then
      echo "mutation $name: SURVIVED (accepted)"; survived=$((survived+1))
    else
      echo "mutation $name: SURVIVED (tests did not notice)"; rc=1; survived=$((survived+1))
    fi
  else
    if [ "$name" = "no-op-control" ]; then
      echo "mutation $name: KILLED: the harness is broken (a no-op change must not fail the suite)"; rc=1
    else
      echo "mutation $name: killed"; killed=$((killed+1))
    fi
  fi
  rm -rf "$dir"
}
mutate no-op-control        'SPAWN_TOOLS = ("Agent", "Task")' 'SPAWN_TOOLS = ("Agent", "Task")  # control'
mutate ticket-lock-unwired     'lock_msg = ticket_lock_message(tin' 'lock_msg = None and ticket_lock_message(tin'
mutate unset-allowed        'if not model or model == "inherit":' 'if model == "inherit":'
mutate inherit-allowed      'if not model or model == "inherit":' 'if not model:'
mutate blank-not-trimmed    'model = str(tin.get("model") or "").strip().lower()' 'model = str(tin.get("model") or "").lower()'
mutate task-not-guarded     'if tool not in ("Agent", "Task"):' 'if tool not in ("Agent",):'
mutate allow-list-off       'if allowed and model not in allowed:' 'if False:'
mutate allow-list-case      'model = str(tin.get("model") or "").strip().lower()' 'model = str(tin.get("model") or "").strip()'
mutate empty-list-blocks    'if allowed and model not in allowed:' 'if model not in allowed:'
mutate other-tool-crash-blocks 'if not spawn:
            log(f"FAIL-OPEN' 'if False:
            log(f"FAIL-OPEN'
mutate spawn-crash-allowed  'if not spawn:
            log(f"FAIL-OPEN' 'if True:
            log(f"FAIL-OPEN'
mutate unreadable-allowed   '_fail_closed("unreadable payload: %s" % type(e).__name__)' 'return 0'
mutate non-object-allowed   'if not isinstance(data, dict):' 'if False:'
mutate tool-name-type-ignored 'if not isinstance(tool, str):' 'if False:'
mutate tool-input-type-ignored '        if spawn:
            _fail_closed("tool_input is not an object")' '        if False:
            _fail_closed("tool_input is not an object")'
mutate task-not-a-spawn     'SPAWN_TOOLS = ("Agent", "Task")' 'SPAWN_TOOLS = ("Agent",)'
mutate every-tool-is-a-spawn 'spawn = tool in SPAWN_TOOLS' 'spawn = True'
mutate block-exit-not-forced 'os._exit(2)' 'sys.exit(2)'
mutate report-all-types     'rtype in REPORT_TYPES' 'rtype'
mutate report-builder-only  'REPORT_TYPES = frozenset(("builder", "bug-gate", "playtester"))' 'REPORT_TYPES = frozenset(("builder",))'
mutate report-switch-ignored 'report_check_on() and' 'True and'
mutate report-none-refused  'if rg._is_exempt(prompt) or matches:' 'if matches:'
mutate report-dotdot-allowed 'if any(_DOTDOT_RE.search(t) for t in v.split() if "/" in t):' 'if False:'
mutate report-control-allowed 'if any((ord(c) < 32 and c != "\t") or ord(c) == 127 for c in v):' 'if False:'
mutate report-empty-hint-lost 'if any(not raw.strip(" \t`'"'"'\"\r") for raw in _REPORT_TAG_RE.findall(prompt)):' 'if False:'
mutate report-fenced-handoff-exempts 'and not _HANDOFF_RE.search(_FENCE_RE.sub("", rprompt))' 'and not _HANDOFF_RE.search(rprompt)'
mutate report-handoff-ignored 'and not _HANDOFF_RE.search(_FENCE_RE.sub("", rprompt))' ''
mutate report-gate-missing-blocks 'if rg is None:
        return None' 'if rg is None:
        return "no report-gate"'
mutate report-hubkit-home-ignored 'os.path.join(home, "report-gate")' 'os.path.join(home, "nope")'
mutate report-error-blocks  'log(f"REPORT fail-open {type(e).__name__}")' 'raise SystemExit(2)'
mutate no-reason-printed    '        print(msg, file=sys.stderr)' '        pass'
mutate no-log               '    if line:
        log(line)' '    pass'
mutate index-failure-blocks '        log(f"GATE-SPAWN index write failed (ignored): {type(e).__name__}")' '        sys.exit(2)'
mutate index-follows-symlink ' | os.O_NOFOLLOW' ''
mutate index-not-append 'os.O_WRONLY | os.O_APPEND | os.O_CREAT' 'os.O_WRONLY | os.O_CREAT'
mutate index-all-types 'if stype not in ("builder", "bug-gate"):' 'if False:'
mutate index-before-refusal '    allowed = allowed_models()
' '    allowed = allowed_models(); record_gate_spawn(tin, model, None)
'
mutate prompt-ticket-ignored 'or TICKET_RE.findall(prompt[:400])[:1]' 'or []'
mutate prompt-ticket-anywhere 'TICKET_RE.findall(prompt[:400])[:1]' 'TICKET_RE.findall(prompt)[:1]'
mutate second-ticket-dropped 'sorted(set(TICKET_RE.findall(desc)))' 'sorted(set(TICKET_RE.findall(desc)))[:1]'
mutate prompt-beats-description 'sorted(set(TICKET_RE.findall(desc))) or TICKET_RE.findall(prompt[:400])[:1]' 'TICKET_RE.findall(prompt[:400])[:1] or sorted(set(TICKET_RE.findall(desc)))'
mutate index-path-ignores-env 'if os.environ.get("GATE_SPAWNS_LOG"):' 'if False:'
[ $control_ok -eq 1 ] || { echo "model-guard: no-op control did not run to a survive"; rc=1; }
echo "model-guard: $killed killed, $survived survived"
[ $rc -eq 0 ] && echo "model-guard: mutation check PASS" || echo "model-guard: mutation check FAIL"
exit $rc
