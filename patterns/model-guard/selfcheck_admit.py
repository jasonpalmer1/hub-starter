"""Bug-gate admission by the builder's own recorded checks (optional, used by model-guard.py).

A bug-gate brief that names a commit after an explicit cue word (tip, HEAD, sha, commit, at) is looked up in the
records builders ALREADY make: `<git-common-dir>/selfcheck/<sha>` or `<git-common-dir>/pregate/<sha>`. A record is
one JSON object per commit, {"sha": <full sha>, "exit": 0, "clean": true, ...}, written by the builder's own check
script. The repo is found from the absolute paths in the brief (nearest existing ancestor that is a git
repo); when no repo or no commit can be resolved the answer is a warning, never a block.

Rollout: warn-only until $HUBKIT_HOME/state/gate-admit-warn-since is 7 days old (same pattern as gate-round-cap-warn-since);
a missing marker means warn-only. Anything unexpected (unreadable record, git failure, crash) fails OPEN.
Only FileNotFoundError means "no record". Documented limits: a waiver line inside a code fence still counts; a
sha with no cue word is not checked; the record is a reminder, not an integrity control (anyone can write one)."""
import datetime
import json
import os
import re
import subprocess
import time

WARN_DAYS = 7
_CUE = r"(?<![A-Za-z0-9_])(?:tip|head|sha|commit|at)(?![A-Za-z0-9_])[ \t:=#`'\"(\[]{0,6}"
# 7-40 hex after a cue; not part of a longer word; not the start of a UUID-shaped string (hex-hyphen-hex).
_SHA = re.compile(_CUE + r"([0-9A-Fa-f]{7,40})(?![0-9A-Za-z_])(?!-[0-9A-Fa-f]{2,})", re.I)
_PATH = re.compile(r"(?<![\w./~-])(~/|/)[A-Za-z0-9_.@+%~/-]{3,}")
_WAIVE = re.compile(r"^[ \t]*SELFCHECK-WAIVED:[ \t]*(\S.*?)[ \t]*\r?$", re.M)
MAX_SHAS, MAX_PATHS = 5, 8


class _FailOpen(Exception):
    pass


class _Deadline(Exception):
    pass


BUDGET_S = 3.0          # one wall-clock budget for the whole admission check (hook timeout is 10 s)
_DEADLINE = [None]


def _budget():
    try:
        v = float(os.environ.get("GATE_ADMIT_BUDGET") or BUDGET_S)
        return v if v > 0 else BUDGET_S
    except ValueError:
        return BUDGET_S


def _remaining(cap):
    """Seconds a subprocess may still run: min(cap, time left). Raises _Deadline when the budget is spent."""
    if _DEADLINE[0] is None:
        return cap
    left = _DEADLINE[0] - time.monotonic()
    if left <= 0.05:
        raise _Deadline()
    return min(cap, left)


def marker_path():
    home = os.environ.get("HUBKIT_HOME") or os.path.expanduser("~/.claude/hubkit")
    return os.environ.get("GATE_ADMIT_MARKER") or os.path.join(home, "state", "gate-admit-warn-since")


def enforcing(marker=None, now=None):
    """True only when the marker holds a parseable `since <date>` that is >= WARN_DAYS old. Missing, empty, malformed,
    unreadable or future-dated = warn-only (never falls back to file mtime)."""
    marker = marker or marker_path()
    now = now if now is not None else float(os.environ.get("GATE_ADMIT_NOW") or datetime.datetime.now().timestamp())
    try:
        with open(marker, encoding="utf-8", errors="replace") as f:
            txt = f.read(400)
        m = re.search(r"since\s+(\d{4}-\d{2}-\d{2}(?:[T ][0-9:.+\-Zz]+)?)", txt)
        if not m:
            return False
        try:
            since = datetime.datetime.fromisoformat(m.group(1).replace("Z", "+00:00").replace("z", "+00:00")).timestamp()
        except Exception:
            return False
        return now - since >= WARN_DAYS * 86400
    except Exception:
        return False


def shas_in(prompt):
    out = []
    for m in _SHA.finditer(prompt or ""):
        s = m.group(1).lower()      # git decides below whether it is a commit; plain words/numbers fail to resolve and only warn
        if s not in out:
            out.append(s)
    return out[:MAX_SHAS]


def _git(args, cwd, timeout=4):
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0",
               GIT_CEILING_DIRECTORIES=os.path.expanduser("~"))
    p = subprocess.run(["git", "-C", cwd] + args, capture_output=True, text=True, timeout=_remaining(timeout), env=env)
    return p.returncode, p.stdout.strip()


def repo_dirs(prompt):
    """Existing directories (nearest existing ancestor of each absolute path named in the brief), in order, unique."""
    out = []
    for m in _PATH.finditer(prompt or ""):
        raw = m.group(0).rstrip(".:,;")
        p = os.path.expanduser(raw) if raw.startswith("~") else raw
        while p and p != "/" and not os.path.isdir(p):
            p = os.path.dirname(p)
        if p and p != "/" and p != os.path.expanduser("~") and p not in out:
            out.append(p)
        if len(out) >= MAX_PATHS:
            break
    return out


def read_record(common, full):
    """Mirror of `selfcheck precheck`. Returns (kind, None) when valid, (None, reason) when red or missing.
    Raises _FailOpen for any I/O error other than FileNotFoundError."""
    found = None
    for kind in ("pregate", "selfcheck"):
        p = os.path.join(common, kind, full)
        try:
            with open(p, "rb") as f:
                raw = f.read(1 << 20)
        except FileNotFoundError:
            continue
        except OSError as e:
            raise _FailOpen("%s: %s" % (type(e).__name__, p))
        if raw:
            found = (kind, raw)
            break
    if not found:
        return None, "no pregate/selfcheck record for %s (missing or empty)" % full[:12]
    kind, raw = found
    try:
        d = json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return None, "%s record is not valid JSON" % kind
    if not isinstance(d, dict):
        return None, "%s record is not a JSON object" % kind
    if d.get("sha") != full:
        return None, "%s record sha %r does not match" % (kind, d.get("sha"))
    if d.get("clean") is False:
        return None, "%s record: tree was dirty when it was written" % kind
    if "exit" in d and d["exit"] != 0:
        return None, "%s record exit code is %r, not 0" % (kind, d["exit"])
    if kind == "selfcheck":
        if d.get("exit") != 0 or d.get("clean") is not True:
            return None, "selfcheck record lacks exit=0 and clean=true"
    elif not d.get("checks"):
        return None, "pregate record lists no checks"
    return kind, None


def evaluate(prompt):
    """Return ('ok', None) | ('red', reason) | ('unresolved', reason). Raises on anything unexpected."""
    shas = shas_in(prompt)
    if not shas:
        return "nosha", "bug-gate prompt names no commit after a cue word (tip/HEAD/sha/commit/at); admission not checked"
    seen, reds, resolved = set(), [], False
    for d in repo_dirs(prompt):
        try:
            rc, common = _git(["rev-parse", "--path-format=absolute", "--git-common-dir"], d)
            if rc != 0 or not common or common in seen:
                continue
            seen.add(common)
        except _Deadline:
            raise
        except (subprocess.SubprocessError, OSError):
            continue
        for s in shas:
            rc, full = _git(["rev-parse", "--verify", "-q", s + "^{commit}"], d)
            if rc != 0 or not re.fullmatch(r"[0-9a-f]{40}", full or ""):
                continue
            resolved = True
            kind, why = read_record(common, full)
            if why is None:
                return "ok", None
            else:
                reds.append("%s: %s" % (full[:12], why))
    if resolved:
        return "red", "; ".join(reds)[:600]
    return "unresolved", "bug-gate prompt names sha(s) %s but no repo/commit could be resolved from its paths" % ", ".join(s[:12] for s in shas)


def check(tin, prompt, path=None):
    """Return None (not applicable / allowed), ('waived', reason), ('warn', msg) or ('block', msg). Fails open."""
    _DEADLINE[0] = None
    try:
        if str(tin.get("subagent_type") or "").strip().lower() != "bug-gate":
            return None
        w = _WAIVE.search(prompt or "")
        if w:
            return ("waived", w.group(1)[:200])
        _DEADLINE[0] = time.monotonic() + _budget()
        try:
            kind, msg = evaluate(prompt or "")
        except _Deadline:
            return ("warn", "selfcheck admission skipped (time budget %.1fs spent, fail-open)" % _budget())
        except subprocess.TimeoutExpired:
            return ("warn", "selfcheck admission skipped (subprocess timed out, fail-open)")
        except _FailOpen as e:
            return ("warn", "selfcheck admission skipped (record unreadable, fail-open): %s" % e)
        if kind == "ok":
            return None
        if kind in ("nosha", "unresolved"):
            return ("warn", msg)
        hint = ("no all-green selfcheck/pregate record for the named commit (%s). The builder runs its own check script on that exact committed HEAD and fixes what is red; "
                "or add a line 'SELFCHECK-WAIVED: <reason>' to the brief." % msg)
        if enforcing(path):
            return ("block", hint)
        return ("warn", "WARN-ONLY until the gate-admit marker is %d days old (would block): %s" % (WARN_DAYS, hint))
    except Exception as e:
        return ("warn", "selfcheck admission crashed (fail-open): %s" % type(e).__name__)
    finally:
        _DEADLINE[0] = None
