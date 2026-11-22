#!/usr/bin/env bash
# Tests tools/ship-slice.sh against a TEMP bare remote (never the real one). Each case builds a fresh sandbox.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(dirname "$HERE")"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
export PYTHONDONTWRITEBYTECODE=1 GIT_AUTHOR_NAME=hub-starter GIT_AUTHOR_EMAIL=hub-starter@users.noreply.github.com GIT_COMMITTER_NAME=hub-starter GIT_COMMITTER_EMAIL=hub-starter@users.noreply.github.com
unset HUBSTARTER_DENYLIST
FAILS=0; N=0
ok()  { N=$((N+1)); echo "ok   $1"; }
bad() { N=$((N+1)); FAILS=$((FAILS+1)); echo "FAIL $1"; }
STUB_PASS='#!/usr/bin/env bash
echo stub-core-pass; exit 0'
mk() {  # mk <name>: sandbox repo (copy of tracked files, stub test-all) + bare origin with the same first commit pushed
  local d="$TMP/$1"; mkdir -p "$d/work"
  git init -q --bare "$d/remote.git"
  git -C "$REPO" ls-files | while IFS= read -r f; do mkdir -p "$d/work/$(dirname "$f")"; cp -p "$REPO/$f" "$d/work/$f"; done
  printf '%s\n' "$STUB_PASS" > "$d/work/tools/test-all.sh"
  rm -f "$d/work/tools/identity-allow.txt"   # the copy has fresh history; the frozen sha list only fits the real one
  git -C "$d/work" init -q -b main
  git -C "$d/work" config user.name hub-starter; git -C "$d/work" config user.email hub-starter@users.noreply.github.com
  git -C "$d/work" remote add origin "$d/remote.git"
  python3 "$d/work/tools/ship_list.py" --write >/dev/null 2>&1; git -C "$d/work" add . ; python3 "$d/work/tools/ship_list.py" --write >/dev/null 2>&1
  git -C "$d/work" add . && git -C "$d/work" commit -q -m base && git -C "$d/work" push -q origin main
  echo "$d"
}
run() { bash "$1/work/tools/ship-slice.sh" "${@:2}" >"$1/out.txt" 2>&1; echo $?; }
commit_file() { echo "$2" > "$1/work/$3"; git -C "$1/work" add . ; python3 "$1/work/tools/ship_list.py" --write >/dev/null 2>&1; git -C "$1/work" add . ; git -C "$1/work" commit -q -m "$3"; }

# 0 sanity: clean ff push works and prints ship.push <sha>
d=$(mk ok); commit_file "$d" hello a.txt; rc=$(run "$d" T0)
want=$(git -C "$d/work" rev-parse HEAD)
{ [ "$rc" = 0 ] && grep -q "^ship.push $want" "$d/out.txt" && [ "$(git -C "$d/remote.git" rev-parse main)" = "$want" ]; } && ok "ff push succeeds and prints ship.push" || { bad "ff push succeeds"; cat "$d/out.txt"; }

# 1 non-fast-forward refused
d=$(mk nff); git clone -q "$d/remote.git" "$d/other"; echo x > "$d/other/x.txt"; git -C "$d/other" add .; git -C "$d/other" commit -q -m other; git -C "$d/other" push -q origin HEAD:main
commit_file "$d" local b.txt; before=$(git -C "$d/remote.git" rev-parse main); rc=$(run "$d" T1)
{ [ "$rc" != 0 ] && grep -q "non-fast-forward" "$d/out.txt" && [ "$(git -C "$d/remote.git" rev-parse main)" = "$before" ]; } && ok "non-ff refused, remote untouched" || { bad "non-ff refused"; cat "$d/out.txt"; }

# 2 dirty tree refused
d=$(mk dirty); echo junk > "$d/work/untracked.txt"; rc=$(run "$d" T2)
{ [ "$rc" != 0 ] && grep -q "dirty tree" "$d/out.txt"; } && ok "dirty tree refused" || { bad "dirty tree refused"; cat "$d/out.txt"; }

# 3 detached HEAD refused
d=$(mk det); git -C "$d/work" checkout -q --detach; rc=$(run "$d" T3)
{ [ "$rc" != 0 ] && grep -q "detached HEAD" "$d/out.txt"; } && ok "detached HEAD refused" || { bad "detached HEAD refused"; cat "$d/out.txt"; }

# 4 planted absolute home path refused by prepush (string built from two halves; this repo never holds it)
d=$(mk leak); A="/Us"; B="ers/someone/x"; commit_file "$d" "path $A$B" leak.txt; before=$(git -C "$d/remote.git" rev-parse main); rc=$(run "$d" T4)
{ [ "$rc" != 0 ] && grep -q "prepush-check failed" "$d/out.txt" && [ "$(git -C "$d/remote.git" rev-parse main)" = "$before" ]; } && ok "planted home path refused by prepush, nothing pushed" || { bad "planted path refused"; cat "$d/out.txt"; }

# 5 force-style arguments refused and never passed through
for flag in --force -f --force-with-lease +main --delete; do
  d=$(mk "force$flag"); commit_file "$d" f c.txt; before=$(git -C "$d/remote.git" rev-parse main); rc=$(run "$d" "$flag")
  { [ "$rc" != 0 ] && grep -q "never accepted" "$d/out.txt" && [ "$(git -C "$d/remote.git" rev-parse main)" = "$before" ]; } && ok "argument $flag refused" || { bad "argument $flag refused"; cat "$d/out.txt"; }
done
d=$(mk force2); commit_file "$d" f d.txt; rc=$(run "$d" T5 --force)
[ "$rc" != 0 ] && ok "force flag after slice name refused" || bad "force flag after slice name refused"

# 6 remote moves between the check and the push (the core-test step pushes from another clone)
d=$(mk race); git clone -q "$d/remote.git" "$d/other"; commit_file "$d" local e.txt
cat > "$d/work/tools/test-all.sh" <<EOS
#!/usr/bin/env bash
echo racing; cd "$d/other" && echo r > r.txt && git add r.txt && git commit -q -m racer && git push -q origin HEAD:main; exit 0
EOS
git -C "$d/work" add .; git -C "$d/work" commit -q -m stubrace
rc=$(run "$d" T6); racer=$(git -C "$d/other" rev-parse HEAD)
{ [ "$rc" != 0 ] && grep -q "remote moved" "$d/out.txt" && [ "$(git -C "$d/remote.git" rev-parse main)" = "$racer" ]; } && ok "remote moved mid-run refused, racer commit kept" || { bad "remote moved mid-run refused"; cat "$d/out.txt"; }

# 7 failed core tests refused
d=$(mk core); commit_file "$d" c f.txt; printf '#!/usr/bin/env bash\nexit 1\n' > "$d/work/tools/test-all.sh"; git -C "$d/work" add .; git -C "$d/work" commit -q -m failcore
before=$(git -C "$d/remote.git" rev-parse main); rc=$(run "$d" T7)
{ [ "$rc" != 0 ] && grep -q "core tests failed" "$d/out.txt" && [ "$(git -C "$d/remote.git" rev-parse main)" = "$before" ]; } && ok "failed core tests refused" || { bad "failed core tests refused"; cat "$d/out.txt"; }

# 8 private denylist set: the empty option list must not trip set -u on macOS bash 3.2 (/bin/bash); prepush must run in denylist mode.
#   (The sandbox's own git identity trips --dynamic here, so the push itself is not asserted; only that the scan ran with the denylist.)
d=$(mk deny); commit_file "$d" g g.txt; : > "$TMP/deny.txt"
rc=$(HUBSTARTER_DENYLIST="$TMP/deny.txt" /bin/bash "$d/work/tools/ship-slice.sh" T8 >"$d/out.txt" 2>&1; echo $?)
{ ! grep -q "unbound variable" "$d/out.txt" && ! grep -q "generic-only" "$d/out.txt" && grep -qE "^scan: [0-9]+ finding" "$d/out.txt"; } && ok "denylist set: runs under /bin/bash and prepush applies the denylist" || { bad "denylist set under /bin/bash"; cat "$d/out.txt"; }

# 9 a clone that would commit as a personal identity is refused before anything runs; nothing pushed
d=$(mk ident); commit_file "$d" i i.txt; git -C "$d/work" config user.name "Pat Example"; before=$(git -C "$d/remote.git" rev-parse main); rc=$(env -u GIT_AUTHOR_NAME -u GIT_COMMITTER_NAME bash "$d/work/tools/ship-slice.sh" T9 >"$d/out.txt" 2>&1; echo $?)
{ [ "$rc" != 0 ] && grep -q "personal identity" "$d/out.txt" && [ "$(git -C "$d/remote.git" rev-parse main)" = "$before" ]; } && ok "personal identity clone refused, nothing pushed" || { bad "personal identity clone refused"; cat "$d/out.txt"; }

echo "ship-slice tests: $((N-FAILS))/$N passed"
[ $FAILS -eq 0 ]
