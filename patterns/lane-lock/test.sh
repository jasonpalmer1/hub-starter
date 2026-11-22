#!/usr/bin/env bash
# Runs the lane-lock tests in a fresh scratch HOME, alone, with nothing else installed.
#   bash test.sh            run the tests
#   bash test.sh --mutate   also break each guard in a temp copy and check the tests notice
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT
export HOME="$SCRATCH/home"; mkdir -p "$HOME"
unset LANE_LOCK LANE_LOCK_ROOT LANE_LOCK_ALARMS_LOG LANE_LOCK_WAIT_S LANE_BREAKER_THRESHOLD LANE_LOCK_REPORTS HUBKIT_HOME
export PYTHONDONTWRITEBYTECODE=1

command -v python3 >/dev/null || { echo "FAIL: python3 not found"; exit 1; }
python3 -W ignore "$HERE/test_lane_lock.py" || { echo "FAIL: lane-lock tests"; exit 1; }
echo "lane-lock: tests PASS"

[ "${1:-}" = "--mutate" ] || exit 0

# Equivalent mutants: edits that cannot change any observable behaviour, so no test can kill them.
#   corrupt-owner-is-free: read_owner_failing_closed() returns ({"_corrupt": True}, True). Every caller
#   branches on the second value (was_corrupt) and never reads the dict when it is True, so swapping the
#   dict for {} changes nothing. The refusal itself is killed by corrupt-claim-allowed and
#   release-corrupt-ok, the "UNKNOWN" status text by the owner-corruption tests.
EQUIVALENT="corrupt-owner-is-free unfloored-ruling corrupt-alias-open corrupt-floor-dropped"

#   corrupt-floor-dropped and corrupt-alias-open are each other's second belt: a corrupt file is tripped both by the dict
#   read_breaker_failing_closed returns AND by the explicit `corrupt or` in _tripped, so dropping either one alone is invisible.
#   corrupt-alias-open: read_breaker_failing_closed already returns tripped=True for a corrupt file, so the explicit
#   `corrupt or` in _tripped is a second belt on the same fact; removing it changes nothing observable.
#   unfloored-ruling: a missing trip time (floor None) can only arise when breaker.json cannot be stat'ed, and then the
#   mtime comparison raises TypeError, which _judge_evidence turns into "no evidence" anyway: same refusal.
# Each mutation edits ONE guard in a temp copy; the suite must then FAIL. Four run at a time.
mutate() {  # name, old text, new text
  local name="$1" old="$2" new="$3" dir="$SCRATCH/mut-$1"
  mkdir -p "$dir"
  python3 - "$HERE/lane-lock.py" "$dir/lane-lock.py" "$old" "$new" <<'PY' || { echo "mutation $name: could not apply (guard text changed?)" > "$dir.out"; return; }
import sys
src, dst, old, new = sys.argv[1:5]
s = open(src).read()
if old not in s:
    sys.exit(1)
open(dst, "w").write(s.replace(old, new, 1))
PY
  if LANE_LOCK="$dir/lane-lock.py" python3 -W ignore "$HERE/test_lane_lock.py" >/dev/null 2>&1; then
    case " $EQUIVALENT " in
      *" $name "*) echo "mutation $name: equivalent (documented, see EQUIVALENT above)" > "$dir.out" ;;
      *) echo "mutation $name: SURVIVED (tests did not notice)" > "$dir.out" ;;
    esac
  else
    echo "mutation $name: killed" > "$dir.out"
  fi
}
n=0
run() { mutate "$@" & n=$((n+1)); [ $((n % 4)) -eq 0 ] && wait; }
run live-holder-ignored   'if op and op.get("owner") != args.owner and holder_live:' 'if False:'
run same-owner-refused    'if op and op.get("owner") != args.owner and holder_live:' 'if op and holder_live:'
run dead-never-heals      'if not pid_alive(pid):
        return False
    if not expected:' 'if not pid_alive(pid):
        return True
    if not expected:'
run pid-reuse-ignored     'return current == expected' 'return True'
run legacy-key-is-dead    '    if not _recognized(expected):' '    if False:'
run legacy-key-silent     'append_alarm(f"pid {pid}: recorded start_key is in an unrecognized' 'None and append_alarm(f"pid {pid}: recorded start_key is in an unrecognized'
run tz-not-pinned         'env={**os.environ, **_PS_ENV}' 'env=dict(os.environ)'
run pid-optional          '    if args.pid is None:' '    if False:'
run pid-zero-ok           '    if args.pid <= 0:' '    if False:'
run usage-exits-2         '        sys.exit(1)

''' '        sys.exit(2)

'
run lane-name-newline      'not LANE_RX.fullmatch(lane)' 'not LANE_RX.match(lane)'
run ps-fail-assume-dead   '    if current is None or not _recognized(current):
        return True' '    if current is None or not _recognized(current):
        return False'
run lane-name-unchecked   'if lane is not None and not LANE_RX.fullmatch(lane):' 'if False:'
run status-creates-lane   'd = lane_dir(args.lane, create=False)     # asking never creates a lane' 'd = lane_dir(args.lane)'
run peer-can-release      'if op.get("owner") != args.owner:
            print(f"REFUSED release' 'if False:
            print(f"REFUSED release'
run release-corrupt-ok    '        if owner_corrupt:
            print(f"REFUSED release' '        if False:
            print(f"REFUSED release'
run corrupt-owner-is-free 'return {"_corrupt": True}, True' 'return {}, True'
run corrupt-claim-allowed '        if owner_corrupt:
            print(f"REFUSED lane={args.lane}: owner.json is CORRUPT, so' '        if False:
            print(f"REFUSED lane={args.lane}: owner.json is CORRUPT, so'
run corrupt-breaker-open  'return {"fail_streak": max(2, threshold()), "tripped": True, "tripped_at": now_iso(), "history": []}, True' 'return _fresh_breaker(), True'
run corrupt-not-alarmed   '    print(f"WARNING: {msg}", file=sys.stderr)
    append_alarm(msg)
    return {"fail_streak"' '    print(f"WARNING: {msg}", file=sys.stderr)
    return {"fail_streak"'
run bad-utf8-crash        '    except (OSError, ValueError) as e:
        return None, type(e).__name__' '    except OSError as e:
        return None, type(e).__name__'
run non-object-ok         '    if not isinstance(data, dict):
        return None, "not a JSON object"' '    if False:
        return None, "not a JSON object"'
run force-release-reads-strict 'op, why = _read_record(owner_path(d))
        if op is None and why is None:' 'op, why = read_owner_failing_closed(d)
        if not op and why is None:'
run force-release-not-logged '    print(f"OK {line}")
    append_alarm(line)
    return 0


def cmd_status' '    print(f"OK {line}")
    return 0


def cmd_status'
run critsec-file-hangs    '                if self.lockdir.exists() and not self.lockdir.is_dir():' '                if False:'
run critsec-no-steal      '                    if stole:' '                    if True:'
run threshold-ignored     'if breaker["fail_streak"] >= threshold() and not breaker.get("tripped"):' 'if breaker["fail_streak"] >= 2 and not breaker.get("tripped"):'
run never-trips           'if breaker["fail_streak"] >= threshold() and not breaker.get("tripped"):' 'if False:'
run trip-not-alarmed      '        print(f"TRIPPED {msg}")
        append_alarm(msg)' '        print(f"TRIPPED {msg}")'
run breaker-ignored       '    if not breaker.get("tripped"):
        return False' '    if True:
        return False'
run pass-clears-trip      '                breaker["history"][-1]["while_tripped"] = True' '                breaker["history"][-1]["while_tripped"] = True
                breaker["tripped"] = False'
run pass-keeps-streak     '                breaker["fail_streak"] = 0
            _history(breaker, args.ticket, args.gate, "PASS"' '                pass
            _history(breaker, args.ticket, args.gate, "PASS"'
run reset-keeps-trip      'breaker["tripped"], breaker["tripped_at"], breaker["fail_streak"] = False, None, 0' 'breaker["fail_streak"] = 0'
run reset-not-logged      '        print(f"OK {line}")
        append_alarm(line)
    else:' '        print(f"OK {line}")
    else:'
run history-unbounded     'breaker["history"] = h[-20:]' 'breaker["history"] = h'
run lanes-share-breaker   '    d = root() / lane
    if create:' '    d = root() / "all"
    if create:'
run default-outside-home  'str(hubkit_home() / "state" / "lanes")' 'str(Path.home() / "lanes")'
run alarm-crash           '    except OSError:
        pass  # logging an alarm must never crash the caller' '    except KeyError:
        pass  # logging an alarm must never crash the caller'
run list-empty-wrong      'print("(no lanes yet)")' 'print("")'
run reset-lead-ok-when-tripped '    if fails_n >= 2:
        ok_ev, why' '    if False:
        ok_ev, why'
run reset-name-only       'if args.by not in ("judge", "hub") or not ok_ev:' 'if args.by not in ("judge", "hub") and not ok_ev:'
run reset-evidence-only   'if args.by not in ("judge", "hub") or not ok_ev:' 'if not ok_ev:'
run tripped-floor-dropped '    if was_tripped:
        fails_n = max(fails_n, 2)' '    if False:
        fails_n = max(fails_n, 2)'
run corrupt-floor-dropped 'return {"fail_streak": max(2, threshold()), "tripped": True, "tripped_at": now_iso(), "history": []}, True' 'return {"fail_streak": max(2, threshold()), "tripped": False, "tripped_at": now_iso(), "history": []}, True'
run corrupt-streak-zero   'return {"fail_streak": max(2, threshold()), "tripped": True' 'return {"fail_streak": 0, "tripped": True'
run pass-while-tripped-zeroes '            if not was_tripped:
                breaker["fail_streak"] = 0' '            if True:
                breaker["fail_streak"] = 0'
run pass-row-anchors      'if (res == "PASS" and h.get("while_tripped") is not True)' 'if (res == "PASS")'
run plain-reset-anchors   'or (h.get("gate") == "reset" and h.get("ruled") is True):' 'or (h.get("gate") == "reset"):'
run symlink-guard-dropped 'if os.path.dirname(base) != rroot or os.path.dirname(f) != base or not os.path.isfile(f):' 'if not os.path.isfile(f):'
run ruling-not-newer      'if os.stat(f).st_mtime < floor:' 'if False:'
run ruling-names-nothing  'if not _names_lane_or_ticket(txt, names, lane):' 'if False:'
run ruling-no-verdict     'if not _ruling_text_ok(txt):' 'if False:'
run fence-ignored         '        if fence:
            continue
        if _RULING_LABEL' '        if _RULING_LABEL'
run traversal-allowed     'if re.search(r"(^|[\\/\s])\.\.([\\/\s]|$)", reason) or "\x00" in reason:' 'if False:'
run unfloored-ruling      'if floor is None:
            return False, "cannot establish' 'if False:
            return False, "cannot establish'
run substring-lane        'return re.compile(r"(?<![A-Za-z0-9_.:/@+-])" + re.escape(nm) + r"(?![A-Za-z0-9_@+/-])(?![.:][A-Za-z0-9_])", re.I)' 'return re.compile(re.escape(nm), re.I)'
run lane-named-by-prose   'if any(_lane_marker_names(ln, rx) for ln in lines) or _lane_table_names(lines, rx):' 'if rx.search(txt):'
run lane-table-dropped    'for ln in lines) or _lane_table_names(lines, rx):' 'for ln in lines) or False:'
run lane-marker-substring '_LANE_MARK = re.compile(r"\blanes?\b", re.I)' '_LANE_MARK = re.compile(r"lanes?", re.I)'
run lane-left-colon       '(?<![A-Za-z0-9_.:/@+-])' '(?<![A-Za-z0-9_.@+/-])'
run lane-left-at-plus     '(?<![A-Za-z0-9_.:/@+-])' '(?<![A-Za-z0-9_.:/-])'
run lane-right-slash      '(?![A-Za-z0-9_@+/-])(?![.:]' '(?![A-Za-z0-9_@+-])(?![.:]'
run ticket-uses-lane-rule 'elif _ticket_rx(nm).search(txt):' 'elif _token_rx(nm).search(txt):'
run ruled-row-not-marked  '            if pl["ruled"]:
                breaker["history"][-1]["ruled"] = True' '            if False:
                breaker["history"][-1]["ruled"] = True'
# lane alias folding, worst-state-binds, exact bookkeeping names
run alias-not-folded      'return (m.group(1) + m.group(2) + "-" + str(int(m.group(3)))) if m else s' 'return s'
run key-case-sensitive    's = str(lane).strip().lower()' 's = str(lane).strip()'
run states-exact-dir-only 'if lane_key(n) == key:' 'if n == lane:'
run pass-first-dir-only   'for d, breaker, corrupt in states:
            # A PASS' 'for d, breaker, corrupt in states[:1]:
            # A PASS'
run reset-first-dir-only  'for pl in plans:
            breaker = pl["breaker"]' 'for pl in plans[:1]:
            breaker = pl["breaker"]'
run streak-summed         'worst["fail_streak"] = max(streaks)' 'worst["fail_streak"] = sum(streaks)'
run claim-last-dir-only   'worst["tripped"] = any(_tripped(x[1], x[2]) for x in states)' 'worst["tripped"] = _tripped(states[-1][1], states[-1][2])'
run corrupt-alias-open    'return bool(corrupt or b.get("tripped"))' 'return bool(b.get("tripped"))'
run bookkeeping-listed    'if p.is_dir() and p.name not in NON_LANE_DIRS' 'if p.is_dir()'
run bookkeeping-prefix    'p.is_dir() and p.name not in NON_LANE_DIRS' 'p.is_dir() and not p.name.startswith(".keylocks")'
run keylock-moved         'root() / ".keylocks" / name)' 'root() / ".keylocks2" / name)'
wait
rc=0
cat "$SCRATCH"/mut-*.out | sort
cat "$SCRATCH"/mut-*.out | grep -q -E "SURVIVED|could not apply" && rc=1
[ $rc -eq 0 ] && echo "lane-lock: mutation check PASS" || echo "lane-lock: mutation check FAIL"
exit $rc
