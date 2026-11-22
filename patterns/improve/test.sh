#!/usr/bin/env bash
# Runs the improve tests in a fresh scratch HOME, alone, with nothing else installed.
#   bash test.sh            run the tests
#   bash test.sh --mutate   also break each guard in a temp copy and check the tests notice
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT
export HOME="$SCRATCH/home"; mkdir -p "$HOME"
unset HUBKIT_HOME LEDGER_PATH IMPROVE IMPROVE_NOW
export PYTHONDONTWRITEBYTECODE=1

command -v python3 >/dev/null || { echo "FAIL: python3 not found"; exit 1; }
python3 -W ignore "$HERE/test_improve.py" || { echo "FAIL: improve tests"; exit 1; }
python3 -W ignore "$HERE/test_bug_sweep.py" || { echo "FAIL: bug-sweep tests"; exit 1; }
echo "improve: tests PASS"

[ "${1:-}" = "--mutate" ] || exit 0

# Each mutation edits ONE rule in a temp copy of the tool; the suite must then FAIL.
rc=0
SUITE=test_improve.py   # the suite that must notice the mutation
FILE=improve.py         # the file the mutation edits
mutate() {  # name, old text, new text
  local name="$1" dir="$SCRATCH/mut-$1"
  mkdir -p "$dir/improve" "$dir/ledger"
  cp "$HERE/improve.py" "$dir/improve/improve.py"
  cp "$HERE/bug_sweep.py" "$dir/improve/bug_sweep.py"
  cp "$HERE/../ledger/ledger_lib.py" "$dir/ledger/ledger_lib.py"
  python3 - "$dir/improve/$FILE" "$2" "$3" <<'PY' || { echo "mutation $name: could not apply (rule text changed?)"; rc=1; return; }
import sys
p, old, new = sys.argv[1:4]
s = open(p).read()
if old not in s:
    sys.exit(1)
open(p, "w").write(s.replace(old, new, 1))
PY
  if IMPROVE="$dir/improve/improve.py" BUG_SWEEP="$dir/improve/bug_sweep.py" python3 -W ignore "$HERE/$SUITE" >/dev/null 2>&1; then
    echo "mutation $name: SURVIVED (tests did not notice)"; rc=1
  else
    echo "mutation $name: killed"
  fi
}
mutate stale-evidence-allowed 'now - mtime > EVIDENCE_MAX_AGE_S' 'False'
mutate future-evidence-allowed 'mtime > now + FUTURE_SLOP_S or' 'False or'
mutate outside-home-allowed '    if not inside_home(real):
        raise Refuse("evidence-outside-home")' '    if False:
        raise Refuse("evidence-outside-home")'
mutate symlink-not-followed 'real = os.path.realpath(os.path.join(home(), rel))' 'real = os.path.abspath(os.path.join(home(), rel))'
mutate threshold-two 'MIN_COUNT = 3' 'MIN_COUNT = 2'
mutate threshold-four 'MIN_COUNT = 3' 'MIN_COUNT = 4'
mutate below-threshold-allowed '        if row["count"] < r["threshold"]:' '        if False:'
mutate caused-allowed '        if not (row["status"] in ("open", "reopened") or row["status"].startswith("passed-since")):' '        if False:'
mutate missing-signature-allowed '    if r["source"] == "failure" and r["row"] is None:
        return "REFUSE", "no-evidence", {}' '    if False:
        return "REFUSE", "no-evidence", {}'
mutate duplicate-allowed '        if prior["closed"] is None:
            return "REFUSE", "duplicate", {}' '        if False:
            return "REFUSE", "duplicate", {}'
mutate reopen-never 'if not recur:' 'if True:'
mutate reopen-without-recurrence 'if not recur:' 'if False:'
mutate daily-cap-four 'DAILY_CAP = 3' 'DAILY_CAP = 4'
mutate daily-cap-counts-all-days 'and day_of(e.get("t", 0)) == today) >= DAILY_CAP' 'and True) >= DAILY_CAP'
mutate skip-improve-labelled-tickets 'if i not in own and rec["status"] in ("open", "active"))' 'if i not in own and rec["status"] in ("open", "active") and rec.get("project") != PROJECT)'
mutate open-cap-six 'OPEN_CAP = 5' 'OPEN_CAP = 6'
mutate open-cap-counts-closed 'if c["closed"] is None) >= OPEN_CAP' 'if True) >= OPEN_CAP'
mutate flapping-allowed 'FLAP_CHANGES = 3 ' 'FLAP_CHANGES = 99 '
mutate injection-scan-off '    if injected(sig_text(r)):
        return "REFUSE", "evidence-injection", {}' '    if False:
        return "REFUSE", "evidence-injection", {}'
mutate injection-patterns-off 'return bool(INJECTION.search(t)) or (not free_text and not SAFE_TEXT.match(t))' 'return (not free_text and not SAFE_TEXT.match(t))'
mutate injection-charset-off 'return bool(INJECTION.search(t)) or (not free_text and not SAFE_TEXT.match(t))' 'return bool(INJECTION.search(t))'
mutate title-copies-log-text 'return "Improve: repeat failure %s (%dx)" % (r["key"], r["number"])' 'return ("Improve: %s (%dx)" % (r["row"]["sig"], r["number"]))[:60]'
mutate close-takes-a-note 'fixed, after, _ = measured_after(c, now)' 'fixed, after = True, {"number": 0}'
mutate close-without-fresh-reading 'need_after=c["proposed"]' 'need_after=None'
mutate close-ignores-newer-failure 'if last is not None and before.get("last") is not None and last > before["last"]:' 'if False:'
mutate close-ignores-missing-pass 'if row["pass_at"] is None or (' 'if False and ('
mutate close-ignores-below-listing '    if row["where"] == "below":
        raise Refuse("still-failing")' '    if False:
        raise Refuse("still-failing")'
mutate close-without-before-reading 'if not isinstance(before, dict) or "number" not in before:' 'if False:'
mutate target-direction-flipped 'ok = r["number"] > r["threshold"] if r["op"] == "<=" else r["number"] < r["threshold"]' 'ok = r["number"] < r["threshold"] if r["op"] == "<=" else r["number"] > r["threshold"]'
mutate operator-check-off 'rec["asked_by"] != "operator" or ' 'False or '
mutate queue-check-off 'if i not in own and rec["status"] in ("open", "active"))' 'if i not in own and rec["status"] in ("dropped",))'
mutate by-design-cap-six 'BY_DESIGN_CAP = 5' 'BY_DESIGN_CAP = 6'
mutate by-design-short-reason 'len(why) < 10 or' 'False or'
mutate by-design-instruction-reason 'or injected(why, free_text=True)' 'or False'
mutate by-design-for-targets 'if c["source"] != "failure" or' 'if'
mutate candidate-id-not-stable '("%s\0%s" % (source, key))' '("%s\0%s%s" % (source, key, time.time()))'
mutate key-not-validated '    if not KEY_RE.match(str(key)):' '    if False:'
mutate dry-run-records-readings '    if not dry:
        append_jsonl("improve-readings.jsonl"' '    if True:
        append_jsonl("improve-readings.jsonl"'
mutate no-heartbeat 'with open(path_in_state("improve.tick"), "w") as fh:' 'with open(os.devnull, "w") as fh:'
mutate dry-run-beats-heartbeat '        if not args.dry_run:
            with open(path_in_state("improve.tick")' '        if True:
            with open(path_in_state("improve.tick")'
mutate corrupt-ledger-ignored '    except ValueError:
        raise Refuse("ledger-unreadable")' '    except ValueError:
        return {}'
mutate stale-register-reads-as-idle '        elif why in ("stale-evidence", "evidence-outside-home"):' '        elif False:'
mutate mine-reads-outside-the-lock '    ctx = locked() if not args.dry_run else contextlib.nullcontext()
    with ctx:
        # Everything the decision rests on is read INSIDE the lock, so an overlapping miner sees our tickets.
        try:
            records = ledger_records()
        except Refuse as ex:
            print("improve: %s" % ex.reason)
            return 2
        cands = candidates(records)' '    records = ledger_records()
    cands = candidates(records)
    ctx = locked() if not args.dry_run else contextlib.nullcontext()
    with ctx:
        try:
            pass
        except Refuse as ex:
            return 2'
mutate missing-heading-crashes '    except Refuse:
        return [], "no-heading"' '    except ZeroDivisionError:
        return [], "no-heading"'
mutate no-cooldown-after-drop 'if prior["dropped"] and source in ("target", "bugsweep"):' 'if False:'
# bug-sweep: the guards in bug_sweep.py and the bugsweep source in improve.py, noticed by test_bug_sweep.py
SUITE=test_bug_sweep.py
FILE=bug_sweep.py
mutate sweep-queue-check-off '            blockers = improve.queue_blockers(records, cands)' '            blockers = []'
mutate sweep-unlisted-allowed '            if args.project is not None and args.project not in names:' '            if False:'
mutate sweep-name-not-validated '    if args.project is not None and not NAME_RE.match(args.project):' '    if False:'
mutate sweep-entry-name-not-checked '    if improve.injected(it["name"]):
        return "bad-name"' '    if False:
        return "bad-name"'
mutate sweep-covered-allowed '        if result_is_fresh(project):
            raise Refuse("range-covered")' '        if False:
            raise Refuse("range-covered")'
mutate sweep-stale-evidence-not-refreshed '    return age <= improve.EVIDENCE_MAX_AGE_S' '    return True'
mutate sweep-entry-timeout-nonfinite-allowed 'or not math.isfinite(t) or t <= 0:' 'or t <= 0:'
mutate sweep-rewritten-history-ignored '        if git(path, "cat-file", "-e", sha + "^{commit}")[0] == 0:
            known.append(sha)' '        known.append(sha)
        break'
mutate sweep-failure-counts-as-covered 'e.get("result") == "pass" and' 'True and'
mutate sweep-lock-off '            raise Refuse("sweep-running")' '            pass'
mutate sweep-detached-head-allowed '    if rc != 0:
        raise Refuse("detached-head")' '    if False:
        raise Refuse("detached-head")'
mutate sweep-shell-used 'cwd=cwd, shell=False,' 'cwd=cwd, shell=True,'
mutate sweep-string-command-allowed 'if not isinstance(smoke, list) or not smoke or not all(isinstance(a, str) and a and "\0" not in a for a in smoke):' 'if not smoke:'
mutate sweep-no-new-session 'start_new_session=True' 'start_new_session=False'
mutate sweep-kills-parent-only 'os.killpg(pgid, sig)' 'os.kill(pgid, sig)'
mutate sweep-result-dir-unchecked '    if not improve.inside_home(os.path.realpath(d)):
        raise Refuse("evidence-outside-home")' '    if False:
        raise Refuse("evidence-outside-home")'
mutate sweep-corrupt-config-ignored '        raise Refuse("projects-unreadable")' '        return []'
mutate sweep-output-not-capped 'fh.seek(max(0, size - OUT_KEEP))' 'fh.seek(0)'
mutate sweep-token-kept 'return TOKENISH.sub("[redacted]", text)' 'return text'
mutate sweep-dry-run-runs 'if args.dry_run:
                    print("WOULD RUN' 'if False:
                    print("WOULD RUN'
mutate sweep-no-heartbeat 'with open(improve.path_in_state("bug-sweep.tick"), "w") as fh:' 'with open(os.devnull, "w") as fh:'
mutate sweep-failure-not-written '                write_result(name, head, n, code, text)' '                pass'
FILE=improve.py
mutate projects-symlink-followed '    real = os.path.realpath(os.path.join(home(), rel))' '    real = os.path.abspath(os.path.join(home(), rel))'
mutate bugsweep-source-not-mined '        keys += [("bugsweep", n) for n in bugsweep_keys()]' '        pass'
mutate bugsweep-title-copies-output '    if r["source"] == "bugsweep":
        return "Improve: smoke test fails in %s" % r["key"]' '    if r["source"] == "bugsweep":
        return "Improve: smoke test fails in %s %s" % (r["key"], r.get("output", ""))'
mutate bugsweep-output-not-carried '    if r.get("output"):' '    if False:'
mutate bugsweep-close-skips-reading '    if c["source"] != "failure":
        fixed = not fires(r)[0]' '    if c["source"] == "target":
        fixed = not fires(r)[0]'
mutate bugsweep-number-flipped 'return {"source": "bugsweep", "key": name, "path": rel, "mtime": mtime, "row": None, "threshold": 0.0, "op": "<=",' 'return {"source": "bugsweep", "key": name, "path": rel, "mtime": mtime, "row": None, "threshold": 1.0, "op": "<=",'
mutate pending-candidate-reads-idle '        if not proposed and pending:' '        if False:'
mutate idle-event-off '                if not refused:
                    emit("loop.idle_declared", now)' '                pass'
mutate idle-with-refused '                if not refused:
                    emit("loop.idle_declared", now)' '                emit("loop.idle_declared", now)'
[ $rc -eq 0 ] && echo "improve: mutation check PASS" || echo "improve: mutation check FAIL"
exit $rc
