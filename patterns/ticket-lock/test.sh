#!/usr/bin/env bash
# Runs the ticket-lock tests in a fresh scratch HOME, alone, with nothing else installed.
#   bash test.sh            run the tests
#   bash test.sh --mutate   also break each guard in a temp copy and check the tests notice
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT
export HOME="$SCRATCH/home"; mkdir -p "$HOME"
unset HUBKIT_HOME TICKET_LOCK_DIR TICKET_LOCK_ALARM_LOG TICKET_LOCK_TTL_S TICKET_LOCK_NOW TICKET_LOCK_ID_RE MODEL_GUARD_LOG LIB_DIR
export PYTHONDONTWRITEBYTECODE=1

command -v python3 >/dev/null || { echo "FAIL: python3 not found"; exit 1; }
python3 "$HERE/test_ticket_lock.py" || { echo "FAIL: ticket-lock tests"; exit 1; }
echo "ticket-lock: tests PASS"

[ "${1:-}" = "--mutate" ] || exit 0

# Each mutation edits ONE guard in a temp copy of the library; the suite must then FAIL.
rc=0
mutate() {  # name, old text, new text
  local name="$1" old="$2" new="$3" dir="$SCRATCH/mut-$1"
  mkdir -p "$dir"
  python3 - "$HERE/ticket_lock.py" "$dir/ticket_lock.py" "$old" "$new" <<'PY' || { echo "mutation $name: could not apply (guard text changed?)"; rc=1; return; }
import sys
src, dst, old, new = sys.argv[1:5]
s = open(src).read()
if old not in s:
    sys.exit(1)
open(dst, "w").write(s.replace(old, new, 1))
PY
  if LIB_DIR="$dir" python3 "$HERE/test_ticket_lock.py" >/dev/null 2>&1; then
    echo "mutation $name: SURVIVED (tests did not notice)"; rc=1
  else
    echo "mutation $name: killed"
  fi
}
mutate held-lock-ignored     'if held and not handoff_from:' 'if False:'
mutate handoff-always-wins   'if held and not handoff_from:' 'if held and False:'
mutate gate-types-lock       'LOCK_TYPES = frozenset(("builder", "general-purpose", "claude"))' 'LOCK_TYPES = frozenset(("builder", "general-purpose", "claude", "bug-gate"))'
mutate dup-ok-ignored        'if "DUP-OK" in prompt:' 'if False:'
mutate ttl-ignored           'return -FUTURE_SKEW_S <= age < ttl_s()' 'return True'
mutate future-claim-live     'return -FUTURE_SKEW_S <= age < ttl_s()' 'return age < ttl_s()'
mutate provisional-never-lapses 'if age > PROVISIONAL_S:' 'if False:'
mutate refresh-no-min-age    'and age >= MIN_REFRESH_S:' 'and True:'
mutate refresh-any-brief     'and ph and lk.get("phash") == ph' 'and True'
mutate started-never-proven  '        if lk.get("agent_id"):
            return True' '        if lk.get("agent_id"):
            return False'
mutate prompt-ids-lock       'tickets = tickets_of(desc)' 'tickets = tickets_of(desc + " " + prompt)'
mutate redispatch-no-age     'n - float(lk.get("started") or 0) > REDISPATCH_AGE_S' 'True'
mutate redispatch-any-session 'lk.get("session") == session_id and' ''
mutate handoff-mid-line      'HANDOFF_RE = re.compile(r"^[ \t]*HANDOFF-FROM:[ \t]*(\S[^\n]*)", re.M)' 'HANDOFF_RE = re.compile(r"HANDOFF-FROM:[ \t]*(\S[^\n]*)", re.M)'
mutate release-keeps-lock    '        for t, lk in mine:
            try:
                os.unlink(_path(t))' '        for t, lk in []:
            try:
                os.unlink(_path(t))'
mutate release-exact-only    'hashlib.sha256(hay[:n].encode' 'hashlib.sha256(hay.encode'
mutate release-short-prefix  'hashlib.sha256(hay[:n].encode("utf-8", "replace")).hexdigest() == lk["phash"]' 'True'
mutate close-any-status      'and seg[3] in ("done", "closed")' 'and True'
mutate id-shape-fixed        'return re.compile(os.environ.get("TICKET_LOCK_ID_RE") or DEFAULT_ID_RE)' 'return re.compile(DEFAULT_ID_RE)'
mutate evidence-hash-dropped ' and hashlib.sha256(hay[:pl].encode("utf-8", "replace")).hexdigest() == ph:' ':'
mutate evidence-prefix-slice-dropped 'hashlib.sha256(hay[:pl].encode(' 'hashlib.sha256(hay.encode('
mutate evidence-mtime-dropped 'if os.path.getmtime(fp) < float(lk.get("since") or lk.get("started") or 0):' 'if False:'
mutate evidence-session-gate-dropped 'if not (sid and ph and pl):' 'if not (ph and pl):'
mutate evidence-plen-gate-dropped 'if not (sid and ph and pl):' 'if not (sid and ph):'
# Not mutated: `len(hay) >= pl` in has_started is redundant with the hash (a text shorter than the brief can never
# hash to the brief's hash), so dropping it changes no behaviour. test_transcript_shorter_than_the_brief pins the intent.
mutate blocked-claim-still-owns 'if was_blocked(lk):' 'if False:'
mutate blocked-needs-no-error-flag 'and b.get("is_error") is True):' '):'
mutate blocked-hook-phrase-unanchored 're.match(r"\s*PreToolUse:' 're.search(r"\s*PreToolUse:'
mutate blocked-ignores-started-worker 'if has_started(lk):
            return False
        key, sid' 'if False:
            return False
        key, sid'
mutate blocked-ignores-tool-use-id 'and b.get("tool_use_id") == key' 'and True'
mutate blocked-except-guard-dropped 'r"\s*PreToolUse:\S+ hook error", t):
                                return True
    except Exception:' 'r"\s*PreToolUse:\S+ hook error", t):
                                return True
    except KeyError:'
mutate blocked-glob-escape-dropped '_session_glob().replace("{sid}", glob.escape(sid))' '_session_glob().replace("{sid}", sid)'
mutate started-glob-escape-dropped '_projects_glob().replace("{sid}", glob.escape(sid))' '_projects_glob().replace("{sid}", sid)'
mutate blocked-toolu-check-dropped 'key.startswith("toolu_") and sid' 'key and sid'
mutate blocked-list-join-dropped 't = " ".join(x.get("text", "") for x in t if isinstance(x, dict))' 't = None'
mutate blocked-list-join-no-space 't = " ".join(' 't = "".join('
mutate blocked-leading-ws-dropped 're.match(r"\s*PreToolUse:' 're.match(r"PreToolUse:'
mutate blocked-hook-name-optional 'PreToolUse:\S+ hook error' 'PreToolUse:\S* hook error'
mutate sid-validation-dropped 'if not _sid_ok(sid):' 'if False:'
mutate sid-dotdot-dropped ' and ".." not in sid' ''
mutate sid-separator-dropped ' and not any(c in sid for c in "/\\\0")' ''
mutate refresh-boundary-exclusive 'and age >= MIN_REFRESH_S:' 'and age > MIN_REFRESH_S:'
mutate provisional-boundary-inclusive 'if age > PROVISIONAL_S:' 'if age >= PROVISIONAL_S:'
[ $rc -eq 0 ] && echo "ticket-lock: mutation check PASS" || echo "ticket-lock: mutation check FAIL"
exit $rc
