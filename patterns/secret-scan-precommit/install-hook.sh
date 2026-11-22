#!/usr/bin/env bash
# Wire secret-scan.py into ONE git repo as its pre-commit hook. Never overwrites a hook.
#   bash install-hook.sh /path/to/repo
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="${1:-}"
[ -n "$REPO" ] && git -C "$REPO" rev-parse --git-dir >/dev/null 2>&1 || { echo "install-hook: give the path of a git repo"; exit 2; }
# the hooks FOLDER, then the name: asking git for the file path would resolve a symlinked hook to its target
HOOK="$(git -C "$REPO" rev-parse --path-format=absolute --git-path hooks)/pre-commit"
LINE="python3 \"$HERE/secret-scan.py\" || exit 1"
# "Already wired" means our exact line is in the hook as a whole line, with no `exit` before it
# (an `exit 0` above it would make the scan never run). A comment or a mention does not count.
wired() {
  [ -f "$HOOK" ] && LINE="$LINE" awk 'BEGIN{L=ENVIRON["LINE"]} $0==L{f=1;exit} /^[ \t]*exit([ \t]|$)/{exit} END{exit f?0:1}' "$HOOK"
}
if [ -e "$HOOK" ] || [ -L "$HOOK" ]; then
  if wired; then echo "install-hook: already wired ($HOOK)"; exit 0; fi
  echo "install-hook: $HOOK already exists and is not ours; not touching it."
  echo "Add this line to it yourself:"
  echo "  $LINE"
  exit 1
fi
mkdir -p "$(dirname "$HOOK")"
printf '#!/bin/sh\n# added by hub-starter secret-scan-precommit\n%s\n' "$LINE" > "$HOOK"
chmod +x "$HOOK"
echo "install-hook: wired $HOOK"
