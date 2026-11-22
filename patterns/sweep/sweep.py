#!/usr/bin/env python3
"""sweep: find stale tickets and tickets owned by a dead lane; hand the dead lane's tickets back.

    python3 sweep.py sweep [--json]                 findings, each sent through wake-gate (read only)
    python3 sweep.py orphans [--reassign] [--json]  tickets whose owner is provably dead; --reassign
                                                    puts them back in the queue (status open, owner unowned)
    python3 sweep.py hold --lane <name> --minutes N | --clear
                                                    a held lane's tickets are never taken away
                                                    (same holds/<lane>.json the work-loop pattern writes)

An owner is DEAD only when the live-owner list says so. The list is $HUBKIT_HOME/state/live-owners.txt
(override: SWEEP_LIVE_OWNERS), written by your own script, one owner name per line:

    # live-owners <epoch seconds>
    lane-a
    lane-b
    # end

Anything else is UNKNOWN, and unknown is never dead: file missing, unreadable, a symlink, empty, no
header or no `# end` line (a truncated write), older than 30 min, dated in the future, no names at all, or any
malformed name line.
A ticket is also left alone when its owner is alive, is `operator`/`unowned`, is held, the ticket is not
open or active, it changed after the list was written or less than 10 min ago, or the ledger has a bad line.
Names compare whole (trimmed, lower case): `lane-1` is not `lane-10`.
Wakes: only a dead lane holding an operator-asked ticket reaches wake-gate as a WAKE candidate; stale
tickets, unknown liveness and done-without-proof are warn and only ever logged.
State: $HUBKIT_HOME/state/{live-owners.txt,holds/,sweep.lock,events.jsonl}. Python 3.9, stdlib only, POSIX only.
"""
import argparse
import contextlib
import fcntl
import json
import os
import re
import stat
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (HERE, os.path.join(HERE, "..", "ledger")):
    if os.path.isfile(os.path.join(_p, "ledger_lib.py")):
        sys.path.insert(0, _p)
        break
import ledger_lib  # noqa: E402

LIVE_MAX_AGE_S = 30 * 60
MIN_TICKET_AGE_S = 10 * 60
PROTECTED = ("", "unowned", "operator")
LANE_RE = re.compile(r"^[a-z0-9-]{1,32}$")
NAME_RE = re.compile(r"^[a-z0-9._@-]{1,64}$")  # one owner name per live-list line, after trim and lower case
DEAD_KIND = "dead-lane-holding-operator-asks"


def home():
    return os.environ.get("HUBKIT_HOME") or os.path.join(os.path.expanduser("~"), ".claude", "hubkit")


def state_dir():
    return os.path.join(home(), "state")


def now_s():
    raw = os.environ.get("SWEEP_NOW")  # tests only: a fixed clock
    return float(raw) if raw else time.time()


def norm(name):
    return str(name or "").strip().lower()


def clean(text, n=80):
    return re.sub(r"[\x00-\x1f\x7f\s]+", " ", str(text)).strip()[:n]


def emit(event, **fields):
    rec = dict(ts=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now_s())), event=event, **fields)
    try:
        os.makedirs(state_dir(), exist_ok=True)
        fd = os.open(os.path.join(state_dir(), "events.jsonl"), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, (json.dumps(rec, sort_keys=True) + "\n").encode("utf-8"))
        finally:
            os.close(fd)
    except OSError:
        pass


def read_live(now):
    """((set of live owners, written_at), None) or (None, why-unknown). Unknown is never read as dead."""
    path = os.environ.get("SWEEP_LIVE_OWNERS") or os.path.join(state_dir(), "live-owners.txt")
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            return None, "live-owner list is not a plain file"
        with open(path, "rb") as fh:
            text = fh.read(1 << 20).decode("utf-8")
    except FileNotFoundError:
        return None, "live-owner list missing"
    except (OSError, UnicodeDecodeError):
        return None, "live-owner list unreadable"
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    m = re.match(r"^# live-owners (\d{9,11}(?:\.\d+)?)$", lines[0]) if lines else None
    if not m or lines[-1] != "# end" or "\x00" in text:
        return None, "live-owner list has no header or no end line"
    at = float(m.group(1))
    if at > now + 60 or now - at > LIVE_MAX_AGE_S:
        return None, "live-owner list is stale or dated in the future"
    names = {norm(ln) for ln in lines[1:-1] if not ln.startswith("#")}
    if not all(NAME_RE.match(n) for n in names):  # comma-joined, annotated, JSON...: not our grammar, so UNKNOWN
        return None, "live-owner list has a malformed name line"
    if not names:
        return None, "live-owner list names nobody"
    # A name written behind a # comment ("# lane-a") must not read as dead: every word in a comment that could be an
    # owner name is kept as alive. That can only keep a ticket where it is, never take one.
    for ln in lines[1:-1]:
        if ln.startswith("#"):
            names.update(w for w in (norm(x) for x in ln.lstrip("#").split()) if NAME_RE.match(w))
    return (names, at), None


def read_hold(lane):
    """True when the lane is held (an unreadable hold file counts as a hold)."""
    p = os.path.join(state_dir(), "holds", lane + ".json")
    if not os.path.exists(p):
        return False
    try:
        with open(p, "r", encoding="utf-8") as fh:
            raw = json.load(fh)["until"]
            if isinstance(raw, bool):
                return True
            until = float(raw)
            return until != until or now_s() < until  # NaN is a hold of unknown length, not "no hold"
    except (OSError, ValueError, KeyError, TypeError):
        return True


def read_ledger():
    """(records, bad line count). Callers refuse to write when bad > 0."""
    events, bad = [], 0
    try:
        with open(ledger_lib.LEDGER_PATH, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    v = json.loads(line)
                except ValueError:
                    v = None
                if isinstance(v, dict) and "id" in v and not isinstance(v["id"], str):
                    print("sweep: skipping a ledger line with a non-text id", file=sys.stderr)
                elif isinstance(v, dict):
                    events.append(v)
                else:
                    bad += 1
    except FileNotFoundError:
        pass
    return ledger_lib.fold(events), bad


def ts_epoch(rec):
    return ledger_lib.parse_ts(rec.get("updated_at") or rec.get("created_at") or "1970-01-01T00:00:00Z").timestamp()


def find_orphans(records, live, held, now):
    """Pure rule. live = (names, written_at) or None. Returns [(ticket id, owner)] provably dead."""
    if live is None:
        return []
    names, at = live
    out = []
    for tid, rec in sorted(records.items()):
        owner = norm(rec.get("owner"))
        if rec.get("status") not in ("open", "active") or owner in PROTECTED or owner in names:
            continue
        if held(owner) or ts_epoch(rec) >= at or now - ts_epoch(rec) < MIN_TICKET_AGE_S:
            continue
        out.append((tid, owner))
    return out


def load_wake_gate():
    for d in (os.path.join(HERE, "..", "wake-gate"), os.path.join(HERE, "wake-gate")):
        if os.path.isfile(os.path.join(d, "wake_gate.py")):
            sys.path.insert(0, d)
            try:
                import wake_gate
                return wake_gate
            except Exception:
                return None
    return None


def build_findings(records, bad, live, why, now):
    f = []
    if bad:
        f.append(dict(kind="ledger-bad-lines", severity="warn", subject="ledger",
                      summary="%d unreadable ledger line(s); nothing is reassigned" % bad))
    if live is None:
        f.append(dict(kind="liveness-unknown", severity="warn", subject="live-owners", summary=why))
    for tid, rec in sorted(records.items()):
        if rec.get("status") == "done" and not ledger_lib.has_done_gate_proof(rec):
            f.append(dict(kind="done-without-proof", severity="warn", subject=clean(tid), summary="%s is done with no proof" % clean(tid)))
        elif ledger_lib.is_stale(rec):
            f.append(dict(kind="stale-ticket", severity="warn", subject=clean(tid),
                          summary="%s untouched 48h+, owner %s" % (clean(tid), clean(rec.get("owner")))))
    for tid, owner in find_orphans(records, live, read_hold, now):
        if records[tid].get("asked_by") == "operator":
            f.append(dict(kind=DEAD_KIND, severity="warn", subject=owner, 
                          summary="%s holds operator ask %s but is not alive" % (clean(owner), clean(tid))))
        else:
            f.append(dict(kind="dead-owner", severity="warn", subject=clean(tid),
                          summary="%s owned by dead %s" % (clean(tid), clean(owner))))
    return f


def cmd_sweep(args):
    now = now_s()
    records, bad = read_ledger()
    live, why = read_live(now)
    wg = load_wake_gate()
    for f in build_findings(records, bad, live, why, now):
        f["source"] = "sweep"
        f["verdict"] = wg.gate(f, now=now)["verdict"] if wg else "NO-GATE"
        emit("sweep.finding", kind=f["kind"], subject=clean(f["subject"]), verdict=f["verdict"])
        print(json.dumps(f, sort_keys=True) if args.json else
              "%s %s %s: %s" % (f["verdict"], f["kind"], clean(f["subject"]), f["summary"]))
    return 0


def cmd_orphans(args):
    now = now_s()
    out = dict(dead=[], reassigned=[])

    def say(text):  # with --json stdout carries the one JSON line only
        print(text, file=sys.stderr if args.json else sys.stdout)

    def finish(**extra):
        out.update(extra)
        if args.json:
            print(json.dumps(out, sort_keys=True))
        return 0

    os.makedirs(state_dir(), exist_ok=True)
    with open(os.path.join(state_dir(), "sweep.lock"), "a") as lockfh:
        try:
            fcntl.flock(lockfh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            say("sweep: another sweep is running; nothing done")
            return finish(busy=True)
        live, why = read_live(now)
        records, bad = read_ledger()
        if live is None:
            say("sweep: liveness unknown (%s); no ticket is taken from anyone" % why)
            return finish(unknown=why)
        found = find_orphans(records, live, read_hold, now)
        out["dead"] = [clean(t) for t, _ in found]
        for tid, owner in found:
            say("%s dead owner: %s" % (clean(tid), clean(owner)))
            if not args.reassign:
                continue
            with ledger_lib.ledger_lock():  # re-check under the ledger lock: the ticket may have moved
                fresh, bad2 = read_ledger()
                if bad2:
                    say("sweep: ledger has unreadable lines; not reassigning")
                    break
                if (tid, owner) not in find_orphans({tid: fresh.get(tid, {})}, live, read_hold, now):
                    continue
                ledger_lib.append_event({"ts": ledger_lib.now_iso(), "id": tid, "event": "status", "status": "open",
                                         "owner": "unowned", "note": "returned to queue: owner %s not live" % clean(owner)})
            emit("orphans.reassigned", ticket=clean(tid), owner=clean(owner))
            out["reassigned"].append(clean(tid))
    return finish()


def cmd_hold(args):
    lane = norm(args.lane)
    if not LANE_RE.match(lane):
        print("sweep: bad lane name", file=sys.stderr)
        return 2
    p = os.path.join(state_dir(), "holds", lane + ".json")
    if args.clear:
        with contextlib.suppress(OSError):
            os.unlink(p)
        print("hold cleared for %s" % lane)
        return 0
    if args.minutes is None or not (0 < args.minutes <= 7 * 24 * 60):
        print("sweep: hold needs --minutes 1..10080 (a hold always expires) or --clear", file=sys.stderr)
        return 2
    os.makedirs(os.path.dirname(p), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".hold.", dir=os.path.dirname(p))
    with os.fdopen(fd, "w") as fh:
        json.dump({"until": now_s() + args.minutes * 60, "reason": clean(args.reason)}, fh)
    os.replace(tmp, p)
    print("held %s for %d min" % (lane, args.minutes))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sweep")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_sweep)
    o = sub.add_parser("orphans")
    o.add_argument("--reassign", action="store_true")
    o.add_argument("--json", action="store_true")
    o.set_defaults(fn=cmd_orphans)
    h = sub.add_parser("hold")
    h.add_argument("--lane", required=True)
    h.add_argument("--minutes", type=int)
    h.add_argument("--clear", action="store_true")
    h.add_argument("--reason", default="")
    h.set_defaults(fn=cmd_hold)
    try:
        a = ap.parse_args(argv)
    except SystemExit as e:
        return 2 if e.code else 0
    try:
        return a.fn(a)
    except (OSError, ValueError) as e:
        print("sweep: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
