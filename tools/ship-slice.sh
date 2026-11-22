#!/usr/bin/env bash
# Ship one finished slice: checks, then a fast-forward-only push of main.  Usage: bash tools/ship-slice.sh <slice-name>
# Refuses (exit 1): force-style arguments, dirty tree, detached HEAD, not on main, failed core tests,
# failed prepush-check, remote not an ancestor of HEAD (non-fast-forward), remote moved before push.
# Private denylist: set HUBSTARTER_DENYLIST (a path outside the repo) to use it; otherwise --generic-only.
# On success prints "ship.push <sha>" after confirming `git ls-remote origin main` equals HEAD.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$HERE")"
die() { echo "ship-slice: REFUSED: $*" >&2; exit 1; }
for a in "$@"; do
  case "$a" in -f|-f*|--force*|--mirror|--delete|-d|+*|--no-verify) die "force-style argument '$a' is never accepted";; esac
done
[ $# -eq 1 ] || die "usage: ship-slice.sh <slice-name>"
SLICE="$1"
case "$SLICE" in *[!A-Za-z0-9._-]*|-*|"") die "bad slice name";; esac
cd "$ROOT" || die "cannot enter repo"
G() { git -C "$ROOT" "$@"; }
BR="$(G symbolic-ref -q --short HEAD)" || die "detached HEAD"
[ "$BR" = "main" ] || die "on '$BR', not main"
[ -z "$(G status --porcelain)" ] || die "dirty tree (commit or remove changes first)"
G fetch -q origin || die "fetch failed"
if G rev-parse -q --verify origin/main >/dev/null; then
  G merge-base --is-ancestor origin/main HEAD || die "non-fast-forward: origin/main is not an ancestor of HEAD"
fi
python3 "$HERE/scan.py" --guard-identity || die "this clone would commit as a personal identity (run: bash tools/install-hooks.sh)"
nice -n 10 bash "$HERE/test-all.sh" --core || die "core tests failed"
if [ -n "${HUBSTARTER_DENYLIST:-}" ]; then PA=(); else PA=(--generic-only); fi
nice -n 10 bash "$HERE/prepush-check.sh" ${PA[@]+"${PA[@]}"} || die "prepush-check failed"   # empty-array safe on macOS bash 3.2 under set -u
[ -z "$(G status --porcelain)" ] || die "checks left the tree dirty"
SHA="$(G rev-parse HEAD)"
# remote may have moved while the checks ran: re-fetch and re-verify right before the push
G fetch -q origin || die "re-fetch failed"
if G rev-parse -q --verify origin/main >/dev/null; then
  G merge-base --is-ancestor origin/main "$SHA" || die "remote moved during checks; not a fast-forward any more"
fi
G push origin "$SHA:refs/heads/main" || die "push rejected"
REMOTE="$(G ls-remote origin refs/heads/main | cut -f1)"
[ "$REMOTE" = "$SHA" ] || die "post-push check: ls-remote ($REMOTE) != HEAD ($SHA)"
echo "ship.push $SHA ($SLICE)"
