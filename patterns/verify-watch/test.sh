#!/usr/bin/env bash
# Runs the verify-watch tests in a fresh scratch HOME, alone.
#   bash test.sh            run the tests
#   bash test.sh --mutate   also break each guard in a temp copy and check the tests notice
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT
export HOME="$SCRATCH/home"; mkdir -p "$HOME"
unset HUBKIT_HOME VERIFY_HOSTS VERIFY_KILL VERIFY_NO_SOURCES VERIFY_TZ
export PYTHONDONTWRITEBYTECODE=1

command -v python3 >/dev/null || { echo "FAIL: python3 not found"; exit 1; }
python3 "$HERE/test_verify_watch.py" || { echo "FAIL: verify-watch tests"; exit 1; }
echo "verify-watch: tests PASS"

[ "${1:-}" = "--mutate" ] || exit 0

rc=0
mutate() {  # name, file, old text, new text
  local name="$1" file="$2" old="$3" new="$4" dir="$SCRATCH/mut-$1"
  mkdir -p "$dir"
  for f in verify_watch.py test_verify_watch.py; do cp "$HERE/$f" "$dir/$f"; done
  python3 - "$dir/$file" "$old" "$new" <<'PY' || { echo "mutation $name: could not apply (guard text changed?)"; rc=1; return; }
import sys
p, old, new = sys.argv[1:4]
s = open(p).read()
if old not in s:
    sys.exit(1)
open(p, "w").write(s.replace(old, new, 1))
PY
  if python3 "$dir/test_verify_watch.py" >/dev/null 2>&1; then
    echo "mutation $name: SURVIVED (tests did not notice)"; rc=1
  else
    echo "mutation $name: killed"
  fi
}
mutate allowlist-open verify_watch.py 'if host not in hosts:' 'if False:'
mutate https-optional verify_watch.py 'if not url.startswith("https://"):
            return None, "url must be https"' 'if False:
            return None, "url must be https"'
mutate always-pass-marker verify_watch.py 'low in _ALWAYS_PASS or ' ''
mutate cap-fails-open verify_watch.py 'return EMIT_CAP_PER_DAY
' 'return 0
'
mutate two-urls-ok verify_watch.py 'if len(urls) != 1:' 'if len(urls) < 1:'
mutate line-cap-loose verify_watch.py 'MAX_LINE = 400' 'MAX_LINE = 600'
mutate url-cap-loose verify_watch.py 'MAX_URL = 300' 'MAX_URL = 400'
mutate env-kill-dropped verify_watch.py 'os.environ.get("VW_OFF") == "1" or ' ''
mutate stranger-source-removable verify_watch.py 'return {"inbound-mail"} | {' 'return set() | {'
mutate naive-ts-skipped verify_watch.py '                if t.tzinfo is None:
                    t = t.replace(tzinfo=datetime.timezone.utc)' '                pass'
[ $rc -eq 0 ] && echo "verify-watch: mutation check PASS" || echo "verify-watch: mutation check FAIL"
exit $rc
