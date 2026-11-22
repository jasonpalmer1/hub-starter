#!/usr/bin/env bash
# Runs the failure-register tests in a fresh scratch HOME, alone, with nothing else installed.
#   bash test.sh            run the tests
#   bash test.sh --mutate   also break each rule in a temp copy and check the tests notice
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT
export HOME="$SCRATCH/home"; mkdir -p "$HOME"
unset HUBKIT_HOME FR_DIR
export PYTHONDONTWRITEBYTECODE=1

command -v python3 >/dev/null || { echo "FAIL: python3 not found"; exit 1; }
python3 -W ignore "$HERE/test_failure_register.py" || { echo "FAIL: failure-register tests"; exit 1; }
echo "failure-register: tests PASS"

[ "${1:-}" = "--mutate" ] || exit 0

# Each mutation edits ONE rule in a temp copy of the tools; the suite must then FAIL.
rc=0
mutate() {  # name, file, old text, new text
  local name="$1" dir="$SCRATCH/mut-$1"
  mkdir -p "$dir"
  cp "$HERE/failure_register.py" "$HERE/record_failure.py" "$dir/"
  python3 - "$dir/$2" "$3" "$4" <<'PY' || { echo "mutation $name: could not apply (rule text changed?)"; rc=1; return; }
import sys
p, old, new = sys.argv[1:4]
s = open(p).read()
if old not in s:
    sys.exit(1)
open(p, "w").write(s.replace(old, new, 1))
PY
  if FR_DIR="$dir" python3 -W ignore "$HERE/test_failure_register.py" >/dev/null 2>&1; then
    echo "mutation $name: SURVIVED (tests did not notice)"; rc=1
  else
    echo "mutation $name: killed"
  fi
}
R=failure_register.py
H=record_failure.py
mutate threshold-2 $R 'MIN_COUNT = 3' 'MIN_COUNT = 2'
mutate threshold-4 $R 'MIN_COUNT = 3' 'MIN_COUNT = 4'
mutate burst-off $R '<= burst:' '<= -1:'
mutate window-ignored $R 'events = [e for e in events if since <= e.ts <= until]' 'events = list(events)'
mutate ts-not-normalised $R '    t = _TS.sub("<ts>", t)
' ''
mutate hex-not-normalised $R '    t = _HEX.sub("<id>", t)
' ''
mutate duration-not-normalised $R '    t = _DUR.sub("<dur>", t)
' ''
mutate numbers-not-normalised $R '    t = _NUM.sub("N", t)
' ''
mutate uuid-not-normalised $R '    t = _UUID.sub("<id>", t)
    t = _LONG' '    t = _LONG'
mutate control-chars-kept $R '    t = re.sub(r"[\x00-\x1f\x7f]", " ", t)
' ''
mutate pipe-not-escaped $R '.replace("|", "/") or "-"' ' or "-"'
mutate paths-leak $R '    t = _PATH.sub("<path>", t)
' ''
mutate credential-patterns-off $R 'for rx in _SECRETS:' 'for rx in []:'
mutate keyval-off $R '    t = _KEYVAL.sub(lambda m: m.group(1) + m.group(2) + "<secret>", t)
' ''
mutate long-token-leaks $R '    t = _LONG.sub(_long_token, t)
' ''
mutate cause-not-scrubbed $R 'return re.sub(r"\s+", " ", scrub(text).replace' 'return re.sub(r"\s+", " ", str(text).replace'
mutate rotation-double-counts $R '                    if line in seen_other:
                        continue' '                    if False:
                        continue'
mutate corrupt-line-crashes $R '        except ValueError:
            bad += 1
            continue' '        except KeyError:
            bad += 1
            continue'
mutate corrupt-not-reported $R '    if bad:
        warn.append' '    if False:
        warn.append'
mutate exit-code-ignored $R 'if isinstance(exit_code, int) and not isinstance(exit_code, bool) and exit_code != 0:' 'if False:'
mutate http404-ignored $R 'if "404" in low:' 'if False:'
mutate no-heartbeat $R 'with open(os.path.join(out_dir, "failure-register.tick"), "w") as fh:' 'with open(os.devnull, "w") as fh:'
mutate replay-writes-heartbeat $R '    if not replay:
        with open(os.path.join(out_dir, "failure-register.tick")' '    if True:
        with open(os.path.join(out_dir, "failure-register.tick")'
mutate cause-forgotten $R 'causes[cells[0]] = cells[-2]' 'causes[cells[0]] = ""'
mutate no-reopen $R 'if fixed and row["last"] > fixed:' 'if False:'
mutate by-design-ignored $R 'if cause.startswith("by-design"):
        return "by-design"' 'if False:
        return "by-design"'
mutate passed-since-ignored $R 'if row["pass"] and row["pass"] > row["last"]:' 'if False:'
mutate standing-zero-flagged $R '        if code != 0:
            out.append' '        if True:
            out.append'
mutate opens-a-ticket $R 'print("wrote %s: ' 'open(os.path.join(os.path.dirname(os.path.abspath(out_dir)), "ledger.jsonl"), "a").write("{}\n"); print("wrote %s: '
mutate writes-outside-out $R 'print("wrote %s: ' 'open(os.path.join(os.path.dirname(os.path.abspath(out_dir)), "notes.txt"), "a").write("x"); print("wrote %s: '
mutate no-repeat-event $R 'emit(out_dir, "failure.repeat"' 'emit(out_dir, "failure.repeatX"'
mutate explain-unscrubbed $R 'raw = scrub(" ".join(' 'raw = (" ".join('
mutate helper-no-scrub $H 'return re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f\x7f]", " ", fr.scrub(text))).strip()[:limit]' 'return re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f\x7f]", " ", str(text))).strip()[:limit]'
mutate helper-truncates $H 'os.O_WRONLY | os.O_APPEND | os.O_CREAT' 'os.O_WRONLY | os.O_TRUNC | os.O_CREAT'
mutate helper-raises $H 'except OSError as ex:' 'except KeyError as ex:'
mutate helper-no-cap $H 'TEXT_LIMIT = 300' 'TEXT_LIMIT = 300000'
[ $rc -eq 0 ] && echo "failure-register: mutation check PASS" || echo "failure-register: mutation check FAIL"
exit $rc
