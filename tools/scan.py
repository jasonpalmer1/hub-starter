#!/usr/bin/env python3
"""Generic leak scanner for a repo that is about to be shared.

Usage:
  scan.py --root DIR [--manifest FILE] [--denylist FILE]... [--dynamic] [--no-git] [--history-allow FILE] [--identity-allow FILE]
          [--allow-term WORD]... [--base COMMIT | --base-file FILE]
  (--base: judge only commits and blobs after COMMIT, every rule, allow files ignored; a bad COMMIT exits 2.
   --base-file: same from a pin file; a missing or bad pin scans ALL history. Neither flag: all history.)
  scan.py --guard-identity   (run inside a repo: refuse unless git would commit as the kit identity)

Exit 0 = clean, 1 = violations, 2 = usage / unreadable input (fails closed).
Output is one line per finding: FILE:LINE category. The matched text is never
printed, so the report itself cannot leak.

What it checks (fails closed):
  manifest   every file physically in the tree must be listed in the manifest,
             every manifest entry must exist; walks ignored files too
  ext        only allowlisted extensions may ship (so .pyc, .lock, .log, .env,
             .bak, binaries are refused); they are still content-scanned
  symlink    symlinks are refused
  path-leak  absolute home paths and Claude project slugs
  infra-id   32-hex ids, UUIDs, hosting hostnames, build-tool names
  secret     credential shapes
  email      any email address that is not a placeholder or a noreply address
  provenance memory-file names, wiki links, ticket ids above the fixture range
  denylist   private terms (from --denylist files and --dynamic machine facts),
             matched after stripping case, punctuation and unicode tricks,
             across string concatenation and line breaks
  identity   git author or committer name/email, and commit messages
  history    every distinct blob reachable from any ref (so a leak that was committed and then deleted
             still fails) gets the same content rules; commit messages get them too (ticket ids excepted)
"""
import functools
import os
import re
import socket
import subprocess
import sys
import unicodedata

ALLOWED_EXT = (".md", ".py", ".sh", ".json", ".mjs", ".txt", ".plist.example", ".task.xml")
ALLOWED_NAMES = {"LICENSE", "VERSION", ".gitignore", ".gitattributes"}
# Extensionless command-line scripts that ship, by exact path (content is still scanned).
ALLOWED_SCRIPT_PATHS = {"patterns/ledger/ledger", "tools/githooks/pre-commit", "tools/githooks/pre-merge-commit"}
MAX_FIXTURE_TICKET = 10

# Patterns are assembled from pieces so this file does not trip itself.
LINE_RULES = [
    ("path-leak", re.compile("/Us" + "ers/[A-Za-z0-9_.-]+")),
    ("path-leak", re.compile("-Us" + "ers-[A-Za-z0-9]")),
    ("path-leak", re.compile("/ho" + "me/[a-z][a-z0-9_-]+/")),
    ("path-leak", re.compile(r"[A-Za-z]:\\Us" + r"ers\\")),
    ("infra-id", re.compile(r"(?<![0-9A-Za-z])[0-9a-f]{32}(?![0-9A-Za-z])")),
    ("infra-id", re.compile(
        r"(?<![0-9A-Za-z])[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?![0-9A-Za-z])", re.I)),
    ("infra-id", re.compile(r"\.pages" + r"\.dev|\.workers" + r"\.dev", re.I)),
    ("secret", re.compile("sk-" + r"ant-[A-Za-z0-9_-]{16,}")),
    ("secret", re.compile("sk-" + r"[A-Za-z0-9]{32,}")),
    ("secret", re.compile("gh[pousr]" + r"_[A-Za-z0-9]{30,}")),
    ("secret", re.compile("github_" + r"pat_[A-Za-z0-9_]{30,}")),
    ("secret", re.compile("AK" + r"IA[0-9A-Z]{16}")),
    ("secret", re.compile("xox" + r"[abprs]-[A-Za-z0-9-]{10,}")),
    ("secret", re.compile("-----BEGIN [A-Z ]*PRI" + "VATE KEY")),
    ("secret", re.compile(
        r"(?i)(api[_-]?key|secret|token|passw(or)?d)[\"']?\s*[:=]\s*[\"'][A-Za-z0-9_/+=-]{16,}[\"']")),
    ("secret", re.compile(r"(?i)\bbea" + r"rer\s+[A-Za-z0-9._~+/=-]{16,}")),
    ("secret", re.compile(r"\bey" + r"J[A-Za-z0-9_-]{8,}\.ey" + r"J[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{5,}")),
    ("secret", re.compile(r"hooks\.sl" + r"ack\.com/(services|workflows|triggers)/[A-Za-z0-9/_-]{8,}")),
    ("secret", re.compile(r"(?i)\b(passw(or)?d|passwd|secret|api[_-]?key|token)\b[\"']?\s*[:=]\s*[A-Za-z0-9_/+=-]{20,}(?![A-Za-z0-9_/+=(\[.-])")),
    ("infra-id", re.compile(r"(?<![0-9.])(?:10\.\d{1,3}|192\.168|172\.(?:1[6-9]|2\d|3[01])|100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7]))\.\d{1,3}\.\d{1,3}(?![0-9])")),
    ("infra-id", re.compile(r"\.ts" + r"\.net\b", re.I)),
    ("provenance", re.compile("feed" + r"back_[a-z0-9]")),
    ("provenance", re.compile("\\x5b\\x5b[^\\x5d\\n]+\\x5d\\x5d")),
]
TICKET = re.compile(r"(?<![A-Za-z0-9])L[-_ ](\d{4,})(?![A-Za-z0-9])")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)")
OK_EMAIL_DOMAINS = ("example.com", "example.org", "example.net", "users.noreply.github.com",
                    "noreply.github.com", "localhost", "anthropic.com")
OK_EMAIL_LOCALS = ("noreply", "no-reply")
# The only identities a commit in this repo may carry. A real name, or a noreply address that embeds an account
# id or handle (12345+handle@users.noreply.github.com), is personal material even though the domain is allowed.
OK_IDENTITY_NAMES = ("hub-starter", "github", "claude")
OK_IDENTITY_LOCALS = ("hub-starter", "noreply", "no-reply")

CONFUSABLES = str.maketrans({
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "х": "x", "у": "y", "і": "i", "ј": "j",
    "ѕ": "s", "ԁ": "d", "ɡ": "g", "ο": "o", "α": "a", "ε": "e", "ι": "i", "ν": "v", "ρ": "p",
    "τ": "t", "υ": "u", "κ": "k", "ı": "i", "ℓ": "l", "ǀ": "l",
})
GENERIC_DYNAMIC = {"hub", "ledger", "projects", "project", "home", "user", "users", "admin", "tests",
                   "tools", "docs", "build", "builds", "test", "agents", "hooks", "root", "local",
                   "shared", "public", "private", "client", "server", "common", "claude", "github",
                   "documents", "desktop", "downloads", "library", "applications", "workspace",
                   "default", "starter", "kit", "scripts", "source", "stuff", "scratch", "backup"}


def fold(s):
    """casefold, drop accents, map lookalike letters to ascii, keep a-z0-9 only"""
    s = unicodedata.normalize("NFKD", s).translate(CONFUSABLES).casefold()
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if c.isascii() and c.isalnum())


def fold_loose(s):
    """same folding but keeps separators as single spaces (for word-boundary terms)"""
    s = unicodedata.normalize("NFKD", s).translate(CONFUSABLES).casefold()
    s = unicodedata.normalize("NFKD", s)
    return "".join(c if (c.isascii() and c.isalnum()) else " " for c in s)


# Plain English / code words. A project folder named like one of these must not match inside
# ordinary code (a regex call, a helper called extract_text), so such terms are matched as a
# whole word with no separators allowed between the letters. Names and brands keep the loose
# match. Stored reversed so this list never matches itself when a folder has such a name.
_PLAIN_REV = "sisylana evihcra sevihcra pukcab eldnub ehcac gifnoc draobhsad yolped ngised stfard elpmaxe selpmaxe tropxe tcartxe tropmi tupni stupni tekram stekram rotinom srotinom seton tuptuo stuptuo esrap resrap enilepip senilepip koobyalp redaer ecnerefer esaeler sesaeler troper stroper hcraeser weiver rennur elpmas selpmas reparcs tpircs hcraes ecivres secivres sgnittes ygetarts etalpmet setalpmet gnidart rekrow srekrow retirw"
PLAIN_WORDS = {w[::-1] for w in _PLAIN_REV.split()}


@functools.lru_cache(maxsize=None)
def _system_words():
    """the system dictionary, read once per process (is_plain_word is called for every project folder)"""
    words = set()
    try:
        with open("/usr/share/dict/words", encoding="utf-8", errors="ignore") as fh:
            for w in fh:
                w = w.strip()
                if w.isascii() and w.isalpha() and w.islower() and len(w) >= 5:
                    words.add(w)
    except OSError:
        pass
    return frozenset(words)


def is_plain_word(text):
    t = text.strip()
    if not (t.isascii() and t.isalpha() and t.islower()):
        return False
    return t in PLAIN_WORDS or t in _system_words()


def fold_strict(s):
    """like fold_loose but underscore stays a word character, so extract_text is one token"""
    s = unicodedata.normalize("NFKD", s).translate(CONFUSABLES).casefold()
    s = unicodedata.normalize("NFKD", s)
    return "".join(c if (c.isascii() and (c.isalnum() or c == "_")) else " " for c in s)


def fold_ident(s):
    """like fold_loose but - _ . stay, so a hyphenated folder name is one token and plain prose spacing is not.
    A camelCase hump becomes | so myFooBar still has a word edge before Foo and still joins Foo to Bar.
    Invisible characters (zero-width, soft hyphen, other format or combining marks) are dropped and every
    typographic dash becomes a plain hyphen, so pasted text cannot split a name."""
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "|", s)
    s = unicodedata.normalize("NFKD", s).translate(CONFUSABLES).casefold()
    s = unicodedata.normalize("NFKD", s)
    out = []
    for c in s:
        cat = unicodedata.category(c)
        if cat in ("Cf", "Mn", "Me") or c == "\u00ad":
            continue
        if cat == "Pd" or c == "\u2212":
            c = "-"
        out.append(c if (c.isascii() and (c.isalnum() or c in "-_.|")) else " ")
    return "".join(out)


def is_plain_compound(text):
    """a lowercase folder name like a-b whose parts include a plain word and whose other parts are all
    3 characters or fewer: it has no distinctive part, so it reads as ordinary words. Only that shape is
    matched as one identifier (separators - _ . or none), never across spaces. Any name with a longer
    non-plain part keeps the loose match."""
    t = text.strip()
    if not (t.isascii() and t.islower() and re.fullmatch(r"[a-z0-9]+(?:[-_.][a-z0-9]+)+", t)):
        return False
    parts = re.split(r"[-_.]", t)
    if not any(is_plain_word(p) for p in parts):
        return False
    return all(is_plain_word(p) or len(p) <= 3 for p in parts)


class Term:
    def __init__(self, text, word, strict=False, ident=False):
        self.key = fold(text)
        self.word = word
        self.strict = strict
        self.ident = ident
        if ident:
            chars = [re.escape(c) for c in self.key]
            self.rx = re.compile(r"(?<![a-z0-9])" + r"[-_.|]*".join(chars) + r"(?![a-z0-9])")
        elif strict:
            self.rx = re.compile(r"(?<![a-z0-9_])" + re.escape(self.key) + r"(?![a-z0-9_])")
        elif word:
            chars = [re.escape(c) for c in self.key]
            self.rx = re.compile(r"(?<![a-z0-9])" + r"[^a-z0-9]*".join(chars) + r"(?![a-z0-9])")


def load_terms(paths):
    terms = []
    for p in paths:
        try:
            with open(p, encoding="utf-8") as fh:
                lines = fh.read().splitlines()
        except OSError:
            print("scan: cannot read denylist (fails closed)", file=sys.stderr)
            sys.exit(2)
        for ln in lines:
            ln = ln.strip()
            if not ln or ln.startswith("#"):
                continue
            word = ln.startswith("w:")
            t = Term(ln[2:] if word else ln, word)
            if len(t.key) >= 3:
                terms.append(t)
    return terms


def run(cmd, cwd=None):
    try:
        return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=15).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def dynamic_terms(root):
    """machine facts: who and where this is being scanned from"""
    vals = set()
    for v in (os.environ.get("USER"), os.environ.get("LOGNAME"), os.path.basename(os.path.expanduser("~")),
              socket.gethostname().split(".")[0]):
        if v:
            vals.add(v)
    for key in ("user.name", "user.email"):
        v = run(["git", "config", "--global", key]).strip()
        if v:
            vals.add(v)
            if key == "user.email":
                vals.add(v.split("@")[0])
    proj = os.path.expanduser("~/projects")
    projnames = set(os.listdir(proj)) if os.path.isdir(proj) else set()
    vals.update(projnames)
    own = fold(os.path.basename(os.path.abspath(root)))
    out = []
    for v in vals:
        k = fold(v)
        if len(k) >= 5 and k not in GENERIC_DYNAMIC and k != own and not own.startswith(k):
            out.append(Term(v, True, strict=(v in projnames and is_plain_word(v)),
                            ident=(v in projnames and is_plain_compound(v))))
    return out


def term_hits_in_text(terms, text):
    """yield (line_no, ) for every denylist hit. Non-word terms are matched across
    string concatenation and line breaks by folding the whole text with a line map."""
    hits = set()
    folded, linemap = [], []
    for n, line in enumerate(text.split("\n"), 1):
        f = fold(line)
        folded.append(f)
        linemap.extend([n] * len(f))
        loose = fold_loose(line)
        strict = fold_strict(line)
        ident = fold_ident(line)
        for t in terms:
            if t.ident:
                if t.rx.search(ident):
                    hits.add(n)
            elif t.strict:
                if t.rx.search(strict):
                    hits.add(n)
            elif t.word and t.rx.search(loose):
                hits.add(n)
    blob = "".join(folded)
    for t in terms:
        if t.word or t.strict or t.ident:
            continue
        start = 0
        while True:
            i = blob.find(t.key, start)
            if i < 0:
                break
            hits.add(linemap[i])
            start = i + 1
    return sorted(hits)


def walk(root):
    out = []
    for dp, dn, fn in os.walk(root):
        dn[:] = [d for d in dn if d != ".git"]
        for f in fn:
            out.append(os.path.relpath(os.path.join(dp, f), root))
    return sorted(out)


def read_manifest(path):
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        print("scan: cannot read manifest (fails closed)", file=sys.stderr)
        sys.exit(2)
    ents = set()
    for ln in lines:
        ln = ln.strip()
        if ln and not ln.startswith("#"):
            ents.add(re.split(r"\t|\s{2,}", ln)[0].strip())
    return ents


def content_findings(text, tickets=True):
    """(line, category) for every rule hit in one text (a file, a history blob or a commit message)."""
    out = []
    for n, line in enumerate(text.split("\n"), 1):
        seen = set()
        for cat, rx in LINE_RULES:
            if cat not in seen and rx.search(line):
                seen.add(cat)
                out.append((n, cat))
        if tickets:
            for m in TICKET.finditer(line):
                if int(m.group(1)) > MAX_FIXTURE_TICKET:
                    out.append((n, "provenance"))
                    break
        for m in EMAIL.finditer(line):
            dom = m.group(1).lower()
            local = m.group(0).split("@")[0].lower()
            if not (dom in OK_EMAIL_DOMAINS or dom.endswith(".example.com") or local in OK_EMAIL_LOCALS):
                out.append((n, "email"))
                break
    return out


def scan(root, manifest=None, denylists=(), dynamic=False, check_git=True, history=True, history_allow=(), identity_allow=(), base=None):
    findings = []

    def add(rel, line, cat):
        findings.append((rel, line, cat))

    terms = load_terms(denylists)
    if dynamic:
        terms += dynamic_terms(root)

    files = walk(root)
    if manifest:
        ents = read_manifest(manifest)
        for rel in files:
            if rel not in ents:
                add(rel, 0, "manifest")
        for e in sorted(ents - set(files)):
            add(e, 0, "manifest")

    for rel in files:
        full = os.path.join(root, rel)
        name = os.path.basename(rel)
        if os.path.islink(full):
            add(rel, 0, "symlink")
            continue
        if not (name in ALLOWED_NAMES or rel in ALLOWED_SCRIPT_PATHS or rel.endswith(ALLOWED_EXT)):
            add(rel, 0, "ext")
        if name.startswith(".env") or name.endswith((".pyc", ".lock", ".log")) or ".bak" in name:
            add(rel, 0, "ext")
        if terms and term_hits_in_text(terms, rel):
            add(rel, 0, "denylist")
        for rx_name, rx in (("path-leak", LINE_RULES[0][1]),):
            if rx.search(rel):
                add(rel, 0, "path-leak")
        try:
            with open(full, "rb") as fh:
                raw = fh.read()
        except OSError:
            add(rel, 0, "unreadable")
            continue
        text = raw.decode("utf-8", errors="replace")
        if b"\x00" in raw:
            add(rel, 0, "binary")
        for n, cat in content_findings(text):
            add(rel, n, cat)
        if terms:
            for n in term_hits_in_text(terms, text):
                add(rel, n, "denylist")

    gitcfg = os.path.join(root, ".git", "config")
    if os.path.isfile(gitcfg) and terms:
        with open(gitcfg, encoding="utf-8", errors="replace") as fh:
            for n in term_hits_in_text(terms, fh.read()):
                add(".git/config", n, "denylist")

    if check_git:
        findings += git_checks(root, terms, history, history_allow, identity_allow, base)
    return sorted(set(findings))


def ident_bad(name, email):
    """True when a commit name/email pair is not the kit identity. An empty email is bad too."""
    name, email = (name or "").strip(), (email or "").strip()
    if not email or "@" not in email:
        return True
    dom = email.split("@")[-1].lower()
    local = email.split("@")[0].lower()
    if not (dom in OK_EMAIL_DOMAINS or dom.endswith(".example.com") or local in OK_EMAIL_LOCALS):
        return True
    if name.lower() not in OK_IDENTITY_NAMES:
        return True
    return local not in OK_IDENTITY_LOCALS and not email.lower().endswith("@example.com")


def guard_identity(cwd):
    """Pre-commit use: the author and committer git WOULD record right now must be the kit identity.
    Exit 0 ok, 1 refused. Never prints the name or email, only which role is wrong."""
    bad = []
    for role in ("GIT_AUTHOR_IDENT", "GIT_COMMITTER_IDENT"):
        v = run(["git", "var", role], cwd=cwd).strip()
        m = re.fullmatch(r"(.*?) <(.*)> \d+ [+-]\d{4}", v)
        if not m or ident_bad(m.group(1), m.group(2)):
            bad.append(role.split("_")[1].lower())
    if bad:
        print("identity-guard: REFUSED, " + " and ".join(bad) + " is not the kit identity. Run:\n"
              "  git config user.name hub-starter && git config user.email hub-starter@users.noreply.github.com")
        return 1
    return 0


HEX40 = re.compile(r"[0-9a-f]{40}")


def valid_base(root, base):
    """A pinned history baseline is usable only if it is one full 40-hex commit id that exists and is an
    ancestor of HEAD. Anything else is refused so the caller fails closed (full-history scan)."""
    if not isinstance(base, str) or not HEX40.fullmatch(base):
        return False
    try:
        t = subprocess.run(["git", "cat-file", "-t", base], cwd=root, capture_output=True, text=True, timeout=60)
        if t.returncode != 0 or t.stdout.strip() != "commit":
            return False
        a = subprocess.run(["git", "merge-base", "--is-ancestor", base, "HEAD"], cwd=root, capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return False
    return a.returncode == 0


def read_base_file(path):
    """tools/history-base.txt: '#' comment lines and blank lines are ignored; exactly one other line, a full
    40-hex commit id. Missing file, zero or two ids, short or non-hex ids -> None (caller scans everything)."""
    try:
        with open(path, encoding="utf-8") as fh:
            lines = [ln.strip() for ln in fh if ln.strip() and not ln.lstrip().startswith("#")]
    except (OSError, UnicodeDecodeError):
        return None
    if len(lines) != 1 or not HEX40.fullmatch(lines[0]):
        return None
    return lines[0]


def git_checks(root, terms, history=True, history_allow=(), identity_allow=(), base=None):
    """base=None: every commit and blob on every ref, with the allow files (full history).
    base=<commit>: only commits and blobs reachable from HEAD and NOT from base, every rule, and the allow files
    are ignored (a new commit gets no excuse)."""
    out = []
    seen = set()
    if not os.path.isdir(os.path.join(root, ".git")):
        return [(".git", 0, "no-git-repo")]
    if base:
        history_allow, identity_allow = (), ()
        rng = ["HEAD", "--not", base]
    else:
        rng = ["--all"]
    log = run(["git", "log", *rng, "--format=%H%x1f%an%x1f%ae%x1f%cn%x1f%ce%x1f%B%x1e"], cwd=root)
    for rec in log.split("\x1e"):
        rec = rec.strip("\n")
        if not rec:
            continue
        parts = rec.split("\x1f")
        if len(parts) < 6:
            continue
        sha = parts[0][:8]
        sha_full = parts[0].lower()
        seen.add(sha_full)
        ident = "\n".join(parts[1:5])
        if terms and term_hits_in_text(terms, ident):
            out.append((".git/commit-" + sha, 0, "identity"))
        if sha_full not in identity_allow:
            for nm, em in ((parts[1], parts[2]), (parts[3], parts[4])):
                if ident_bad(nm, em):
                    out.append((".git/commit-" + sha, 0, "identity"))
                    break
        if terms and term_hits_in_text(terms, parts[5]):
            out.append((".git/commit-" + sha, 0, "denylist"))
        if history:
            # a ticket id in a message is allowed (it cannot be edited out of finished history); every other rule applies
            for n, cat in content_findings(parts[5], tickets=False):
                out.append((".git/commit-" + sha, n, cat))
    for stale in sorted(set(identity_allow) - seen):
        out.append((".git/identity-allow-" + stale[:8], 0, "identity-allow-stale"))
    if history:
        out += history_blob_findings(root, terms, history_allow, rng)
    return out


def history_blob_findings(root, terms, history_allow=(), rng=("--all",)):
    """Scan every distinct blob reachable from any branch, tag or other ref, so a leak committed and later
    deleted still fails. Reported as history/<path>@<blob8>:LINE category (never the matched text)."""
    out = []
    listing = run(["git", "rev-list", *rng, "--objects"], cwd=root)
    paths = {}
    for ln in listing.split("\n"):
        sha, _, path = ln.partition(" ")
        if len(sha) == 40 and path and sha not in paths and sha not in history_allow:
            paths[sha] = path
    if not paths:
        return out
    try:      # one process for the whole batch: names the blobs, then returns each one's bytes
        proc = subprocess.run(["git", "cat-file", "--batch"], cwd=root, input="".join(k + "\n" for k in paths).encode(),
                              capture_output=True, timeout=300)
        data = proc.stdout
    except (OSError, subprocess.SubprocessError):
        return [("history", 0, "unreadable")]
    pos = 0
    while pos < len(data):
        eol = data.index(b"\n", pos)
        head = data[pos:eol].split()
        pos = eol + 1
        if len(head) != 3:                      # "<sha> missing" or malformed
            if len(head) == 2:
                out.append(("history/%s" % paths.get(head[0].decode(), "?"), 0, "unreadable"))
            continue
        sha, kind, size = head[0].decode(), head[1], int(head[2])
        raw = data[pos:pos + size]
        pos += size + 1
        if kind != b"blob":
            continue
        path = paths[sha]
        label = "history/%s@%s" % (path, sha[:8])
        if b"\x00" in raw:
            out.append((label, 0, "binary"))
        text = raw.decode("utf-8", errors="replace")
        if terms and term_hits_in_text(terms, path):
            out.append((label, 0, "denylist"))
        if LINE_RULES[0][1].search(path):
            out.append((label, 0, "path-leak"))
        for n, cat in content_findings(text):
            out.append((label, n, cat))
        if terms:
            for n in term_hits_in_text(terms, text):
                out.append((label, n, "denylist"))
    return out


def read_history_allow(path):
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        print("scan: cannot read history allowlist (fails closed)", file=sys.stderr)
        sys.exit(2)
    out = set()
    for ln in lines:
        ln = ln.strip()
        if ln and not ln.startswith("#"):
            tok = ln.split()[0].lower()
            if not re.fullmatch(r"[0-9a-f]{40}", tok):
                print("scan: bad history allowlist line (need a full blob id)", file=sys.stderr)
                sys.exit(2)
            out.add(tok)
    return out


def read_identity_allow(path):
    """Full 40-char COMMIT ids whose recorded identity was reviewed and accepted (history cannot be rewritten)."""
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        print("scan: cannot read identity allowlist (fails closed)", file=sys.stderr)
        sys.exit(2)
    out = set()
    for ln in lines:
        ln = ln.strip()
        if ln and not ln.startswith("#"):
            tok = ln.split()[0].lower()
            if not re.fullmatch(r"[0-9a-f]{40}", tok):
                print("scan: bad identity allowlist line (need a full commit id)", file=sys.stderr)
                sys.exit(2)
            out.add(tok)
    return out


def main(argv):
    if argv[:1] == ["--guard-identity"]:
        return guard_identity(os.getcwd())
    identity_allow = set()
    root = manifest = None
    history_allow = set()
    denylists, dynamic, check_git = [], False, True
    base = base_file = None
    explicit_base = False
    it = iter(argv)
    for a in it:
        if a == "--root":
            root = next(it, None)
        elif a == "--manifest":
            manifest = next(it, None)
        elif a == "--denylist":
            denylists.append(next(it, None))
        elif a == "--dynamic":
            dynamic = True
        elif a == "--history-allow":
            history_allow = read_history_allow(next(it, None) or "")
        elif a == "--identity-allow":
            identity_allow = read_identity_allow(next(it, None) or "")
        elif a == "--no-git":
            check_git = False
        elif a == "--base":
            base = next(it, None) or ""
            explicit_base = True
        elif a == "--base-file":
            base_file = next(it, None) or ""
        else:
            print(__doc__)
            return 2
    if not root or not os.path.isdir(root) or None in denylists:
        print(__doc__)
        return 2
    if base is None and base_file is not None:       # pin file: any problem falls back to the full-history scan
        base = read_base_file(base_file)
        if base is None or not valid_base(root, base):
            print("scan: history-base pin missing or invalid; scanning ALL history", file=sys.stderr)
            base = None
    elif explicit_base and not valid_base(root, base):
        print("scan: --base must be one full commit id that is an ancestor of HEAD", file=sys.stderr)
        return 2
    if base:
        print("scan: range mode, history after the pinned base only (use --full for all history)", file=sys.stderr)
    findings = scan(root, manifest, denylists, dynamic, check_git, True, history_allow, identity_allow, base)
    for rel, line, cat in findings:
        print("%s:%d %s" % (rel, line, cat))
    print("scan: %d finding(s)" % len(findings), file=sys.stderr)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
