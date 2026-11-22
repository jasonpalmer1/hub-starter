#!/usr/bin/env python3
"""failure-register: a read-only register of failures that keep coming back.

Reads $HUBKIT_HOME/state/failures.jsonl (one JSON object per line, written by record_failure.py or by
your own jobs), turns each failure into a signature "job | step | reason class" with the changing parts
(times, process ids, hashes, paths, counts) removed, and writes failure-register.md plus a heartbeat file
failure-register.tick. A signature seen in 3 or more separate incidents inside the window is listed as a
repeat. Below that it stays silent (listed under "Below threshold" only).

It is READ-ONLY: it never opens a ticket, card or message. It writes only into --out (register, tick,
events.jsonl). Secrets and absolute paths are removed from every line it prints.

Record fields: ts (ISO text or epoch seconds), job, step, reason (text, may be empty), exit (int, optional),
text (raw failure text, optional), ok (true = a pass for job+step). Python 3.9, standard library only.
"""
import argparse
import glob
import gzip
import hashlib
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

BURST_SECS = 600  # same signature within 10 minutes = one incident (retries, spam bursts)
MIN_COUNT = 3
SIG_LIMIT = 80

# ------------------------------------------------------------------ scrub (secrets, paths)
_SECRETS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)", re.S),
    re.compile(r"\b(?:sk|pk|rk)[-_][A-Za-z0-9_-]{16,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*"),
    re.compile(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{12,}"),
]
_KEYVAL = re.compile(r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|apikey|authorization|credentials?)(\s*[=:]\s*)(?:bearer\s+|basic\s+)?\S+")
_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
_LONG = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{32,}(?![A-Za-z0-9_-])")
_PATH = re.compile(r"(?<![\w:/.])(?:/[\w.@+=,-]+){2,}/?|(?<![\w])~/[^\s\"')]*|\b[A-Za-z]:\\[^\s\"')]*")


def _long_token(m):
    s = m.group(0)
    if re.fullmatch(r"[0-9a-fA-F]+", s) and len(s) <= 64:
        return "<id>"
    if not (re.search(r"\d", s) and re.search(r"[A-Za-z]", s)):
        return s  # a long plain word or number run, not a credential
    return "<secret>"


def scrub(text):
    """Remove secrets and absolute paths. Keeps everything else."""
    t = "" if text is None else str(text)
    for rx in _SECRETS:
        t = rx.sub("<secret>", t)
    t = _KEYVAL.sub(lambda m: m.group(1) + m.group(2) + "<secret>", t)
    t = _UUID.sub("<id>", t)
    t = _LONG.sub(_long_token, t)
    t = _PATH.sub("<path>", t)
    return t


# ------------------------------------------------------------------ normalise
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|\[\d{1,2}m")
_TS = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?Z?")
_CLOCK = re.compile(r"\b\d{1,2}:\d{2}:\d{2}\b")
_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_PID = re.compile(r"\b(pid|holder pid|runs)[ =:]*\d+", re.I)
_HEX = re.compile(r"\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}\b", re.I)
_DUR = re.compile(r"\b\d+(?:\.\d+)?\s?(?:s|ms|m|h|min|MB|GB|KB)\b")
_NUM = re.compile(r"(?<![A-Za-z_-])\d+(?![A-Za-z_])")


def norm(text):
    """Collapse the parts that change between runs so the same failure groups together."""
    t = _ANSI.sub("", "" if text is None else str(text))
    t = scrub(t)
    t = _TS.sub("<ts>", t)
    t = _CLOCK.sub("<t>", t)
    t = _DATE.sub("<d>", t)
    t = _PID.sub(lambda m: m.group(1) + " <n>", t)
    t = _HEX.sub("<id>", t)
    t = _DUR.sub("<dur>", t)
    t = _NUM.sub("N", t)
    t = re.sub(r"[\x00-\x1f\x7f]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def label(text):
    return norm(text)[:SIG_LIMIT].replace("|", "/") or "-"


def reason_class(reason, text, exit_code):
    """One class per record. If several reasons appear, the first match in this fixed order wins."""
    low = ("%s %s" % (reason or "", text or "")).lower()
    if isinstance(exit_code, int) and not isinstance(exit_code, bool) and exit_code != 0:
        if exit_code == 124 or "timed out" in low or "timeout" in low:
            return "timeout"
        return "exit-%d" % exit_code
    if "404" in low:
        return "http-404"
    if "refus" in low:
        return "refused"
    if "timed out" in low or "timeout" in low:
        return "timeout"
    if "stale" in low:
        return "stale"
    if "skipped" in low:
        return "skipped"
    return "unknown"


class Event(object):
    __slots__ = ("ts", "job", "step", "cls", "raw", "src", "note")

    def __init__(self, ts, job, step, cls, raw, src, note=""):
        self.ts, self.job, self.step, self.cls, self.raw, self.src, self.note = ts, job, step, cls, raw, src, note

    @property
    def sig(self):
        return "%s | %s | %s" % (self.job, self.step, self.cls)


def sig_id(sig):
    return "rf-" + hashlib.sha1(sig.encode()).hexdigest()[:6]


# ------------------------------------------------------------------ read
def parse_ts(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    s = str(value or "").strip().rstrip("Z")
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


def read_rotated(path):
    """Yield (file label, line number, line) for path plus rotated siblings (.1, .2.gz ...).
    A line already seen in a different file of the same family is skipped, so a rotation
    that copied part of the day into both files never double counts."""
    files = [path] + sorted(glob.glob(path + ".[0-9]*"))
    seen_other = set()
    for f in files:
        if not os.path.isfile(f):
            continue
        opener = gzip.open if f.endswith(".gz") else open
        try:
            with opener(f, "rt", errors="replace") as fh:
                this = set()
                for n, line in enumerate(fh, 1):
                    line = line.rstrip("\n")
                    if line in seen_other:
                        continue
                    this.add(line)
                    yield os.path.basename(f), n, line
                seen_other |= this
        except (OSError, EOFError):
            continue


def load(path):
    """Returns (failures, passes, warnings)."""
    events, passes, warn, bad = [], [], [], 0
    if not os.path.isfile(path) and not glob.glob(path + ".[0-9]*"):
        return events, passes, ["source missing: %s" % os.path.basename(path)]
    for lab, n, line in read_rotated(path):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            bad += 1
            continue
        if not isinstance(rec, dict):
            bad += 1
            continue
        ts = parse_ts(rec.get("ts"))
        if ts is None:
            bad += 1
            continue
        job, step = label(rec.get("job")), label(rec.get("step"))
        if rec.get("ok") is True:
            passes.append((ts, job, step))
            continue
        cls = reason_class(rec.get("reason"), rec.get("text"), rec.get("exit"))
        raw = scrub(" ".join(str(x) for x in (rec.get("reason"), rec.get("text")) if x))
        events.append(Event(ts, job, step, cls, raw, "%s:%d" % (lab, n)))
    if bad:
        warn.append("%d unreadable line(s) skipped" % bad)
    return events, passes, warn


def parse_standing(text, prefixes, now):
    """Optional: `launchctl list` style lines (PID Status Label) whose label starts with a prefix."""
    out = []
    if not prefixes:
        return out
    for line in text.splitlines():
        parts = line.split(None, 2)
        if len(parts) != 3 or parts[0] == "PID":
            continue
        _, status, lab = parts
        if not lab.startswith(tuple(prefixes)):
            continue
        try:
            code = int(status)
        except ValueError:
            continue
        if code != 0:
            out.append(Event(now, label(lab), "last-exit", "exit-%d" % code, line, "standing", note="standing"))
    return out


# ------------------------------------------------------------------ analyse
def collapse(events, burst):
    by = defaultdict(list)
    for e in events:
        by[e.sig].append(e)
    out = {}
    for sig, evs in by.items():
        evs.sort(key=lambda e: e.ts)
        inc = []
        for e in evs:
            if inc and (e.ts - inc[-1]["last"]).total_seconds() <= burst:
                inc[-1]["lines"].append(e)
                inc[-1]["last"] = e.ts
            else:
                inc.append({"first": e.ts, "last": e.ts, "lines": [e]})
        out[sig] = inc
    return out


def last_pass(sample, passes):
    best = None
    for ts, job, step in passes:
        if job == sample.job and step == sample.step and (best is None or ts > best):
            best = ts
    return best


def load_prev_causes(md_path):
    """CAUSE cells survive a re-run: they are typed by a person into the register file."""
    causes = {}
    if os.path.isfile(md_path):
        with open(md_path, errors="replace") as fh:
            for line in fh:
                if line.startswith("| rf-"):
                    cells = [c.strip() for c in line.strip().strip("|").split("|")]
                    if len(cells) >= 9:
                        # the signature cell holds " | " separators, so count from the right:
                        # ... | Last pass | CAUSE | Status
                        causes[cells[0]] = cells[-2]
    return causes


_FIXED_AT = re.compile(r"@(\d{4}-\d{2}-\d{2})\s*$")


def status_of(cause, row):
    """open | caused | by-design | reopened | passed-since."""
    if cause.startswith("by-design"):
        return "by-design"
    if cause:
        m = _FIXED_AT.search(cause)
        if m:
            fixed = parse_ts(m.group(1) + "T23:59:59")
            if fixed and row["last"] > fixed:
                return "reopened"
        return "caused"
    if row["pass"] and row["pass"] > row["last"]:
        return "passed-since (still no CAUSE)"
    return "open"


def fmt(ts):
    return ts.strftime("%Y-%m-%d %H:%MZ") if ts else "-"


def cell(text):
    return re.sub(r"\s+", " ", scrub(text).replace("|", "/")).strip()[:120]


def render(events, passes, warn, now, since, until, replay, args, prev):
    inc = collapse([e for e in events if e.note != "standing"], args.burst_secs)
    rows, below = [], []
    for sig, incidents in inc.items():
        sample = incidents[0]["lines"][0]
        row = {"sig": sig, "id": sig_id(sig), "count": len(incidents),
               "lines": sum(len(i["lines"]) for i in incidents),
               "first": incidents[0]["first"], "last": incidents[-1]["last"],
               "pass": last_pass(sample, passes)}
        (rows if row["count"] >= args.min_count else below).append(row)
    standing = [e for e in events if e.note == "standing"]
    out = ["# Failure register%s" % (" (REPLAY %s .. %s)" % (since.date(), until.date()) if replay else ""), ""]
    out.append("Generated %s | window %s .. %s | burst collapse %ds | repeat threshold >= %d | read-only, no tickets"
               % (fmt(now), fmt(since), fmt(until), args.burst_secs, args.min_count))
    for w in warn:
        out.append("WARNING: " + w)
    out += ["", "## Repeats (count >= %d)" % args.min_count, "",
            "| id | Signature (job / step / reason class) | Count | Raw lines | First seen | Last seen | Last pass | CAUSE | Status |",
            "|---|---|---|---|---|---|---|---|---|"]
    statuses = {}
    for r in sorted(rows, key=lambda r: (-r["count"], r["sig"])):
        cause = cell(prev.get(r["id"], ""))
        statuses[r["id"]] = status_of(cause, r)
        out.append("| %s | %s | %d | %d | %s | %s | %s | %s | %s |" % (
            r["id"], r["sig"], r["count"], r["lines"], fmt(r["first"]), fmt(r["last"]),
            ("last pass " + fmt(r["pass"])) if r["pass"] else "no pass seen", cause, statuses[r["id"]]))
    if not rows:
        out.append("| - | none | | | | | | | |")
    out += ["", "## Standing non-zero exits (listed regardless of count)", "",
            "| id | Label | Last exit | Seen at | CAUSE | Status |", "|---|---|---|---|---|---|"]
    for e in sorted(standing, key=lambda e: e.job):
        i = sig_id(e.sig)
        cause = cell(prev.get(i, ""))
        out.append("| %s | %s | %s | %s | %s | %s |" % (
            i, e.job, e.cls.replace("exit-", ""), fmt(e.ts), cause,
            "by-design" if cause.startswith("by-design") else ("caused" if cause else "open")))
    if not standing:
        out.append("| - | none | | | | |")
    out += ["", "## Below threshold (count 1-%d, not flagged): %d signatures" % (args.min_count - 1, len(below)), ""]
    for r in sorted(below, key=lambda r: (-r["count"], r["sig"]))[:40]:
        out.append("- %s x%d  %s" % (r["id"], r["count"], r["sig"]))
    out.append("")
    return "\n".join(out), rows, below, standing, statuses


def explain(events, needle, burst):
    needle_l = needle.lower()
    hit = [e for e in events if needle_l in e.sig.lower() or needle_l == sig_id(e.sig)]
    sigs = sorted({e.sig for e in hit})
    inc = collapse(hit, burst)
    print("# --explain %r: %d raw lines, %d signatures, %d incidents" % (
        needle, len(hit), len(sigs), sum(len(v) for v in inc.values())))
    for s in sigs:
        print("\n## %s  [%s]  incidents=%d" % (s, sig_id(s), len(inc[s])))
        for e in sorted([x for x in hit if x.sig == s], key=lambda e: e.ts):
            print("  %s  %s  %s" % (fmt(e.ts), e.src, _ANSI.sub("", e.raw)[:220]))


def emit(out_dir, event, **fields):
    rec = {"ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "event": event}
    rec.update(fields)
    try:
        fd = os.open(os.path.join(out_dir, "events.jsonl"), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, (json.dumps(rec, sort_keys=True) + "\n").encode("utf-8"))
        finally:
            os.close(fd)
    except OSError:
        pass


def default_state():
    home = os.environ.get("HUBKIT_HOME") or os.path.join(os.path.expanduser("~"), ".claude", "hubkit")
    return os.path.join(home, "state")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="failure_register")
    ap.add_argument("--input", help="failures.jsonl (default $HUBKIT_HOME/state/failures.jsonl)")
    ap.add_argument("--out", help="folder for the register and tick (default $HUBKIT_HOME/state)")
    ap.add_argument("--since")
    ap.add_argument("--until")
    ap.add_argument("--now", help="override the clock (tests)")
    ap.add_argument("--explain", metavar="SIG")
    ap.add_argument("--min-count", type=int, default=MIN_COUNT)
    ap.add_argument("--burst-secs", type=int, default=BURST_SECS)
    ap.add_argument("--launchctl-file", help="optional: output of `launchctl list` to scan for non-zero exits")
    ap.add_argument("--prefix", action="append", default=[], help="label prefix to watch in --launchctl-file (repeatable)")
    args = ap.parse_args(argv)
    state = default_state()
    out_dir = args.out or state
    path = args.input or os.path.join(state, "failures.jsonl")
    now = parse_ts(args.now) if args.now else datetime.now(timezone.utc)
    until = parse_ts(args.until + "T23:59:59") if args.until else now
    since = parse_ts(args.since + "T00:00:00") if args.since else now - timedelta(days=7)
    replay = bool(args.since or args.until)
    events, passes, warn = load(path)
    if args.launchctl_file and args.prefix:
        try:
            with open(args.launchctl_file, errors="replace") as fh:
                events += parse_standing(fh.read(), args.prefix, min(now, until))
        except OSError as ex:
            warn.append("standing file unreadable: %s" % ex.__class__.__name__)
    events = [e for e in events if since <= e.ts <= until]
    passes = [p for p in passes if p[0] <= until]
    if args.explain:
        explain(events, args.explain, args.burst_secs)
        return 0
    os.makedirs(out_dir, exist_ok=True)
    md_path = os.path.join(out_dir, "failure-register-replay.md" if replay else "failure-register.md")
    md, rows, below, standing, statuses = render(events, passes, warn, now, since, until, replay, args, load_prev_causes(md_path))
    tmp = md_path + ".tmp"
    with open(tmp, "w") as fh:
        fh.write(md)
    os.replace(tmp, md_path)
    if not replay:
        with open(os.path.join(out_dir, "failure-register.tick"), "w") as fh:
            fh.write(now.strftime("%Y-%m-%dT%H:%M:%SZ") + "\n")
        for r in rows:
            if statuses.get(r["id"]) in ("open", "reopened"):
                emit(out_dir, "failure.repeat", id=r["id"], count=r["count"], status=statuses[r["id"]])
    print("wrote %s: %d repeats, %d standing, %d below threshold" % (md_path, len(rows), len(standing), len(below)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
