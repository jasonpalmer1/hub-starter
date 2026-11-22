#!/usr/bin/env bash
# Mutation check: weaken scan.py in a temp copy, one guard at a time, and require the
# test suite to FAIL for every mutation. A mutation that survives means a guard is untested.
# Mutations run in parallel; each applies one exact-text replacement (and errors if the
# text is not found, so a refactor cannot silently turn a mutation into a no-op).
set -u
export PYTHONDONTWRITEBYTECODE=1   # no __pycache__ in the tree: a stray folder fails prepush
HERE="$(cd "$(dirname "$0")" && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
IFS= read -r -d '' MUTS <<'PYEOF' || true
confusables|.translate(CONFUSABLES)|
punctuation|if c.isascii() and c.isalnum())|if c.isascii())
ext-allowlist|if not (name in ALLOWED_NAMES or rel in ALLOWED_SCRIPT_PATHS or rel.endswith(ALLOWED_EXT)):|if False:
script-path|rel in ALLOWED_SCRIPT_PATHS or rel.endswith|True or rel.endswith
ext-blocklist|if name.startswith(".env") or|if False and
hex32|{32}(?![0-9A-Za-z])")),|{33}(?![0-9A-Za-z])")),
identity-denylist|if terms and term_hits_in_text(terms, ident):|if False:
identity-email|    if not (dom in OK_EMAIL_DOMAINS or|    if False and not (dom in OK_EMAIL_DOMAINS or
identity-name|    if name.lower() not in OK_IDENTITY_NAMES:|    if False:
identity-empty-email|    if not email or "@" not in email:|    if False:
identity-off|        if sha_full not in identity_allow:|        if False:
identity-allow-ignored|        if sha_full not in identity_allow:|        if True:
identity-allow-excuses-denylist|        if terms and term_hits_in_text(terms, ident):|        if terms and sha_full not in identity_allow and term_hits_in_text(terms, ident):
identity-allow-prefix-match|        if sha_full not in identity_allow:|        if sha_full[:8] not in {a[:8] for a in identity_allow}:
identity-allow-excuses-message|        if terms and term_hits_in_text(terms, parts[5]):|        if terms and sha_full not in identity_allow and term_hits_in_text(terms, parts[5]):
identity-allow-prefix-12|        if sha_full not in identity_allow:|        if sha_full[:12] not in {a[:12] for a in identity_allow}:
identity-committer-only|for nm, em in ((parts[1], parts[2]), (parts[3], parts[4])):|for nm, em in ((parts[3], parts[4]),):
identity-denylist-drops-committer|ident = "\n".join(parts[1:5])|ident = "\n".join(parts[1:3])
identity-allow-stale|    for stale in sorted(set(identity_allow) - seen):|    for stale in []:
guard-committer|("GIT_AUTHOR_IDENT", "GIT_COMMITTER_IDENT")|("GIT_AUTHOR_IDENT",)
guard-fail-open|        if not m or ident_bad(m.group(1), m.group(2)):|        if False:
manifest-extra|if rel not in ents:|if False:
binary-scan|text = raw.decode("utf-8", errors="replace")|text = "" if bytes([0]) in raw else raw.decode("utf-8", errors="replace")
multiline|blob = "".join(folded)|blob = "|".join(folded)
ticket-range|> MAX_FIXTURE_TICKET|> 99999
dict-strict|strict=(v in projnames and is_plain_word(v))|strict=False
ident-plain-needed|    if not any(is_plain_word(p) for p in parts):|    if False:
ident-lead-edge|rx = re.compile(r"(?<![a-z0-9])" + r"[-_.|rx = re.compile(r"" + r"[-_.
dict-cache|maxsize=None)|maxsize=0)
no-git|return [(".git", 0, "no-git-repo")]|return []
symlink|if os.path.islink(full):|if False:
history-blobs|out += history_blob_findings(root, terms, history_allow)|out += []
history-messages|for n, cat in content_findings(parts[5], tickets=False):|for n, cat in []:
history-allow|and sha not in history_allow:|and True:
rule-bearer|rer\s+[A-Za-z0-9._~+/=-]{16,}|rer\s+[A-Za-z0-9._~+/=-]{99,}
rule-jwt|J[A-Za-z0-9_-]{8,}\.ey|J[A-Za-z0-9_-]{88,}\.ey
rule-slack|[A-Za-z0-9/_-]{8,}")),|[A-Za-z0-9/_-]{88,}")),
rule-unquoted|[:=]\s*[A-Za-z0-9_/+=-]{20,}(?!|[:=]\s*[A-Za-z0-9_/+=-]{99,}(?!
rule-ip10|(?:10\.\d{1,3}|(?:11\.\d{1,3}
rule-ip192|192\.168|193\.168
rule-ip172|172\.(?:1[6-9]|173\.(?:1[6-9]
rule-cgnat|100\.(?:6[4-9]|101\.(?:6[4-9]
rule-tsnet|r"\.net\b"|r"\.nex\b"
rule-ticket-form|L[-_ ](\d{4,})|L[-](\d{4,})
base-honors-allow|        history_allow, identity_allow = (), ()|        pass
base-ignored|        rng = ["HEAD", "--not", base]|        rng = ["--all"]
base-head-only|        rng = ["HEAD", "--not", base]|        rng = ["-1", "HEAD"]
base-hex-unchecked|    if not isinstance(base, str) or not HEX40.fullmatch(base):|    if False:
base-ancestor-unchecked|    return a.returncode == 0|    return True
base-explicit-unvalidated|    elif explicit_base and not valid_base(root, base):|    elif False:
base-file-unvalidated|        if base is None or not valid_base(root, base):|        if base is None:
base-file-two-lines|    if len(lines) != 1 or not HEX40.fullmatch(lines[0]):|    if len(lines) < 1 or not HEX40.fullmatch(lines[0]):
PYEOF
# The test suite reads README.md (one level above tools/) and runs prepush-check.sh, so the temp layout copies both.
layout() {
  mkdir -p "$1/tools"
  cp -R "$HERE"/scan.py "$HERE"/test_scan.py "$HERE"/identity-allow.txt "$HERE"/githooks "$HERE"/prepush-check.sh "$HERE"/ship-list.sh "$HERE"/ship_list.py "$HERE"/history-base.txt "$1/tools"/ || return 1
  cp "$HERE/../README.md" "$1/" || return 1
}
run_one() {
  name="$1"; old="$2"; new="$3"; d="$WORK/$name"
  layout "$d" || { echo "ERROR   $name (layout failed)"; return; }
  d="$d/tools"
  OLD="$old" NEW="$new" F="$d/scan.py" python3 - <<'PY' || { echo "ERROR   $name (pattern not found)"; return; }
import os, sys
f = os.environ["F"]; s = open(f, encoding="utf-8").read()
old, new = os.environ["OLD"].replace("\\x00", "\x00"), os.environ["NEW"].replace("\\x00", "\x00")
if old not in s: sys.exit(1)
open(f, "w", encoding="utf-8").write(s.replace(old, new))
PY
  # Classify by exit code: 0 = tests passed (mutant SURVIVED); 124 (timeout), 126/127 (not runnable / not found) and
  # >=128 (killed by a signal, e.g. OOM or a sweeper) mean the tests never judged the mutant = ERROR, never a kill.
  # 125 is what GNU timeout (and git/docker) return when the wrapper itself failed, so the tests never ran either.
  # Only an ordinary test failure (1..123) that ALSO shows unittest ran at least one test and reported FAILED counts as KILLED:
  # a wrapper that exits 1 without running any assertion is ERROR, not proof the guard works.
  python3 -W ignore "$d/test_scan.py" >"$WORK/$name.testout" 2>&1; rc=$?
  if [ "$rc" -eq 0 ]; then echo "SURVIVED $name"
  elif [ "$rc" -eq 124 ] || [ "$rc" -eq 125 ] || [ "$rc" -eq 126 ] || [ "$rc" -eq 127 ] || [ "$rc" -ge 128 ]; then echo "ERROR    $name (test run died rc=$rc)"
  elif grep -Eq '^Ran [1-9][0-9]* tests? in ' "$WORK/$name.testout" && grep -Eq '^FAILED \((failures|errors)=' "$WORK/$name.testout"; then echo "KILLED   $name"
  else echo "ERROR    $name (test run exited rc=$rc without running assertions)"; fi
}
# Baseline first: the unmutated suite must pass in this layout, or every mutant would "die" for the wrong reason.
if ! layout "$WORK/baseline" || ! python3 -W ignore "$WORK/baseline/tools/test_scan.py" >"$WORK/baseline.log" 2>&1; then
  tail -15 "$WORK/baseline.log" 2>/dev/null
  echo "mutation-check: BASELINE RED (the unmutated suite fails in the temp layout; no mutation result can be trusted)"
  exit 1
fi
# MUT_SHARD=1 or 2 runs only half of the mutations (to keep machine load down).
# MUT_ONLY=name1,name2 runs only the named mutations (used by the hygiene test for a fast run).
# EXPECT counts the mutants this run was asked to start; the run must return exactly that many results.
i=0; EXPECT=0; TOTAL=0
while IFS='|' read -r name old new; do
  i=$((i+1))
  [ -z "$name" ] && continue
  TOTAL=$((TOTAL+1))
  [ -n "${MUT_SHARD:-}" ] && [ $((i%2+1)) -ne "$MUT_SHARD" ] && continue
  if [ -n "${MUT_ONLY:-}" ]; then case ",$MUT_ONLY," in *",$name,"*) ;; *) continue;; esac; fi
  EXPECT=$((EXPECT+1))
  run_one "$name" "$old" "$new" > "$WORK/$name.out" &
done <<< "$MUTS"
wait
cat "$WORK"/*.out | sort
# A lost worker (killed, no result line) must not pass silently: results must equal mutants started.
GOT=$(cat "$WORK"/*.out 2>/dev/null | grep -Ec '^(KILLED|SURVIVED|ERROR) ')
if [ "$EXPECT" -eq 0 ]; then echo "mutation-check: FAIL (no mutations selected)"; exit 1; fi
if [ "$GOT" -ne "$EXPECT" ]; then echo "mutation-check: FAIL (expected $EXPECT results, got $GOT: a worker was lost or reported twice)"; exit 1; fi
if cat "$WORK"/*.out | grep -qv '^KILLED'; then echo "mutation-check: FAIL (a mutation survived or errored)"; exit 1; fi
if [ "$EXPECT" -lt "$TOTAL" ]; then echo "mutation-check: PASS (subset: $EXPECT of $TOTAL mutations killed; MUT_ONLY/MUT_SHARD set, NOT a full run)"; exit 0; fi
echo "mutation-check: PASS (all $EXPECT mutations killed)"
