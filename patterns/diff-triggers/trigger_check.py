#!/usr/bin/env python3
"""trigger_check.py <repo> <base> <tip> [--rows FILE] [--log FILE | --no-log]   (OBSERVE ONLY)

Reads the diff base...tip, loads the rows table, prints one `TRIGGER-OBSERVE:` line per row the diff matches
and a count line. Always exits 0 for a diff it could read; exit 2 only for a bad call or an unreadable repo/ref.
It never refuses, never writes a verdict or ticket row, never touches a gate counter.

Rows file: the table in your install root (default ~/.claude/hubkit/triggers/triggers.tsv, or $HUBKIT_HOME/triggers/),
never a copy from the diff (a branch cannot edit its own checklist before it lands).
Columns (TAB): id, trigger, check, source, replaces, mode.
Trigger = terms joined by ' & ' :
  changed:<glob>          a path in the diff (renames count both names) matches the glob
  unchanged:<glob>        no path matches
  added-path:<regex>      a newly ADDED path matches the regex
  added-line:<glob>       a '+' line was added in a matching path
  added-line-re:<pathre>~~<linere>   a '+' line matching linere was added in a path matching pathre
  none                    matches nothing (a note-only row)
The loader refuses the whole table when a row has an empty `replaces` (the literal `-` is allowed only with
trigger `none`): a new check must name the one it removes.
Also prints a RULES-EDIT note when the diff edits a rules file (the gate reads those from BASE via base_read.py).
"""
import fnmatch
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.realpath(__file__))
HOME_ROOT = os.path.realpath(os.environ.get("HUBKIT_HOME") or os.path.expanduser("~/.claude/hubkit"))
DEFAULT_ROWS = os.path.join(HOME_ROOT, "triggers", "triggers.tsv")
DEFAULT_LOG = os.path.join(HOME_ROOT, "state", "trigger-check", "observe.log")
RULE_FILES = ("CLAUDE.md", "REVIEW.md")
RULE_PATHS = ("scripts/pregate.sh",)


def die(msg, rc=2):
    print("trigger_check: " + msg, file=sys.stderr)
    sys.exit(rc)


def git(repo, *a):
    p = subprocess.run(["git", "-C", repo, "-c", "core.quotePath=false"] + list(a), capture_output=True)
    if p.returncode != 0:
        die("git %s failed: %s" % (a[0], p.stderr.decode("utf-8", "replace").strip()[:200]))
    return p.stdout


def load_rows(path):
    try:
        with open(path, encoding="utf-8") as fh:
            raw = fh.read()
    except OSError as e:
        die("rows file unreadable: %s (%s)" % (path, e))
    rows = []
    for n, line in enumerate(raw.splitlines(), 1):
        if not line.strip() or line.startswith("#"):
            continue
        c = line.split("\t")
        if len(c) != 6:
            die("rows file %s line %d: need 6 tab columns, got %d" % (path, n, len(c)))
        rid, trig, chk, src, rep, mode = [x.strip() for x in c]
        if rid == "id":
            continue
        if not rep or (rep == "-" and trig != "none"):
            die("rows file refused: row %r has an empty `replaces` (a check must name the one it removes)" % rid)
        if mode not in ("observe", "off"):
            die("rows file %s line %d: mode must be observe|off, got %r" % (path, n, mode))
        rows.append((rid, trig, chk, src, rep, mode))
    return rows


def diff_facts(repo, base, tip):
    mb = git(repo, "merge-base", base, tip).decode().strip()
    ns = git(repo, "diff", "--name-status", "-z", "-M", mb, tip).split(b"\0")
    changed, added = set(), set()
    i = 0
    while i < len(ns) and ns[i]:
        st = ns[i].decode("utf-8", "replace")
        i += 1
        k = 2 if st[0] in "RC" else 1
        names = [x.decode("utf-8", "replace") for x in ns[i:i + k]]
        i += k
        changed.update(names)
        if st[0] in "AC":
            added.add(names[-1])
    lines = {}  # path -> added lines
    cur = None
    inhunk = False
    for ln in git(repo, "diff", "-U0", "--no-color", "--text", "-M", mb, tip).decode("utf-8", "replace").splitlines():
        if ln.startswith("diff --git "):
            inhunk = False
        elif ln.startswith("@@"):
            inhunk = True
        if not inhunk and ln.startswith("+++ "):
            cur = ln[4:]
            cur = None if cur == "/dev/null" else (cur[2:] if cur.startswith("b/") else cur)
        elif inhunk and ln.startswith("+") and cur:
            lines.setdefault(cur, []).append(ln[1:])
    return changed, added, lines


def term_ok(t, changed, added, lines):
    t = t.strip()
    if t == "none":
        return False
    kind, _, arg = t.partition(":")
    if kind == "changed":
        return any(fnmatch.fnmatchcase(p, arg) for p in changed)
    if kind == "unchanged":
        return not any(fnmatch.fnmatchcase(p, arg) for p in changed)
    if kind == "added-path":
        return any(re.search(arg, p) for p in added)
    if kind == "added-line":
        return any(fnmatch.fnmatchcase(p, arg) and l for p, l in lines.items())
    if kind == "added-line-re":
        pre, _, lre = arg.partition("~~")
        return any(re.search(pre, p) and any(re.search(lre, x) for x in l) for p, l in lines.items())
    die("unknown trigger term %r" % t)


WEAKENING_LIB = os.path.join(HERE, "lib", "weakening_detector.py")
WEAKENING_SKIP_KINDS = ("blocking hook added",)   # hooks/ blocks by design: this kind would flag every exit 2 line


def weakening_flags(repo, base, tip, rows):
    """Row `w` (mode observe): run the optional weakening detector as a subprocess (this script runs `python3 -I`, no import).
    Observe only: any error, timeout or odd output returns [] and never changes rc."""
    if not any(r[0] == "w" and r[5] == "observe" for r in rows):
        return []
    try:
        mb = git(repo, "merge-base", base, tip).decode().strip()
        p = subprocess.run([sys.executable, "-I", WEAKENING_LIB, "--repo", repo, "--base", mb, "--head", tip,
                            "--budget", "0.7", "--json"], capture_output=True, timeout=5)
        out = json.loads(p.stdout.decode("utf-8", "replace"))
        res = []
        for f in out["flags"]:
            if f["kind"] in WEAKENING_SKIP_KINDS:
                continue
            res.append({k: str(f.get(k, "")).replace("\n", " ").replace("\r", " ") for k in ("id", "kind", "file", "line")})
        return res
    except Exception:
        return []


def main(argv):
    a = argv[1:]
    rows_f, log_f, nolog, pos = DEFAULT_ROWS, DEFAULT_LOG, False, []
    while a:
        x = a.pop(0)
        if x in ("--rows", "--log"):
            if not a:
                die("%s needs a value" % x)
            v = a.pop(0)
            if x == "--rows":
                rows_f = v
            else:
                log_f = v
        elif x == "--no-log":
            nolog = True
        elif x.startswith("--"):
            die("unknown option " + x)
        else:
            pos.append(x)
    if len(pos) != 3:
        die("usage: trigger_check.py <repo> <base> <tip> [--rows FILE] [--log FILE | --no-log]")
    repo, base, tip = pos
    if log_f == DEFAULT_LOG and not HERE.startswith(HOME_ROOT + os.sep):
        # a reviewed worktree must stay byte-clean: write the default log only from the installed copy
        nolog = True
    rows = load_rows(rows_f)
    changed, added, lines = diff_facts(repo, base, tip)
    tip12 = git(repo, "rev-parse", "--verify", tip + "^{commit}").decode().strip()[:12]
    hits = []
    for rid, trig, chk, src, rep, mode in rows:
        if mode != "observe":
            continue
        if all(term_ok(t, changed, added, lines) for t in trig.split(" & ")):
            hits.append((rid, chk, src))
    for rid, chk, src in hits:
        print("TRIGGER-OBSERVE: row %s (%s) would flag: %s" % (rid, src, chk))
    wflags = weakening_flags(repo, base, tip, rows)
    for f in wflags[:12]:
        print("TRIGGER-OBSERVE: weakening [%s] %s %s: %s" % (f["id"], f["kind"], f["file"], f["line"][:100]))
    for p in sorted(changed):
        if os.path.basename(p) in RULE_FILES or p in RULE_PATHS:
            p = p.replace("\n", "\\n").replace("\r", "\\r")
            print("RULES-EDIT: this change edits %s; the gate reads rules from BASE (base_read.py), not from this tip" % p)
    print("TRIGGER-OBSERVE: %d rows matched (observe only, never blocks)" % len(hits))
    if not nolog:
        try:
            os.makedirs(os.path.dirname(log_f), exist_ok=True)
            ts = time.strftime("%Y-%m-%dT%H:%M:%S")
            with open(log_f, "a", encoding="utf-8") as f:
                if hits:
                    for rid, _, _ in hits:
                        f.write("%s trigger_row_would_flag row=%s tip=%s\n" % (ts, rid, tip12))
                else:
                    f.write("%s trigger_row_clear tip=%s\n" % (ts, tip12))
                for wf in wflags:
                    f.write("%s weakening_flag kind=%s tip=%s\n" % (ts, wf["kind"].replace(" ", "_"), tip12))
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
