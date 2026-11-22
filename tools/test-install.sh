#!/usr/bin/env bash
# Runs the installer and pattern.json tests in a scratch HOME (never your real ~/.claude).
#   bash tools/test-install.sh            run the tests
#   bash tools/test-install.sh --mutate   also break each installer guard in a temp copy; tests must fail
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(dirname "$HERE")"
SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT
export HOME="$SCRATCH/home"; mkdir -p "$HOME"
unset HUBKIT_HOME INSTALL_ROOT
export PYTHONDONTWRITEBYTECODE=1
command -v python3 >/dev/null || { echo "FAIL: python3 not found"; exit 1; }
python3 -W ignore "$HERE/test_install.py" || { echo "FAIL: install tests"; exit 1; }
echo "install: tests PASS"
[ "${1:-}" = "--mutate" ] || exit 0

rc=0
mutate() {  # name, old text, new text: edit tools/install.py in a temp copy; the suite must then FAIL
  local name="$1" old="$2" new="$3" dir="$SCRATCH/mut-$1"
  mkdir -p "$dir/tools"
  cp -R "$REPO/install.sh" "$REPO/patterns" "$REPO/kit" "$dir"/
  cp "$HERE/install.py" "$dir/tools/"
  python3 - "$dir/tools/install.py" "$old" "$new" <<'PY' || { echo "mutation $name: could not apply (text changed?)"; rc=1; return; }
import sys
p, old, new = sys.argv[1:4]
s = open(p).read()
if old not in s:
    sys.exit(1)
open(p, "w").write(s.replace(old, new, 1))
PY
  if INSTALL_ROOT="$dir" python3 -W ignore "$HERE/test_install.py" >/dev/null 2>&1; then
    echo "mutation $name: SURVIVED (tests did not notice)"; rc=1
  else
    echo "mutation $name: killed"
  fi
}
# O_EXCL is a second lock behind the lexists check (an equivalent mutant on its own), so it is not mutated alone
mutate overwrite-check 'if os.path.lexists(dest):
            try:' 'if False:
            try:'
mutate dry-run-writes  'if self.dry:
            return' 'if False:
            return'
mutate always-log      'if r.new and not r.dry:' 'if not r.dry:'
mutate no-home-check   'if not home or not os.path.isabs(home):' 'if False:'
mutate settings-write  '    p = print' '    open(os.path.join(claude, "settings.json"), "a").close()
    p = print'
mutate follow-symlink  'os.path.isfile(dest) and not os.path.islink(dest) and read(dest) == data' 'os.path.isfile(dest) and read(dest) == data'
mutate skip-dep        'add(dep, "needed by %s" % n)' 'pass'
mutate prefix-name     'return re.sub(rb"(?m)^name:' 'return data; re.sub(rb"(?m)^name:'
[ $rc -eq 0 ] && echo "install: all mutations killed" || echo "install: MUTATION FAILURE"
exit $rc
