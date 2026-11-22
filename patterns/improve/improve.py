#!/usr/bin/env python3
"""improve: the empty-queue policy. When nothing else is queued, look only at MEASURED problems.

  improve.py mine [--dry-run]      queue empty? read the measured sources; admit at most a few candidates
  improve.py propose --source failure|target --key KEY [--dry-run]    the same gate, for one named signal
  improve.py close CAND [--by-design WHY]   close with a before/after reading taken by this tool, or refuse
  improve.py ask L-N               admit an operator-asked ticket (no caps, no evidence needed)
  improve.py status                open / today counts

A candidate is a ledger ticket with a deterministic id, an evidence line (file under $HUBKIT_HOME, the number read,
the threshold) and a done-test (the same reader, run again). It is built from a template: raw log text is never
copied into a title or note. Every refusal names its reason. Nothing here does the work or spends anything.

Sources (all read from $HUBKIT_HOME/state):
  failure   failure-register.md   a signature seen in >= 3 incidents, no CAUSE typed, evidence <= 7 days old
  target    targets.json          an operator-owned number file that misses its threshold
  bugsweep  bugsweep/NAME.txt     written by bug_sweep.py: line 1 is 1 when the project's smoke test failed, 0 when it passed

Python 3.9, standard library only. POSIX only (fcntl).
"""
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (HERE, os.path.join(HERE, "..", "ledger")):
    if os.path.isfile(os.path.join(_p, "ledger_lib.py")):
        sys.path.insert(0, _p)
        break
import ledger_lib  # noqa: E402  (the ledger pattern; same file format)

MIN_COUNT = 3                  # a signature must be seen in this many incidents
EVIDENCE_MAX_AGE_S = 7 * 86400
FUTURE_SLOP_S = 300            # evidence stamped further ahead than this is not trusted
DAILY_CAP = 3                  # new candidates per UTC day
OPEN_CAP = 5                   # candidates open at once
BY_DESIGN_CAP = 5              # "by-design" closures per 30 days
BY_DESIGN_WINDOW_S = 30 * 86400
FLAP_WINDOW = 6                # readings looked at for flapping
FLAP_CHANGES = 3               # fire/no-fire changes inside the window that mean "flapping"
FILE_MAX = 1_000_000
PROJECT = "improve"

# Text that came out of a log must look like a job name, nothing else. These are the shapes an injection takes.
SAFE_TEXT = re.compile(r"^[A-Za-z0-9 ._:/<>|+-]{1,200}$")
INJECTION = re.compile(
    r"(?i)\bignore\b|\bdisregard\b|\bforget\b|\boverride\b|\bbypass\b|previous instructions|\bsystem\s*:|\bassistant\s*:"
    r"|\byou (?:must|should|are|will)\b|\bL-[0-9N]+\b|\bledger\b|\b(?:close|reopen|drop|approve|mark)\b.{0,20}\b(?:done|ticket|item|all)\b"
    r"|\b(?:raise|lift|remove|disable)\b.{0,15}\b(?:cap|limit|guard)\b|\boperator\b")
NAME_RE = re.compile(r"^[a-z0-9_-]{1,30}$")
KEY_RE = re.compile(r"^rf-[0-9a-f]{6}$")


class Refuse(Exception):
    def __init__(self, reason):
        Exception.__init__(self, reason)
        self.reason = reason


# ------------------------------------------------------------------ paths and clock
def home():
    return os.path.realpath(os.environ.get("HUBKIT_HOME") or os.path.join(os.path.expanduser("~"), ".claude", "hubkit"))


def state_dir():
    return os.path.join(home(), "state")


def now_s():
    raw = os.environ.get("IMPROVE_NOW")  # tests only: a fixed clock (epoch seconds)
    return float(raw) if raw else time.time()


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def day_of(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d")


def parse_when(text):
    """'2026-10-04 01:00Z' or ISO -> epoch seconds, or None."""
    s = str(text or "").strip().rstrip("Z").replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            pass
    return None


def inside_home(real):
    h = home()
    return real == h or real.startswith(h + os.sep)


def evidence_file(rel, now, need_after=None, reading=True):
    """Resolve an evidence path. Returns (relative label, real path, mtime, text). Raises Refuse.
    Order: outside $HUBKIT_HOME (symlinks followed) -> missing -> stale or from the future."""
    real = os.path.realpath(os.path.join(home(), rel))
    if not inside_home(real):
        raise Refuse("evidence-outside-home")
    if not os.path.isfile(real):
        raise Refuse("no-evidence")
    mtime = os.path.getmtime(real)
    if reading and (mtime > now + FUTURE_SLOP_S or now - mtime > EVIDENCE_MAX_AGE_S):
        raise Refuse("stale-evidence")
    if need_after is not None and mtime <= need_after:
        raise Refuse("evidence-not-refreshed")
    try:
        with open(real, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read(FILE_MAX)
    except OSError:
        raise Refuse("no-evidence")
    return os.path.relpath(real, home()), real, mtime, text


def injected(text, free_text=False):
    """Log-derived text must look like a job name and carry no instruction. free_text=True is for a sentence the
    operator typed (a by-design reason): the charset is relaxed, the instruction patterns still apply."""
    t = str(text)
    return bool(INJECTION.search(t)) or (not free_text and not SAFE_TEXT.match(t))


# ------------------------------------------------------------------ sources
def parse_register(text):
    """{id: {sig, count, last, pass_at, cause, status, where}} from failure-register.md. Counts from the right:
    the signature cell holds ' | ' separators."""
    if "## Repeats" not in text:
        raise Refuse("no-evidence")
    rows = {}
    for line in text.splitlines():
        if line.startswith("| rf-"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) < 9:
                continue
            m = re.match(r"last pass (.+)$", cells[-3])
            rows[cells[0]] = {"sig": " | ".join(cells[1:-7]).strip(), "count": _int(cells[-7]), "last": parse_when(cells[-4]),
                              "pass_at": parse_when(m.group(1)) if m else None, "cause": cells[-2], "status": cells[-1],
                              "where": "repeats"}
        else:
            m = re.match(r"^- (rf-[0-9a-f]{6}) x(\d+)\s+(.*)$", line)
            if m and m.group(1) not in rows:
                rows[m.group(1)] = {"sig": m.group(3).strip(), "count": int(m.group(2)), "last": None, "pass_at": None,
                                    "cause": "", "status": "below", "where": "below"}
    return rows


def _int(s):
    try:
        return int(s)
    except ValueError:
        return 0


def read_failure(key, now, need_after=None):
    if not KEY_RE.match(str(key)):
        raise Refuse("bad-key")
    rel, _, mtime, text = evidence_file(os.path.join("state", "failure-register.md"), now, need_after)
    rows = parse_register(text)
    row = rows.get(key)
    return {"source": "failure", "key": key, "path": rel, "mtime": mtime, "row": row, "threshold": MIN_COUNT, "op": ">=",
            "number": row["count"] if row else 0}


def failure_keys(now):
    try:
        _, _, _, text = evidence_file(os.path.join("state", "failure-register.md"), now)
    except Refuse as r:
        return [], r.reason
    try:
        return sorted(parse_register(text)), None
    except Refuse:
        return [], "no-heading"


def load_targets(now):
    """[(name, spec)] from targets.json. Raises Refuse. The file is operator-owned; it names number files only."""
    rel, _, _, text = evidence_file(os.path.join("state", "targets.json"), now, reading=False)  # operator-owned config, not a reading
    try:
        data = json.loads(text)
        items = data["targets"]
        out = [(str(t["name"]), t) for t in items]
    except (ValueError, KeyError, TypeError):
        raise Refuse("no-evidence")
    return out


def read_target(name, now, need_after=None):
    if not NAME_RE.match(str(name)):
        raise Refuse("evidence-injection")
    spec = dict(load_targets(now)).get(name)
    if spec is None:
        raise Refuse("no-evidence")
    rel, _, mtime, text = evidence_file(str(spec.get("file", "")), now, need_after)
    m = re.match(r"\s*(-?\d+(?:\.\d+)?)", text)
    if not m:
        raise Refuse("no-evidence")
    try:
        thr = float(spec["threshold"])
        op = {"max": "<=", "min": ">="}[spec["op"]]
    except (KeyError, ValueError, TypeError):
        raise Refuse("no-evidence")
    return {"source": "target", "key": name, "path": rel, "mtime": mtime, "row": None, "threshold": thr, "op": op,
            "number": float(m.group(1))}


def bugsweep_keys():
    """Project names that have a sweep result file (file names only; the gate reads each one)."""
    try:
        names = os.listdir(os.path.join(state_dir(), "bugsweep"))
    except OSError:
        return []
    return sorted(n[:-4] for n in names if n.endswith(".txt") and NAME_RE.match(n[:-4]))


def read_bugsweep(name, now, need_after=None):
    """A sweep result: number 1 = the smoke test failed, 0 = it passed. The output tail is carried, never used in a title."""
    if not NAME_RE.match(str(name)):
        raise Refuse("evidence-injection")
    rel, _, mtime, text = evidence_file(os.path.join("state", "bugsweep", str(name) + ".txt"), now, need_after)
    m = re.match(r"\s*([01])\s*(?:\n|$)", text)
    if not m:
        raise Refuse("no-evidence")
    out = text.split("--- output ---\n", 1)[1] if "--- output ---\n" in text else ""
    out = re.sub(r"[\x00-\x1f\x7f]+", " ", out).strip()[:400]
    return {"source": "bugsweep", "key": name, "path": rel, "mtime": mtime, "row": None, "threshold": 0.0, "op": "<=",
            "number": float(m.group(1)), "output": out}


def read_source(source, key, now, need_after=None):
    if source == "failure":
        return read_failure(key, now, need_after)
    if source == "bugsweep":
        return read_bugsweep(key, now, need_after)
    if source == "target":
        return read_target(key, now, need_after)
    raise Refuse("bad-source")


def fires(r):
    """Does the measured problem exist right now? -> (bool, reason if not)."""
    if r["source"] == "failure":
        row = r["row"]
        if row is None:
            return False, "no-evidence"
        if row["last"] is not None and row["last"] > r["mtime"] + FUTURE_SLOP_S:
            return False, "stale-evidence"
        if row["count"] < r["threshold"]:
            return False, "below-threshold"
        if not (row["status"] in ("open", "reopened") or row["status"].startswith("passed-since")):
            return False, "has-cause"
        return True, ""
    ok = r["number"] > r["threshold"] if r["op"] == "<=" else r["number"] < r["threshold"]
    return (True, "") if ok else (False, "below-threshold")


def sig_text(r):
    return r["row"]["sig"] if r["source"] == "failure" else r["key"]


def cand_id(source, key):
    return "im-" + hashlib.sha1(("%s\0%s" % (source, key)).encode()).hexdigest()[:8]


# ------------------------------------------------------------------ state files
def path_in_state(name):
    return os.path.join(state_dir(), name)


def read_jsonl(name):
    out = []
    try:
        with open(path_in_state(name), "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    v = json.loads(line)
                except ValueError:
                    continue
                if isinstance(v, dict):
                    out.append(v)
    except OSError:
        pass
    return out


def append_jsonl(name, rec):
    os.makedirs(state_dir(), exist_ok=True)
    fd = os.open(path_in_state(name), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, (json.dumps(rec, sort_keys=True) + "\n").encode("utf-8"))
    finally:
        os.close(fd)


def emit(event, now, **fields):
    rec = {"ts": iso(now), "event": event}
    rec.update(fields)
    try:
        append_jsonl("events.jsonl", rec)
    except OSError:
        pass


@contextlib.contextmanager
def locked():
    os.makedirs(state_dir(), exist_ok=True)
    fd = os.open(path_in_state("improve.lock"), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def ledger_records():
    try:
        return ledger_lib.fold(ledger_lib.read_events())
    except ValueError:
        raise Refuse("ledger-unreadable")


def candidates(records):
    """{cand: {...}} folded from improve-candidates.jsonl. open = proposed and not closed (log or ledger)."""
    out = {}
    for e in read_jsonl("improve-candidates.jsonl"):
        c = e.get("cand")
        if not c:
            continue
        if e.get("event") == "proposed":
            out[c] = {"cand": c, "source": e.get("source"), "key": e.get("key"), "ledger_id": e.get("ledger_id"),
                      "proposed": e.get("t", 0), "before": e.get("before"), "closed": None, "mode": None,
                      "dropped": False}
        elif e.get("event") == "closed" and c in out:
            out[c]["closed"] = e.get("t", 0)
            out[c]["mode"] = e.get("mode")
            out[c]["dropped"] = e.get("mode") == "by-design"
    for c in out.values():
        rec = records.get(c["ledger_id"])
        if c["closed"] is None and rec and rec["status"] in ("done", "dropped"):
            c["closed"] = parse_when((rec.get("updated_at") or "").replace("T", " ")) or c["proposed"]
            c["mode"] = "ledger"
            c["dropped"] = rec["status"] == "dropped"
    return out


# ------------------------------------------------------------------ the gate
def history(source, key):
    return [e for e in read_jsonl("improve-readings.jsonl") if e.get("source") == source and e.get("key") == key]


def flapping(hist, fire_now):
    seq = [bool(e.get("fire")) for e in hist][-(FLAP_WINDOW - 1):] + [fire_now]
    return sum(1 for a, b in zip(seq, seq[1:]) if a != b) >= FLAP_CHANGES


def admit(source, key, now, dry, records, cands):
    """The whole gate for one signal. Returns (decision, reason, detail). Order is fixed; the first block wins."""
    try:
        r = read_source(source, key, now)
    except Refuse as ex:
        return "REFUSE", ex.reason, {}
    if r["source"] == "failure" and r["row"] is None:
        return "REFUSE", "no-evidence", {}
    if injected(sig_text(r)):
        return "REFUSE", "evidence-injection", {}
    fire, why = fires(r)
    if not dry:
        append_jsonl("improve-readings.jsonl", {"t": now, "source": source, "key": key, "fire": fire})
    if not fire:
        return "REFUSE", why, {}
    cid = cand_id(source, key)
    prior = cands.get(cid)
    if prior is not None:
        if prior["closed"] is None:
            return "REFUSE", "duplicate", {}
        recur = (r["row"]["last"] is not None and r["row"]["last"] > prior["closed"]) if source == "failure" \
            else r["mtime"] > prior["closed"]
        if prior["dropped"] and source in ("target", "bugsweep"):
            # Cooldown: the operator dropped it. A refreshed number file is not new evidence; a different
            # reading is. (A dropped failure already needs a newer failure than the drop.)
            b = prior["before"] if isinstance(prior["before"], dict) else {}
            recur = r["number"] != b.get("number")
        if not recur:
            return "REFUSE", "duplicate", {}
    hist = history(source, key)
    if not dry:
        hist = hist[:-1]  # the reading just appended is the current one
    if flapping(hist, True):
        return "REFUSE", "flapping", {}
    today = day_of(now)
    if sum(1 for e in read_jsonl("improve-candidates.jsonl") if e.get("event") == "proposed" and day_of(e.get("t", 0)) == today) >= DAILY_CAP:
        return "REFUSE", "daily-cap", {}
    if sum(1 for c in cands.values() if c["closed"] is None) >= OPEN_CAP:
        return "REFUSE", "open-cap", {}
    detail = {"cand": cid, "reading": r}
    if dry:
        return "PROPOSE", "dry-run", detail
    detail["ledger_id"] = create_ticket(cid, r, now)
    return "PROPOSE", "", detail


def title_for(r):
    if r["source"] == "failure":
        return "Improve: repeat failure %s (%dx)" % (r["key"], r["number"])
    if r["source"] == "bugsweep":
        return "Improve: smoke test fails in %s" % r["key"]
    return "Improve: target %s is off" % r["key"]


def fmt_num(n):
    return ("%d" % n) if float(n) == int(n) else ("%.2f" % n)


def create_ticket(cid, r, now):
    with ledger_lib.ledger_lock():
        events = ledger_lib.read_events()
        eid = ledger_lib.next_id(events)
        ledger_lib.append_event({"ts": ledger_lib.now_iso(), "id": eid, "event": "created", "title": title_for(r),
                                 "asked_by": "hub", "asked_on": ledger_lib.today_str(), "owner": "unowned",
                                 "trigger": "", "project": PROJECT})
        note = "%s | evidence: %s read %s, threshold %s %s | done-test: improve.py close %s (re-reads the same file)" % (
            cid, r["path"], fmt_num(r["number"]), r["op"], fmt_num(r["threshold"]), cid)
        ledger_lib.append_event({"ts": ledger_lib.now_iso(), "id": eid, "event": "note", "note": note})
    before = {"number": r["number"], "threshold": r["threshold"], "op": r["op"], "path": r["path"],
              "last": r["row"]["last"] if r["row"] else None}
    if r.get("output"):
        before["output"] = r["output"]
    append_jsonl("improve-candidates.jsonl", {"event": "proposed", "cand": cid, "t": now, "source": r["source"],
                                              "key": r["key"], "ledger_id": eid, "before": before})
    emit("improve.candidate", now, cand=cid, ledger_id=eid)
    return eid


# ------------------------------------------------------------------ commands
def queue_blockers(records, cands):
    """Every open ticket that no candidate owns blocks mining, whatever its project label or who asked.
    The label is free text, so nothing is skipped on it. (A crash can no longer leave our own ticket without a
    candidate: the ticket and the candidate line are written under one lock.)"""
    own = {c["ledger_id"] for c in cands.values()}
    return sorted(i for i, rec in records.items() if i not in own and rec["status"] in ("open", "active"))


def cmd_mine(args):
    now = now_s()
    ctx = locked() if not args.dry_run else contextlib.nullcontext()
    with ctx:
        # Everything the decision rests on is read INSIDE the lock, so an overlapping miner sees our tickets.
        try:
            records = ledger_records()
        except Refuse as ex:
            print("improve: %s" % ex.reason)
            return 2
        cands = candidates(records)
        blockers = queue_blockers(records, cands)
        if blockers:
            print("queue not empty (%d); mine runs when it is" % len(blockers))
            mine_ids = [i for i in blockers if records[i].get("project") == PROJECT]
            if mine_ids:
                print("blocked by %s: in project improve but no candidate owns it; close it by hand "
                      "(ledger status %s dropped) or finish it" % (", ".join(mine_ids), mine_ids[0]))
            return 4
        keys = []
        fkeys, why = failure_keys(now)
        keys += [("failure", k) for k in fkeys]
        if why == "no-heading":
            print("WARN failure-register has no Repeats heading; treated as empty")
            n_refused_src = 0
        elif why in ("stale-evidence", "evidence-outside-home"):
            print("REFUSE failure-register %s" % why)
            if not args.dry_run:
                emit("improve.refused", now, source="failure", key="-", reason=why)
            n_refused_src = 1
        else:
            n_refused_src = 0
        try:
            keys += [("target", n) for n, _ in load_targets(now)]
        except Refuse:
            pass
        keys += [("bugsweep", n) for n in bugsweep_keys()]
        proposed, refused = 0, 0
        for source, key in keys:
            dec, reason, detail = admit(source, key, now, args.dry_run, records, cands)
            if dec == "PROPOSE":
                proposed += 1
                print("PROPOSE %s %s" % (detail["cand"], detail.get("ledger_id", "(dry run)")))
                cands = candidates(ledger_records())
            elif reason not in ("below-threshold", "has-cause", "duplicate"):
                refused += 1
                print("REFUSE %s %s" % (key, reason))
                if not args.dry_run:
                    emit("improve.refused", now, source=source, key=key, reason=reason)
        refused += n_refused_src
        pending = sorted(c["cand"] for c in candidates(ledger_records()).values() if c["closed"] is None)
        if not proposed and pending:
            # An open candidate is work still to do (its evidence may still fire, or it is fixed but not closed).
            print("PENDING: %d open candidate(s), not idle (%s)" % (len(pending), ", ".join(pending[:3])))
            if not args.dry_run:
                emit("improve.pending", now, open=len(pending))
        elif not proposed:
            print("IDLE: nothing measured is failing" if not refused else "IDLE: nothing admitted (%d refused)" % refused)
            if not args.dry_run:
                emit("improve.idle", now, refused=refused)
                if not refused:
                    emit("loop.idle_declared", now)
        if not args.dry_run:
            with open(path_in_state("improve.tick"), "w") as fh:
                fh.write(iso(now) + "\n")
    return 0


def cmd_propose(args):
    now = now_s()
    ctx = locked() if not args.dry_run else contextlib.nullcontext()
    with ctx:
        try:
            records = ledger_records()
        except Refuse as ex:
            print("improve: %s" % ex.reason)
            return 2
        dec, reason, detail = admit(args.source, args.key, now, args.dry_run, records, candidates(records))
    if dec == "PROPOSE":
        print("PROPOSE %s %s" % (detail["cand"], detail.get("ledger_id", "(dry run)")))
        return 0
    print("REFUSE %s" % reason)
    if not args.dry_run:
        emit("improve.refused", now, source=args.source, key=args.key, reason=reason)
    return 5


def cmd_ask(args):
    now = now_s()
    try:
        records = ledger_records()
    except Refuse as ex:
        print("improve: %s" % ex.reason)
        return 2
    rec = records.get(args.id)
    if rec is None or rec["asked_by"] != "operator" or rec["status"] not in ("open", "active"):
        print("REFUSE not-operator-asked")
        return 5
    print("ALLOW operator-asked %s" % args.id)
    emit("improve.operator_admitted", now, ledger_id=args.id)
    return 0


def by_design_used(now):
    return sum(1 for e in read_jsonl("improve-candidates.jsonl")
               if e.get("event") == "closed" and e.get("mode") == "by-design" and now - e.get("t", 0) <= BY_DESIGN_WINDOW_S)


def measured_after(c, now):
    """Re-run the reader for a candidate. Returns (fixed, after dict) or raises Refuse."""
    before = c["before"]
    if not isinstance(before, dict) or "number" not in before:
        raise Refuse("no-before-reading")
    r = read_source(c["source"], c["key"], now, need_after=c["proposed"])
    if c["source"] != "failure":
        fixed = not fires(r)[0]
        return fixed, {"number": r["number"]}, r
    row = r["row"]
    if row is None:
        return True, {"number": 0}, r          # not in the window at all: quiet for the whole window
    if row["where"] == "below":
        raise Refuse("still-failing")
    last = row["last"]
    after = {"number": 0, "count_in_window": row["count"], "last": last}   # failures since the before reading
    if last is not None and before.get("last") is not None and last > before["last"]:
        raise Refuse("still-failing")           # a newer failure than the one we measured
    if row["pass_at"] is None or (before.get("last") is not None and row["pass_at"] <= before["last"]):
        raise Refuse("no-pass-since")           # no success seen after the last failure
    return True, after, r


def cmd_close(args):
    now = now_s()
    with locked():
        try:
            records = ledger_records()
        except Refuse as ex:
            print("improve: %s" % ex.reason)
            return 2
        cands = candidates(records)
        c = cands.get(args.cand)
        if c is None or c["closed"] is not None:
            print("REFUSE unknown-or-closed")
            return 5
        if args.by_design:
            why = re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f\x7f]", " ", args.by_design)).strip()
            if c["source"] != "failure" or len(why) < 10 or injected(why, free_text=True):
                print("REFUSE by-design-reason")
                return 5
            if by_design_used(now) >= BY_DESIGN_CAP:
                print("REFUSE by-design-cap")
                return 5
            append_jsonl("improve-candidates.jsonl", {"event": "closed", "cand": c["cand"], "t": now, "mode": "by-design",
                                                      "before": c["before"]})
            ledger_lib.append_event({"ts": ledger_lib.now_iso(), "id": c["ledger_id"], "event": "note",
                                     "note": "closed by-design: " + why[:120]})
            ledger_lib.append_event({"ts": ledger_lib.now_iso(), "id": c["ledger_id"], "event": "status", "status": "dropped"})
            print("CLOSED %s by-design" % c["cand"])
            return 0
        try:
            fixed, after, _ = measured_after(c, now)
        except Refuse as ex:
            print("REFUSE %s" % ex.reason)
            emit("improve.refused", now, cand=c["cand"], reason=ex.reason)
            return 5
        if not fixed:
            print("REFUSE still-failing")
            return 5
        append_jsonl("improve-candidates.jsonl", {"event": "closed", "cand": c["cand"], "t": now, "mode": "measured",
                                                  "before": c["before"], "after": after})
        proof = {"type": "measure", "ref": "improve:%s" % c["cand"],
                 "note": "before %s after %s (%s)" % (fmt_num(c["before"]["number"]), fmt_num(after["number"]), c["before"].get("path", ""))}
        ledger_lib.append_event({"ts": ledger_lib.now_iso(), "id": c["ledger_id"], "event": "proof", "proof": proof})
        ledger_lib.append_event({"ts": ledger_lib.now_iso(), "id": c["ledger_id"], "event": "status", "status": "done"})
        emit("improve.closed_measured", now, cand=c["cand"])
    print("CLOSED %s before %s after %s" % (c["cand"], fmt_num(c["before"]["number"]), fmt_num(after["number"])))
    return 0


def cmd_status(args):
    now = now_s()
    try:
        records = ledger_records()
    except Refuse as ex:
        print("improve: %s" % ex.reason)
        return 2
    cands = candidates(records)
    today = day_of(now)
    n_today = sum(1 for e in read_jsonl("improve-candidates.jsonl") if e.get("event") == "proposed" and day_of(e.get("t", 0)) == today)
    open_c = [c for c in cands.values() if c["closed"] is None]
    print("open %d/%d | today %d/%d" % (len(open_c), OPEN_CAP, n_today, DAILY_CAP))
    for c in sorted(open_c, key=lambda c: c["cand"]):
        print("  %s %s" % (c["cand"], c["ledger_id"]))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="improve")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("mine")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_mine)
    p = sub.add_parser("propose")
    p.add_argument("--source", required=True)
    p.add_argument("--key", required=True)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_propose)
    p = sub.add_parser("close")
    p.add_argument("cand")
    p.add_argument("--by-design", default=None, metavar="WHY")
    p.add_argument("--note", default=None, help="accepted and ignored: a note is not a measurement")
    p.set_defaults(fn=cmd_close)
    p = sub.add_parser("ask")
    p.add_argument("id")
    p.set_defaults(fn=cmd_ask)
    p = sub.add_parser("status")
    p.set_defaults(fn=cmd_status)
    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except OSError as ex:
        print("improve: %s" % ex.__class__.__name__)
        return 2


if __name__ == "__main__":
    sys.exit(main())
