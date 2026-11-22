#!/usr/bin/env python3
"""Git pre-commit hook: refuse a commit that would add a secret.

Why: a repo that lets an assistant run `git add` and `git commit` on its own has no human
looking at the diff, and a pasted key reaches the remote before anyone notices. This hook is the
check that does not depend on anyone's attention.

It scans only the lines ADDED by the staged change, so an old hit never blocks unrelated work.
Three checks:
  1. a staged path that is itself a secrets file (.env and its variants such as .env.local and
     .env.production.local, prod.env, .envrc, .dev.vars, a private key)
     (.env.example, .env.sample and .env.template are fine)
  2. a well-known credential format (Anthropic, OpenAI, GitHub, AWS, Stripe, Slack, Google keys,
     private-key blocks, JWTs) or `password = <literal>` style assignment
  3. an exact value that lives in a secret store you point it at (below)

It NEVER prints a matched value: only file, line number and which rule fired.
It fails CLOSED: if the scan itself crashes, the commit is blocked and the crash is named.

Where it looks for exact values (all optional):
  SECRET_SCAN_VALUE_FILES  colon-separated files (a notes file with logins, say). Every token in
                           them that looks like a key is remembered and blocked if it appears in
                           a staged line.
  SECRET_SCAN_ENV_ROOTS    colon-separated folders. Their .env, .env.*, .dev.vars files, and the
                           same one folder down, are read; values of entries whose NAME says
                           secret (KEY, TOKEN, SECRET, PASS ...) are blocked.
                           Default: the top of the repo being committed.
  SECRET_SCAN_ALLOW_PATHS  colon-separated repo-relative paths that hold made-up secret-shaped
                           fixtures on purpose (a scanner's own tests). Exempt from checks 2 and 3.
                           Nothing is exempt by default.

Install into one repo: bash install-hook.sh /path/to/repo    (never overwrites a hook)
Bypass: `git commit --no-verify` skips every hook. Do not use it on a secret; fix the line.
"""
import os
import re
import subprocess
import sys
from pathlib import Path

# A secrets file by name: .env and any dot/dash variant (.env.local, .env.production.local,
# .env.local.bak-1790026563), name.env (prod.env, secrets.env), .envrc, .dev.vars and
# .dev.vars.<anything>, and private key files. A trailing .example/.sample/.template marks the
# committed template and is allowed.
SECRET_PATH = re.compile(
    r"(^|/)(\.env([.-][\w.-]*)?|[\w.-]+\.env|\.envrc([.-][\w.-]*)?|\.dev\.vars(\.[\w.-]+)?"
    r"|id_rsa|id_ed25519|id_ecdsa)$")
SECRET_PATH_OK = re.compile(r"\.(example|sample|template)$")

FORMATS = [
    ("anthropic-key", re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}")),
    ("openai-key", re.compile(r"\bsk-(proj-)?[A-Za-z0-9]{32,}")),
    ("github-token", re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{40,})")),
    ("aws-access-key", re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("stripe-key", re.compile(r"\b[sr]k_live_[A-Za-z0-9]{20,}")),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,1000}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("npm-token", re.compile(r"\bnpm_[A-Za-z0-9]{36,}")),
    ("sendgrid-key", re.compile(r"\bSG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}")),
]
# A keyword may carry a prefix and a suffix joined by _ or - (DB_PASSWORD, client_secret,
# OPENAI_API_KEY, secret_key_base), so the boundary is "not a letter or digit" (a "_" does not
# count as one). camelCase names (clientSecret, apiKey) are caught by CRED_CAMEL.
_KW = r"(?:passcode|password|passphrase|passwd|secret|token|api[_-]?key)"
_VAL = r"[\s:=(`\"']{1,4}([A-Za-z0-9][A-Za-z0-9._-]{9,60})"
# Every repeat is bounded (at most 8 name parts of at most 40 characters). An unbounded chain
# (`(?:\w+-)*`) is quadratic on one long dash-joined line, which would hang the commit.
CRED_ASSIGN = re.compile(
    r"(?i)(?<![A-Za-z0-9])((?:[A-Za-z0-9]{1,40}[_-]){0,8})" + _KW
    + r"((?:[_-][A-Za-z0-9]{1,40}){0,8})(?![A-Za-z0-9])" + _VAL)
CRED_CAMEL = re.compile(
    r"(?<=[a-z0-9])(?:Pass(?:code|word|phrase)|Secret|Token|ApiKey|APIKey)[A-Za-z0-9]{0,40}" + _VAL)
PLACEHOLDER = re.compile(
    r"(?i)^(x{3,}|change|your|example|sample|test|fake|redacted|none|<|"
    r"process\.env\.|env\.|os\.environ|\$\{|\$[A-Z_])")
NOT_A_SECRET = re.compile(r"^(?:\d{4}-\d{2}-\d{2}|v?\d+\.\d+[\w.-]*)$")
# An environment-variable NAME after "token" or "secret" (SERVICE_API_TOKEN) is not a value.
IDENTIFIER = re.compile(r"^(?:[A-Z][A-Z0-9_]+|[A-Z][A-Z-]+)$")
# A file name after a compound name (token_cache = "token_v2_final.json") is not a secret.
FILE_VALUE = re.compile(r"(?i)\.(md|json|jsonl|sh|py|mjs|js|ts|html|txt|log|csv|png|jpg|pdf|toml|ya?ml|cfg|conf|ini)$")


def paths_from_env(name, default=()):
    raw = os.environ.get(name)
    if raw is None:
        return list(default)
    return [p for p in raw.split(":") if p]


class GitError(RuntimeError):
    pass


def git_raw(*args):
    """Bytes of stdout. ANY non-zero exit raises: a git that cannot answer must never read as
    "nothing to scan"."""
    r = subprocess.run(["git", *args], capture_output=True)
    if r.returncode != 0:
        raise GitError(f"git {args[0]} exited {r.returncode}")
    return r.stdout


def git(*args):
    return git_raw(*args).decode("utf-8", "replace")


def known_values():
    """Exact secret values from the stores named in the environment. A set; never printed."""
    vals = set()
    token = re.compile(r"[A-Za-z0-9_\-+/=.]{16,}")
    # Hostnames, emails and file names sit next to secrets in a notes file but are not secrets.
    # Unambiguous on purpose (dot-separated labels, no overlapping groups) so a long value cannot
    # make the match take seconds.
    hostlike = re.compile(r"(?i)^(?:[\w.+-]+@)?[\w-]+(?:\.[\w-]+)*\.(?:com|dev|net|org|io|ai|app|co|sh|us)$")
    filelike = re.compile(r"(?i)\.(md|json|jsonl|sh|py|mjs|js|ts|html|txt|log|csv|png|jpg|pdf|toml|ya?ml)$")
    secret_name = re.compile(r"(?i)(KEY|TOKEN|SECRET|PASS|PRIVATE|AUTH|CRED|SALT|HMAC|SIGNING)")
    public_name = re.compile(r"(?i)(PUBLIC|SITE_?KEY|_URL$|_HOST$|DOMAIN|_ID$)")

    def keep(v):
        v = v.strip().strip("\"'`")
        # Must look like a key, not a word, path, URL, date or hostname: letters AND digits.
        if len(v) >= 16 and re.search(r"[A-Za-z]", v) and re.search(r"\d", v) \
                and not v.startswith(("http", "/", "~")) and not NOT_A_SECRET.match(v) \
                and not (len(v) <= 253 and hostlike.match(v)) and not filelike.search(v):
            vals.add(v)

    for f in paths_from_env("SECRET_SCAN_VALUE_FILES"):
        try:
            for t in token.findall(Path(f).read_text(errors="ignore")):
                keep(t)
        except OSError:
            pass
    top = git("rev-parse", "--show-toplevel").strip()
    for root in map(Path, paths_from_env("SECRET_SCAN_ENV_ROOTS", [top] if top else [])):
        if not root.is_dir():
            continue
        for pattern in (".env", ".env.*", ".env-*", "*.env", ".envrc", ".dev.vars", ".dev.vars.*",
                        "*/.env", "*/.env.*", "*/.env-*", "*/*.env", "*/.envrc", "*/.dev.vars", "*/.dev.vars.*"):
            for f in root.glob(pattern):
                if SECRET_PATH_OK.search(f.name) or not f.is_file():
                    continue
                try:
                    for line in f.read_text(errors="ignore").splitlines():
                        if "=" in line and not line.lstrip().startswith("#"):
                            name, val = line.split("=", 1)
                            name = name.replace("export ", "").strip()
                            if secret_name.search(name) and not public_name.search(name):
                                keep(val)
                except OSError:
                    pass
    return vals


ZERO_SHA = re.compile(r"^0+$")
# Fixed on the command line so no user diff setting (noprefix, mnemonicPrefix, textconv, an
# external diff tool, colour) can change what the output looks like.
DIFF_FLAGS = ("--no-color", "--no-ext-diff", "--no-textconv", "--src-prefix=a/", "--dst-prefix=b/")


def staged_changes():
    """[(path, new_sha, old_sha)] for every added/copied/modified/renamed/type-changed staged
    file, from NUL-separated raw output (no quoting, no prefix, no dependence on diff config).
    Submodule entries have no blob to read and are skipped."""
    out = git_raw("diff", "--cached", "--raw", "-z", "--no-abbrev", "-M", "--diff-filter=ACMRT",
                  *DIFF_FLAGS).split(b"\0")
    changes, i = [], 0
    while i < len(out):
        meta = out[i]
        if not meta.startswith(b":"):
            i += 1
            continue
        fields = meta[1:].split()
        old_mode, new_mode, old_sha, new_sha, status = (f.decode() for f in fields[:5])
        npaths = 2 if status[0] in "RC" else 1
        paths = out[i + 1:i + 1 + npaths]
        if len(paths) != npaths:
            raise GitError("diff output ended early")
        i += 1 + npaths
        if new_mode == "160000":
            continue
        changes.append((paths[-1].decode("utf-8", "replace"), new_sha,
                        "" if ZERO_SHA.match(old_sha) or old_mode == "160000" else old_sha))
    return changes


def lines_of(path, blob, old_sha, new_sha):
    """Yield (path, line_no, text) for the lines this change adds, read from the staged blob."""
    if b"\0" in blob:
        # Looks binary (UTF-16 and UTF-32 text always has NULs): git would print "Binary files
        # differ" and no lines, which is how a secret hides. Scan the WHOLE content instead, as
        # raw bytes and again with the NULs removed (which turns UTF-16/32 back into ASCII).
        # Line numbers are those of the decoded text.
        for text in (blob.decode("latin-1"), blob.replace(b"\0", b"").decode("latin-1")):
            for n, line in enumerate(text.split("\n"), 1):
                yield path, n, line.rstrip("\r")
        return
    if not old_sha:
        for n, line in enumerate(blob.split(b"\n"), 1):
            yield path, n, line.decode("utf-8", "replace").rstrip("\r")
        return
    # Blob-to-blob diff: no path, so no attribute or textconv can apply; --text forces lines.
    # Header lines ("+++ b/<sha>") before the first hunk can only hold a sha, never a secret.
    diff = git_raw("diff", "--text", "--unified=0", *DIFF_FLAGS, old_sha, new_sha)
    n = 0
    for line in diff.split(b"\n"):
        if line.startswith(b"@@"):
            m = re.search(rb"\+(\d+)", line)
            n = int(m.group(1)) if m else 0
        elif line.startswith(b"+"):
            yield path, n, line[1:].decode("utf-8", "replace").rstrip("\r")
            n += 1


def credential_rule(text):
    for m in CRED_ASSIGN.finditer(text):
        v = m.group(3)
        if PLACEHOLDER.match(v) or NOT_A_SECRET.match(v) or IDENTIFIER.match(v):
            continue
        if m.group(1) or m.group(2):
            # A compound name (DB_PASSWORD, secret_key_base) is far more often a setting NAME or a
            # word than a value, so the value must look like a secret: letters AND digits.
            if re.search(r"[A-Za-z]", v) and re.search(r"\d", v) and not FILE_VALUE.search(v):
                return "credential-assignment"
        elif any(c.isdigit() for c in v) or (v.count("-") + v.count("_")) >= 2:
            return "credential-assignment"
    for m in CRED_CAMEL.finditer(text):
        v = m.group(1)
        if PLACEHOLDER.match(v) or NOT_A_SECRET.match(v) or IDENTIFIER.match(v):
            continue
        if re.search(r"[A-Za-z]", v) and re.search(r"\d", v) and not FILE_VALUE.search(v):
            return "credential-assignment"
    return None


def scan():
    hits = []
    changes = staged_changes()
    for p, _, _ in changes:
        if SECRET_PATH.search(p) and not SECRET_PATH_OK.search(p):
            hits.append(f"{p}: secrets file staged (rule: secret-file-path)")
    allow = set(paths_from_env("SECRET_SCAN_ALLOW_PATHS"))
    values = None
    seen = set()
    for path, new_sha, old_sha in changes:
        if path in allow:
            continue
        blob = git_raw("cat-file", "blob", new_sha)
        for _, n, text in lines_of(path, blob, old_sha, new_sha):
            rule = next((name for name, rx in FORMATS if rx.search(text)), None)
            if rule is None:
                rule = credential_rule(text)
            if rule is None:
                if values is None:
                    values = known_values()
                if any(v in text for v in values):
                    rule = "value-from-secret-store"
            if rule and (path, n, rule) not in seen:
                seen.add((path, n, rule))
                hits.append(f"{path}:{n}: (rule: {rule})")
    return hits


def main():
    try:
        hits = scan()
    except Exception as e:      # fail closed: a scan that cannot run must not wave a commit through
        print(f"BLOCKED by secret-scan: the scan itself failed ({type(e).__name__}); "
              f"nothing was checked. Fix the cause or, if you are sure, commit with --no-verify.",
              file=sys.stderr)
        return 1
    if not hits:
        return 0
    print("BLOCKED by secret-scan pre-commit hook: the staged change adds what looks like a secret. "
          "Values are not shown.", file=sys.stderr)
    for h in hits[:20]:
        print("  " + h, file=sys.stderr)
    if len(hits) > 20:
        print(f"  ... and {len(hits) - 20} more", file=sys.stderr)
    print("Fix: remove the value (read it from an environment variable or a secret store instead), "
          "`git add` again, commit again. If it is a made-up fixture, list its path in "
          "SECRET_SCAN_ALLOW_PATHS.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
