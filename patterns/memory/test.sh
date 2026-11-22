#!/usr/bin/env bash
# Runs the memory tests in a scratch HOME. Hooks are probed by feeding JSON on stdin only.
#   bash test.sh            run the tests
#   bash test.sh --mutate   also break each guard in a temp copy and check the tests notice
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT
export HOME="$SCRATCH/home"; mkdir -p "$HOME"
export PYTHONDONTWRITEBYTECODE=1
unset MEMORY_DIR HUBKIT_HOME
command -v python3 >/dev/null || { echo "FAIL: python3 not found"; exit 1; }
python3 "$HERE/test_memory.py" || { echo "FAIL: memory tests"; exit 1; }
echo "memory: tests PASS"
[ "${1:-}" = "--mutate" ] || exit 0

rc=0
mutate() {  # name, file, old text, new text
  local name="$1" file="$2" old="$3" new="$4" dir="$SCRATCH/mut-$1"
  mkdir -p "$dir"; cp "$HERE"/* "$dir"/
  python3 - "$dir/$file" "$old" "$new" <<'PY' || { echo "mutation $name: could not apply"; rc=1; return; }
import sys
p, old, new = sys.argv[1:4]
s = open(p).read()
if old not in s:
    sys.exit(1)
open(p, "w").write(s.replace(old, new, 1))
PY
  if python3 "$dir/test_memory.py" >/dev/null 2>&1; then
    echo "mutation $name: SURVIVED"; rc=1
  else
    echo "mutation $name: killed"
  fi
}
mutate overwrite-allowed  memory_init.py      'if os.path.lexists(target):' 'if False:'
mutate id-not-validated   pre_compact_stub.py 'ID_RE.fullmatch(v) else "unknown"' 'ID_RE.match(v) else "unknown"'
mutate blocks-on-error    pre_compact_stub.py '    except OSError:
        pass
    return 0' '    except OSError:
        return 2
    return 0'
mutate relative-dir-ok    memory_init.py      'if not os.path.isabs(env):' 'if False:'
mutate pickup-noisy       compact_pickup.py   'if not pick:
        return 0' 'if not pick:
        print("x")
        return 0'
[ $rc -eq 0 ] && echo "memory: mutation check PASS" || echo "memory: mutation check FAIL"
exit $rc
