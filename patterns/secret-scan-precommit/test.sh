#!/usr/bin/env bash
# Runs the secret-scan tests in a fresh scratch HOME, alone, with nothing else installed.
#   bash test.sh            run the tests
#   bash test.sh --mutate   also break each guard in a temp copy and check the tests notice
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT
export HOME="$SCRATCH/home"; mkdir -p "$HOME"
unset SECRET_SCAN SECRET_SCAN_VALUE_FILES SECRET_SCAN_ENV_ROOTS SECRET_SCAN_ALLOW_PATHS
export PYTHONDONTWRITEBYTECODE=1

command -v python3 >/dev/null || { echo "FAIL: python3 not found"; exit 1; }
command -v git >/dev/null || { echo "FAIL: git not found"; exit 1; }
python3 -W ignore "$HERE/test_secret_scan.py" || { echo "FAIL: secret-scan tests"; exit 1; }
echo "secret-scan-precommit: tests PASS"

[ "${1:-}" = "--mutate" ] || exit 0

# Equivalent mutants: edits that cannot change any observable behaviour, so no test can kill them.
#   flags-prefix: --src-prefix/--dst-prefix on the blob-to-blob diff. The scanner reads only lines
#   starting "+" and the "+++" header holds a sha, so the prefix text is never read; checked equal
#   under 13 diff configs. (--text, --no-ext-diff and --no-textconv are NOT equivalent: a `*`
#   attribute pattern and diff.external do apply to a blob-to-blob diff; see FlagGuards.)
EQUIVALENT="flags-prefix"

# Each mutation edits ONE guard in a temp copy; the suite must then FAIL. Four run at a time.
mutate() {  # name, target file (secret-scan.py or install-hook.sh), old text, new text
  local name="$1" target="$2" old="$3" new="$4" dir="$SCRATCH/mut-$1"
  mkdir -p "$dir"
  cp "$HERE/secret-scan.py" "$HERE/install-hook.sh" "$dir/"
  python3 - "$HERE/$target" "$dir/$target" "$old" "$new" <<'PY' || { echo "mutation $name: could not apply (guard text changed?)" > "$dir.out"; return; }
import sys
src, dst, old, new = sys.argv[1:5]
s = open(src).read()
if old not in s:
    sys.exit(1)
open(dst, "w").write(s.replace(old, new, 1))
PY
  if SECRET_SCAN="$dir/secret-scan.py" INSTALL_HOOK="$dir/install-hook.sh" python3 -W ignore "$HERE/test_secret_scan.py" >/dev/null 2>&1; then
    case " $EQUIVALENT " in
      *" $name "*) echo "mutation $name: equivalent (documented, see EQUIVALENT above)" > "$dir.out" ;;
      *) echo "mutation $name: SURVIVED (tests did not notice)" > "$dir.out" ;;
    esac
  else
    echo "mutation $name: killed" > "$dir.out"
  fi
}
n=0
run() { mutate "$1" secret-scan.py "${@:2}" & n=$((n+1)); [ $((n % 4)) -eq 0 ] && wait; }
runh() { mutate "$1" install-hook.sh "${@:2}" & n=$((n+1)); [ $((n % 4)) -eq 0 ] && wait; }
run never-blocks          '    if not hits:
        return 0' '    if True:
        return 0'
run crash-fails-open      'commit with --no-verify.",
              file=sys.stderr)
        return 1' 'commit with --no-verify.",
              file=sys.stderr)
        return 0'
run identifier-swallows   'IDENTIFIER = re.compile(r"^(?:' 'IDENTIFIER = re.compile(r"(?:.*|'
run placeholder-ignored   'if PLACEHOLDER.match(v) or NOT_A_SECRET.match(v) or IDENTIFIER.match(v):' 'if False:'
run assignment-ignored    'for m in CRED_ASSIGN.finditer(text):' 'for m in []:'
run values-ignored        'if any(v in text for v in values):' 'if False:'
run value-files-ignored   'for f in paths_from_env("SECRET_SCAN_VALUE_FILES"):' 'for f in []:'
run env-roots-default     '[top] if top else []' '[]'
run public-name-blocked   'if secret_name.search(name) and not public_name.search(name):' 'if True:'
run prints-the-line       'hits.append(f"{path}:{n}: (rule: {rule})")' 'hits.append(f"{path}:{n}: (rule: {rule}) {text}")'
run no-hostname-filter    'and not (len(v) <= 253 and hostlike.match(v))' ''
run no-filename-filter    'and not filelike.search(v):' ':'
run secret-path-ignored   'if SECRET_PATH.search(p) and not SECRET_PATH_OK.search(p):' 'if False:'
run example-files-blocked 'if SECRET_PATH.search(p) and not SECRET_PATH_OK.search(p):' 'if SECRET_PATH.search(p):'
run key-files-ignored     '|id_rsa|id_ed25519|id_ecdsa)$")' ')$")'
run allow-everything      'if path in allow:' 'if True:'
run allow-nothing         'if path in allow:' 'if False:'
run line-numbers-lost     'n = int(m.group(1)) if m else 0' 'n = 0'
run twenty-cap            'for h in hits[:20]:' 'for h in hits:'
run anthropic-format      '("anthropic-key", re.compile(r"sk-ant-' '("anthropic-key", re.compile(r"sk-antX-'
run openai-format         '("openai-key", re.compile(r"\bsk-' '("openai-key", re.compile(r"\bskX-'
run github-format         'gh[pousr]_[A-Za-z0-9]{36,}|' 'ghX[pousr]_[A-Za-z0-9]{36,}|'
run github-pat-format     'github_pat_[A-Za-z0-9_]{40,}' 'github_patX_[A-Za-z0-9_]{40,}'
run aws-format            '(AKIA|ASIA)' '(AKIX|ASIX)'
run stripe-format         '[sr]k_live_' '[sr]k_liveX_'
run slack-format          'xox[abprs]-' 'xoxX[abprs]-'
run google-format         'AIza[0-9A-Za-z_-]{35}' 'AIzX[0-9A-Za-z_-]{35}'
run private-key-format    '-----BEGIN [A-Z ]*PRIVATE KEY-----' '-----BEGIN [A-Z ]*PRIVX KEY-----'
run jwt-format            '("jwt", re.compile(r"\beyJ' '("jwt", re.compile(r"\beyX'
run git-error-ignored     '    if r.returncode != 0:' '    if False:'
run raw-nul-dropped       '"diff", "--cached", "--raw", "-z", "--no-abbrev"' '"diff", "--cached", "--raw", "--no-abbrev"'
run gitlink-read          'if new_mode == "160000":' 'if False:'
run nul-not-binary        '    if b"\0" in blob:' '    if False:'
run raw-view-dropped      'for text in (blob.decode("latin-1"), blob.replace(b"\0", b"").decode("latin-1")):' 'for text in (blob.replace(b"\0", b"").decode("latin-1"),):'
run stripped-view-dropped 'for text in (blob.decode("latin-1"), blob.replace(b"\0", b"").decode("latin-1")):' 'for text in (blob.decode("latin-1"),):'
run flags-prefix          '"--src-prefix=a/", "--dst-prefix=b/")' ')'
run flags-textconv        '"--no-ext-diff", "--no-textconv",' '"--no-ext-diff",'
run flags-extdiff         '"--no-color", "--no-ext-diff",' '"--no-color",'
run flags-text            '"diff", "--text", "--unified=0"' '"diff", "--unified=0"'
run text-diff-ignored     'diff = git_raw("diff", "--text"' 'diff = b"" if True else git_raw("diff", "--text"'
run type-change-dropped   '--diff-filter=ACMRT' '--diff-filter=ACMR'
run chain-prefix-unbounded '((?:[A-Za-z0-9]{1,40}[_-]){0,8})' '((?:[A-Za-z0-9]+[_-])*)'
run chain-suffix-unbounded '((?:[_-][A-Za-z0-9]{1,40}){0,8})' '((?:[_-][A-Za-z0-9]+)*)'
run camel-tail-unbounded  '[A-Za-z0-9]{0,40}" + _VAL' '[A-Za-z0-9]*" + _VAL'
run jwt-header-unbounded  '{10,1000}' '{10,}'
run env-variants-dropped  '(\.env([.-][\w.-]*)?|' '(\.env(\.[\w-]+)?|'
run name-dot-env-dropped  '|[\w.-]+\.env|' '|'
run envrc-dropped         '|\.envrc([.-][\w.-]*)?|' '|'
run dev-vars-suffix-dropped '\.dev\.vars(\.[\w.-]+)?"' '\.dev\.vars"'
run template-exemption-narrowed 'SECRET_PATH_OK = re.compile(r"\.(example|sample|template)$")' 'SECRET_PATH_OK = re.compile(r"\.env\.(example|sample|template)$")'
run new-file-skipped      '    if not old_sha:' '    if False:'
run suffix-dropped        '((?:[_-][A-Za-z0-9]{1,40}){0,8})(?![A-Za-z0-9])" + _VAL' '()(?![A-Za-z0-9])" + _VAL'
run prefix-dropped        '((?:[A-Za-z0-9]{1,40}[_-]){0,8})" + _KW' '()" + _KW'
run camel-ignored         'for m in CRED_CAMEL.finditer(text):' 'for m in []:'
run compound-as-bare      '        if m.group(1) or m.group(2):' '        if False:'
run compound-digit-free   'if re.search(r"[A-Za-z]", v) and re.search(r"\d", v) and not FILE_VALUE.search(v):
                return' 'if re.search(r"[A-Za-z]", v) and not FILE_VALUE.search(v):
                return'
run compound-file-value   'and re.search(r"\d", v) and not FILE_VALUE.search(v):
                return' 'and re.search(r"\d", v):
                return'
run camel-digit-free      'if re.search(r"[A-Za-z]", v) and re.search(r"\d", v) and not FILE_VALUE.search(v):
            return' 'if re.search(r"[A-Za-z]", v) and not FILE_VALUE.search(v):
            return'
run camel-file-value      '        if re.search(r"[A-Za-z]", v) and re.search(r"\d", v) and not FILE_VALUE.search(v):
            return' '        if re.search(r"[A-Za-z]", v) and re.search(r"\d", v):
            return'
run npm-format            '("npm-token", re.compile(r"\bnpm_' '("npm-token", re.compile(r"\bnpmX_'
run sendgrid-format       '("sendgrid-key", re.compile(r"\bSG\.' '("sendgrid-key", re.compile(r"\bSGX\.'
run path-check-after-allow 'for p, _, _ in changes:' 'for p, _, _ in [c for c in changes if c[0] not in paths_from_env("SECRET_SCAN_ALLOW_PATHS")]:'
runh hook-mention-counts   'if wired; then' 'if grep -qF "secret-scan.py" "$HOOK"; then'
runh hook-exit-above-ok    '/^[ \t]*exit([ \t]|$)/{exit} ' ''
runh hook-symlink-resolved 'HOOK="$(git -C "$REPO" rev-parse --path-format=absolute --git-path hooks)/pre-commit"' 'HOOK="$(git -C "$REPO" rev-parse --path-format=absolute --git-path hooks/pre-commit)"'
runh hook-dangling-written 'if [ -e "$HOOK" ] || [ -L "$HOOK" ]; then' 'if [ -e "$HOOK" ]; then'
runh hook-never-wired      'if wired; then echo' 'if false; then echo'
wait
rc=0
cat "$SCRATCH"/mut-*.out | sort
cat "$SCRATCH"/mut-*.out | grep -q -E "SURVIVED|could not apply" && rc=1
[ $rc -eq 0 ] && echo "secret-scan-precommit: mutation check PASS" || echo "secret-scan-precommit: mutation check FAIL"
exit $rc
