#!/usr/bin/env bash
# Publish gate. Run from anywhere; checks the repo this script lives in. Exit 0 only if ALL pass:
#   (a) private denylist scan  (b) generic scan (working tree AND every blob and message in all of git history)  (c) manifest equals git ls-files  (d) git identity
# The private denylist is NEVER inside this repo and has no default location: set
#   HUBSTARTER_DENYLIST=/path/to/your/private/list.txt   (env var), or pass --generic-only.
# History scope: by default the history scan judges only commits after the pinned baseline in tools/history-base.txt
# (every rule, no allow files). --full scans ALL history as before; it must be green before the repo is made public.
# A missing or bad pin falls back to the full scan. --generic-only skips the private list (for anyone who has none); the
# generic scan still runs. Output is FILE:LINE category only, never the matched text.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$HERE")"
DENY="${HUBSTARTER_DENYLIST:-}"
args=(--root "$ROOT" --manifest "$ROOT/MANIFEST.txt")
[ -f "$HERE/identity-allow.txt" ] && args+=(--identity-allow "$HERE/identity-allow.txt")   # frozen list of already-published commits with the personal identity
[ -f "$HERE/history-allow.txt" ] && args+=(--history-allow "$HERE/history-allow.txt")   # reviewed history blobs; every other blob in every commit is scanned
generic=0; full=0
for a in "$@"; do
  case "$a" in
    --generic-only) generic=1;;
    --full) full=1;;
    *) echo "prepush: unknown argument (use --generic-only, --full)"; exit 2;;
  esac
done
[ $full -eq 0 ] && [ -f "$HERE/history-base.txt" ] && args+=(--base-file "$HERE/history-base.txt")   # scan.py validates the pin and falls back to all history
[ $full -eq 1 ] && echo "prepush: --full, scanning ALL history"
if [ $generic -eq 1 ]; then
  echo "prepush: generic-only (private denylist NOT applied)"
else
  [ -n "$DENY" ] || { echo "prepush: FAIL no denylist given. Set HUBSTARTER_DENYLIST to your private list file, or run with --generic-only"; exit 2; }
  [ -f "$DENY" ] || { echo "prepush: FAIL private denylist missing (fails closed). Set HUBSTARTER_DENYLIST or use --generic-only"; exit 2; }
  case "$(cd "$(dirname "$DENY")" && pwd)/" in "$ROOT"/*) echo "prepush: FAIL denylist is inside the repo"; exit 2;; esac
  args+=(--denylist "$DENY" --dynamic)
fi
rc=0
python3 "$HERE/scan.py" "${args[@]}" || rc=1                      # (a)+(b)+(d), file walk covers manifest too
"$HERE/ship-list.sh" --check || rc=1                              # (c) manifest vs git ls-files
if [ -n "$(git -C "$ROOT" status --porcelain)" ]; then
  echo "prepush: uncommitted changes (scan covers the working tree; commit so the pushed tree is the scanned tree)"; rc=1
fi
[ $rc -eq 0 ] && echo "prepush: PASS" || echo "prepush: FAIL"
exit $rc
