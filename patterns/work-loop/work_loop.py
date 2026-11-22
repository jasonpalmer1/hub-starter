#!/usr/bin/env python3
"""work-loop: hand an idle lane its next ticket, never one it must not take.

    python3 work_loop.py next --lane <name> [--peek] [--json]
    python3 work_loop.py lanes            list the lane registry
    python3 work_loop.py tick [--dry-run] feed every idle lane (run this every ~10 min)
    python3 work_loop.py touch --lane <name>    "this lane is active now" (call from a hook)
    python3 work_loop.py lane add|remove|list ...   the lane registry
    python3 work_loop.py hold --lane <name> --minutes N | --clear

A lane is one long-lived worker chat. `next` reads the ledger (the `ledger`
pattern's file), picks one open ticket for that lane, records the offer, and
prints the id. Exit 0 = a ticket, 3 = nothing to hand out, 2 = bad usage.

A ticket is NEVER returned when it is: owned by another lane, active or
claimed, needs-operator, done or dropped, carrying a trigger (it waits for an
outside event), or already offered to a lane in the last 24 hours.
Order: tickets owned by this lane, then unowned ones; within each, tickets the
operator asked for first; ties by ticket number.

State (all under $HUBKIT_HOME/state, default ~/.claude/hubkit):
  lanes.json      optional registry (see README). Absent = any lane name is fine.
  loop-feed.json  who was offered what, and when (24 h memory).
  loop-feed.json.lock  flock, so two callers in the same second get two tickets.
  touch/<lane>    epoch of the lane's last sign of life.   holds/<lane>.json  {"until": epoch}
  inbox/<lane>.txt  the file adapter's delivery.   events.jsonl  one JSON line per loop event.
  loop-tick.json  starvation / idle-declared memory.  findings/  starvation findings, each also sent through wake-gate.
  wake-seen.json  wake-gate's memory (written by the wake-gate pattern, when installed).
  tick.lock       flock: two overlapping ticks never both feed.
Python 3.9, standard library only. POSIX only (fcntl).
"""
import argparse
import contextlib
import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (HERE, os.path.join(HERE, "..", "ledger")):
    if os.path.isfile(os.path.join(_p, "ledger_lib.py")):
        sys.path.insert(0, _p)
        break
import ledger_lib  # noqa: E402  (the ledger pattern's fold; same file format)

OFFER_WINDOW_S = 24 * 3600
ID_RE = re.compile(r"^L-(\d+)$")
EXIT_NONE = 3
FEED_MIN_IDLE_S = 10 * 60      # a lane must be idle MORE than... at least this long to be fed
FEED_RATE_S = 30 * 60          # one feed per lane per 30 min
STARVED_S = 30 * 60            # idle this long, open work exists, nothing fed: starvation
STARVE_ONCE_S = 24 * 3600      # one starvation finding per lane per 24 h
IDLE_ONCE_S = 24 * 3600
BEAT_MAX_AGE_S = 30 * 60       # the tick's deadman limit (3 missed ticks)
LANE_RE = re.compile(r"^[a-z0-9-]{1,32}$")
ARG_RE = re.compile(r"^[A-Za-z0-9_./:=+@%,-]{1,200}$")   # delivery argv words: no shell syntax, ever


def home():
    return os.environ.get("HUBKIT_HOME") or os.path.join(os.path.expanduser("~"), ".claude", "hubkit")


def state_dir():
    return os.path.join(home(), "state")


def ledger_path():
    return os.environ.get("LEDGER_PATH") or os.path.join(state_dir(), "ledger.jsonl")


def now_s():
    raw = os.environ.get("WORK_LOOP_NOW")  # tests only: a fixed clock
    return float(raw) if raw else time.time()


def norm(name):
    """Lane and owner names compare trimmed and lower-case: 'Lane-A ' == 'lane-a'."""
    return str(name or "").strip().lower()


def read_events_tolerant(path):
    """Events in file order. A bad line is skipped and counted, never fatal."""
    events, bad = [], []
    try:
        fh = open(path, "r", encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return events, bad
    with fh:
        for n, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except ValueError:
                bad.append(n)
                continue
            if isinstance(value, dict):
                events.append(value)
            else:
                bad.append(n)
    return events, bad


def load_registry():
    """None when there is no registry file; else {lane: entry}. Raises ValueError if it is unreadable."""
    path = os.path.join(state_dir(), "lanes.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        lanes = data["lanes"]
        if not isinstance(lanes, dict):
            raise TypeError("lanes must be an object")
    except (ValueError, KeyError, TypeError, OSError) as e:
        raise ValueError("lanes.json is unreadable (%s); fix or delete it" % e)
    return {norm(k): (v if isinstance(v, dict) else {}) for k, v in lanes.items()}


def lane_names(lane, entry):
    """Every owner string that means 'this lane': its name, name minus '-lane', registry aliases."""
    names = {lane}
    if lane.endswith("-lane"):
        names.add(lane[:-5])
    for a in (entry or {}).get("aliases", []) or []:
        names.add(norm(a))
    return names


def id_key(tid):
    m = ID_RE.match(tid)
    return (0, int(m.group(1)), tid) if m else (1, 0, tid)


def read_feed(path):
    """(offers, last_feed). A missing or unreadable file is empty, never fatal."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        sent = data.get("sent", {})
        last = data.get("last_feed", {})
        return (sent if isinstance(sent, dict) else {}), (last if isinstance(last, dict) else {})
    except (OSError, ValueError, AttributeError):
        return {}, {}


def read_offers(path):
    return read_feed(path)[0]


def valid_lane(name):
    return bool(LANE_RE.match(name or ""))


def valid_argv(argv):
    return (isinstance(argv, list) and 0 < len(argv) <= 20
            and all(isinstance(a, str) and ARG_RE.match(a) for a in argv))


def clean_text(text, n=80):
    """One printable line, no control characters (ticket titles are untrusted text)."""
    return "".join(ch if ch.isprintable() else " " for ch in str(text or ""))[:n].strip()


def emit(event, **fields):
    """One JSON line in events.jsonl (O_APPEND). Telemetry never breaks the loop."""
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now_s())), "event": event}
    rec.update(fields)
    try:
        os.makedirs(state_dir(), exist_ok=True)
        fd = os.open(os.path.join(state_dir(), "events.jsonl"), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, (json.dumps(rec, sort_keys=True) + "\n").encode("utf-8"))
        finally:
            os.close(fd)
    except OSError:
        pass


def write_atomic(path, obj):
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".loop-feed.", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, indent=1, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def eligible(tid, rec, mine, projects, offers, now):
    """None = this lane must not take the ticket; else True if the lane owns it, False if unowned."""
    if not ID_RE.match(str(tid)):
        return None
    if rec.get("status") != "open":          # active/claimed, needs-operator, done, dropped
        return None
    if rec.get("trigger"):                   # waits for an outside event
        return None
    owner = norm(rec.get("owner"))
    own = owner in mine
    if not own and owner not in ("", "unowned"):   # someone else's ticket
        return None
    if projects and rec.get("project") and norm(rec.get("project")) not in projects:
        return None
    off = offers.get(tid)
    if isinstance(off, dict) and now - float(off.get("at", 0) or 0) < OFFER_WINDOW_S:
        return None                          # offered recently (to any lane)
    return own


def pick(records, lane, entry, offers, now):
    """The decision rule, pure. records = folded ledger; offers = {ticket: {lane, at}}."""
    mine = lane_names(lane, entry)
    projects = {norm(p) for p in (entry or {}).get("projects", []) or []}
    ok = []
    for tid, rec in records.items():
        own = eligible(tid, rec, mine, projects, offers, now)
        if own is None:
            continue
        ok.append((0 if own else 1, 0 if rec.get("asked_by") == "operator" else 1, id_key(tid), tid))
    ok.sort()
    return ok[0][3] if ok else None


def cmd_next(args):
    lane = norm(args.lane)
    if not lane:
        print("work-loop: --lane needs a name", file=sys.stderr)
        return 2
    if not valid_lane(lane):
        print("work-loop: lane names are [a-z0-9-], 1 to 32 characters", file=sys.stderr)
        return 2
    try:
        registry = load_registry()
    except ValueError as e:
        print("work-loop: %s" % e, file=sys.stderr)
        return 2
    entry = None
    if registry is not None:
        if lane not in registry:
            print("work-loop: lane '%s' is not in lanes.json (known: %s)" % (lane, ", ".join(sorted(registry)) or "none"),
                  file=sys.stderr)
            return 2
        entry = registry[lane]
    feed = os.path.join(state_dir(), "loop-feed.json")
    os.makedirs(state_dir(), exist_ok=True)
    with open(feed + ".lock", "a") as lockfh:
        fcntl.flock(lockfh, fcntl.LOCK_EX)   # read, decide and record as one step
        events, bad = read_events_tolerant(ledger_path())
        if bad:
            print("work-loop: skipped %d unreadable ledger line(s), first at line %d" % (len(bad), bad[0]), file=sys.stderr)
        records = ledger_lib.fold(events)
        offers, last_feed = read_feed(feed)
        time.sleep(float(os.environ.get("WORK_LOOP_HOLD_S") or 0))  # tests only: widen the race window
        now = now_s()
        tid = pick(records, lane, entry, offers, now)
        if tid and not args.peek:
            offers[tid] = {"lane": lane, "at": now}
            offers = {k: v for k, v in offers.items()
                      if isinstance(v, dict) and now - float(v.get("at", 0) or 0) < OFFER_WINDOW_S}
            write_atomic(feed, {"sent": offers, "last_feed": last_feed})
            emit("loop.next", lane=lane, ticket=tid, via="next")
    if not tid:
        if args.json:
            print(json.dumps({"ticket": None, "lane": lane, "skipped_lines": len(bad)}))
        else:
            print("work-loop: nothing to hand out to %s" % lane, file=sys.stderr)
        return EXIT_NONE
    if args.json:
        print(json.dumps({"ticket": tid, "lane": lane, "title": records[tid].get("title", ""), "skipped_lines": len(bad)}))
    else:
        print(tid)
    return 0


def cmd_lanes(args):
    try:
        registry = load_registry()
    except ValueError as e:
        print("work-loop: %s" % e, file=sys.stderr)
        return 2
    if registry is None:
        print("no lanes.json: any lane name is accepted")
        return 0
    for name in sorted(registry):
        print(name)
    return 0


# ---------------------------------------------------------------- lane registry

def save_registry(lanes):
    write_atomic(os.path.join(state_dir(), "lanes.json"), {"lanes": lanes})


@contextlib.contextmanager
def locked(name, blocking=True):
    """flock on state/<name>; yields False (not entered) when non-blocking and busy."""
    os.makedirs(state_dir(), exist_ok=True)
    with open(os.path.join(state_dir(), name), "a") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        yield True


def lane_arg(args):
    lane = norm(args.lane)
    if not valid_lane(lane):
        print("work-loop: lane names are [a-z0-9-], 1 to 32 characters", file=sys.stderr)
        return None
    return lane


def cmd_lane(args):
    try:
        registry = load_registry()
    except ValueError as e:
        print("work-loop: %s" % e, file=sys.stderr)
        return 2
    if args.op == "list":
        for name in sorted(registry or {}):
            d = (registry[name].get("deliver") or {}).get("type", "file")
            print("%s\t%s" % (name, d))
        return 0
    lane = norm(args.name)
    if not valid_lane(lane):
        print("work-loop: lane names are [a-z0-9-], 1 to 32 characters", file=sys.stderr)
        return 2
    with locked("lanes.json.lock"):
        registry = load_registry() or {}
        if args.op == "add":
            if lane in registry:
                print("work-loop: lane '%s' already exists" % lane, file=sys.stderr)
                return 2
            entry = {}
            if args.cmd:
                if not valid_argv(args.cmd):
                    print("work-loop: --cmd words may only use letters, digits and _./:=+@%,- (no shell syntax)", file=sys.stderr)
                    return 2
                entry["deliver"] = {"type": "cmd", "argv": list(args.cmd)}
            if args.alias:
                entry["aliases"] = [norm(a) for a in args.alias]
            if args.project:
                entry["projects"] = [norm(p) for p in args.project]
            registry[lane] = entry
            save_registry(registry)
            print("added %s" % lane)
        else:
            if lane not in registry:
                print("work-loop: no lane '%s'" % lane, file=sys.stderr)
                return 2
            del registry[lane]
            save_registry(registry)
            for sub, suffix in (("touch", ""), ("holds", ".json"), ("inbox", ".txt")):
                with contextlib.suppress(OSError):
                    os.unlink(os.path.join(state_dir(), sub, lane + suffix))
            print("removed %s" % lane)
    return 0


def cmd_touch(args):
    lane = lane_arg(args)
    if lane is None:
        return 2
    try:
        registry = load_registry()
    except ValueError as e:
        print("work-loop: %s" % e, file=sys.stderr)
        return 2
    if registry is not None and lane not in registry:
        print("work-loop: lane '%s' is not in lanes.json" % lane, file=sys.stderr)
        return 2
    d = os.path.join(state_dir(), "touch")
    os.makedirs(d, exist_ok=True)
    write_atomic(os.path.join(d, lane), {"at": now_s()})
    return 0


def read_touch(lane):
    try:
        with open(os.path.join(state_dir(), "touch", lane), "r", encoding="utf-8") as fh:
            return float(json.load(fh)["at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def read_hold(lane):
    """(until_epoch or None, unreadable). A hold file we cannot read counts as a hold."""
    p = os.path.join(state_dir(), "holds", lane + ".json")
    if not os.path.exists(p):
        return None, False
    try:
        with open(p, "r", encoding="utf-8") as fh:
            until = float(json.load(fh)["until"])
        if until != until or until in (float("inf"), float("-inf")):   # NaN or infinity is not a date
            return None, True
        return until, False
    except (OSError, ValueError, KeyError, TypeError):
        return None, True


def cmd_hold(args):
    lane = lane_arg(args)
    if lane is None:
        return 2
    p = os.path.join(state_dir(), "holds", lane + ".json")
    if args.clear:
        with contextlib.suppress(OSError):
            os.unlink(p)
        print("hold cleared for %s" % lane)
        return 0
    if args.minutes is None or not (0 < args.minutes <= 7 * 24 * 60):
        print("work-loop: hold needs --minutes 1..10080 (a hold always expires) or --clear", file=sys.stderr)
        return 2
    until = now_s() + args.minutes * 60
    write_atomic(p, {"until": until, "reason": clean_text(args.reason)})
    print("held %s for %d min" % (lane, args.minutes))
    return 0


# ---------------------------------------------------------------- tick

def gate(now, touch_at, last_feed_at, hold_until, hold_bad):
    """Pure: may this lane be fed right now? Returns (ok, reason)."""
    if hold_bad or (hold_until is not None and now < hold_until):
        return False, "held"
    if touch_at is None:
        return False, "never touched"
    if touch_at > now:
        return False, "touch is in the future (clock skew)"
    if now - touch_at < FEED_MIN_IDLE_S:
        return False, "active %dm ago" % ((now - touch_at) // 60)
    if last_feed_at is not None and now - last_feed_at < FEED_RATE_S:
        return False, "fed %dm ago" % ((now - last_feed_at) // 60)
    return True, "idle"


def deliver(lane, entry, tid, title):
    """Returns (ok, adapter, note). file = state/inbox/<lane>.txt; cmd = operator argv, no shell."""
    d = (entry or {}).get("deliver") or {}
    if d.get("type") == "cmd":
        argv = d.get("argv")
        if not valid_argv(argv):
            return False, "cmd", "argv refused (shell syntax or not a list)"
        env = dict(os.environ, WORK_LOOP_LANE=lane, WORK_LOOP_TICKET=tid, WORK_LOOP_TITLE=clean_text(title))
        try:
            r = subprocess.run(argv, shell=False, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, env=env, timeout=20)
        except (OSError, subprocess.SubprocessError) as e:
            return False, "cmd", "could not run (%s)" % e.__class__.__name__
        return r.returncode == 0, "cmd", "exit %d" % r.returncode
    inbox = os.path.join(state_dir(), "inbox")
    try:
        os.makedirs(inbox, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".inbox.", dir=inbox)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("%s\t%s\n" % (tid, clean_text(title)))
        os.replace(tmp, os.path.join(inbox, lane + ".txt"))
    except OSError as e:
        return False, "file", "could not write inbox (%s)" % e.strerror
    return True, "file", "inbox"


def load_wake_gate():
    """The wake-gate pattern installed next to this one (or in the repo). None if absent."""
    for d in (os.path.join(HERE, "..", "wake-gate"), os.path.join(HERE, "wake-gate")):
        if os.path.isfile(os.path.join(d, "wake_gate.py")):
            if d not in sys.path:
                sys.path.insert(0, d)
            try:
                import wake_gate
                return wake_gate
            except Exception:
                return None
    return None


def raise_finding(kind, lane, detail, now):
    """Record a finding file in state/findings/ and send it through wake-gate (WAKE or LOG, once per
    key per 24 h). Without wake-gate installed the file is still written and nothing wakes anyone."""
    verdict = "NO-GATE"
    wg = load_wake_gate()
    if wg is not None:
        # starvation is a real failure (a lane idle with work waiting), so it is critical; wake-gate dedups
        r = wg.gate({"kind": kind, "source": lane, "severity": "crit", "summary": "%s %s: %s" % (kind, lane, detail)},
                    now=now)
        verdict = r["verdict"]
        print("wake-gate %s: %s" % (verdict, r["key"]))
    d = os.path.join(state_dir(), "findings")
    os.makedirs(d, exist_ok=True)
    write_atomic(os.path.join(d, "%s-%s.json" % (kind, lane)),
                 {"kind": kind, "key": "%s:%s" % (kind, lane), "lane": lane, "severity": "crit",
                  "detail": detail, "at": now, "wake_gate": verdict})


def read_tick_state():
    try:
        with open(os.path.join(state_dir(), "loop-tick.json"), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def feed_one(lane, entry, dry, now):
    """Pick, re-check, deliver, record - one lane. Returns (fed, ticket, note, open_exists)."""
    feed = os.path.join(state_dir(), "loop-feed.json")
    ctx = contextlib.nullcontext(True) if dry else locked("loop-feed.json.lock")
    with ctx:
        events, bad = read_events_tolerant(ledger_path())
        records = ledger_lib.fold(events)
        offers, last_feed = read_feed(feed)
        open_exists = any(ID_RE.match(str(t)) and r.get("status") == "open" and not r.get("trigger")
                          for t, r in records.items())
        tid = pick(records, lane, entry, offers, now)
        if not tid:
            return False, None, "no ticket for this lane", open_exists
        title = records[tid].get("title", "")
        if dry:
            return False, tid, "would feed", open_exists
        time.sleep(float(os.environ.get("WORK_LOOP_PRESEND_SLEEP_S") or 0))  # tests only: widen pick->send gap
        # a ticket can be claimed between pick and send: re-read the ledger and confirm
        events2, _ = read_events_tolerant(ledger_path())
        rec2 = ledger_lib.fold(events2).get(tid, {})
        mine = lane_names(lane, entry)
        projects = {norm(p) for p in (entry or {}).get("projects", []) or []}
        if eligible(tid, rec2, mine, projects, offers, now) is None:
            return False, tid, "ticket was taken before send", open_exists
        ok, adapter, note = deliver(lane, entry, tid, title)
        if not ok:
            emit("loop.feed_failed", lane=lane, ticket=tid, adapter=adapter, note=note)
            return False, tid, "delivery failed: " + note, open_exists
        offers[tid] = {"lane": lane, "at": now}
        offers = {k: v for k, v in offers.items()
                  if isinstance(v, dict) and now - float(v.get("at", 0) or 0) < OFFER_WINDOW_S}
        last_feed[lane] = now
        write_atomic(feed, {"sent": offers, "last_feed": last_feed})
        emit("loop.next", lane=lane, ticket=tid, via="tick")
        emit("loop.feed", lane=lane, ticket=tid, adapter=adapter)
        return True, tid, "fed via " + adapter, open_exists


def run_tick(registry, dry, now):
    state = read_tick_state()
    starved_seen = state.get("starved", {}) if isinstance(state.get("starved"), dict) else {}
    idle_at = state.get("idle_declared")
    _, last_feed = read_feed(os.path.join(state_dir(), "loop-feed.json"))
    changed = False
    idle_lanes = []
    for lane in sorted(registry):
        entry = registry[lane]
        if not valid_lane(lane):
            print("skip %s: bad lane name" % ascii(lane)[:40])
            continue
        hold_until, hold_bad = read_hold(lane)
        touch_at = read_touch(lane)
        lf = last_feed.get(lane)
        lf = float(lf) if isinstance(lf, (int, float)) else None
        ok, why = gate(now, touch_at, lf, hold_until, hold_bad)
        if not ok:
            print("skip %s: %s" % (lane, why))
            if touch_at is not None and touch_at > now:
                print("  note: %s touch is ahead of the clock" % lane)
            continue
        fed, tid, note, open_exists = feed_one(lane, entry, dry, now)
        print("%s %s%s: %s" % ("would-feed" if dry and tid else ("fed" if fed else "no-feed"), lane,
                               " " + tid if tid else "", note))
        if fed or (dry and tid):
            continue
        if not open_exists:
            idle_lanes.append(lane)
        elif now - touch_at >= STARVED_S:
            seen = starved_seen.get(lane)
            if isinstance(seen, (int, float)) and now - seen < STARVE_ONCE_S:
                continue
            print("STARVED %s: idle %dm, open work, nothing fed" % (lane, (now - touch_at) // 60))
            if not dry:
                raise_finding("starved", lane, "idle %dm with open tickets; %s" % ((now - touch_at) // 60, note), now)
                emit("loop.starved", lane=lane, idle_min=int((now - touch_at) // 60), why=note)
                starved_seen[lane] = now
                changed = True
    if idle_lanes and not (isinstance(idle_at, (int, float)) and now - idle_at < IDLE_ONCE_S):
        print("IDLE: no open tickets for %s" % ", ".join(idle_lanes))
        if not dry:
            emit("loop.idle_declared", lanes=idle_lanes)
            idle_at = now
            changed = True
    if changed:
        write_atomic(os.path.join(state_dir(), "loop-tick.json"), {"starved": starved_seen, "idle_declared": idle_at})


def cmd_tick(args):
    try:
        registry = load_registry()
    except ValueError as e:
        print("work-loop: %s" % e, file=sys.stderr)
        return 2
    dry = args.dry_run
    now = now_s()
    if dry:    # read-only: no locks, no files, no events
        if not registry:
            print("no lanes registered")
            return 0
        run_tick(registry, True, now)
        return 0
    with locked("tick.lock", blocking=False) as got:
        if not got:
            print("another tick is running; nothing done")
            return 0
        if registry:
            run_tick(registry, False, now)
        else:
            print("no lanes registered")
        # deadman beat, same files the deadman pattern reads (state/heartbeats, state/registry)
        for sub, text in (("heartbeats", "%d %d\n" % (now, BEAT_MAX_AGE_S)), ("registry", "%d\n" % BEAT_MAX_AGE_S)):
            d = os.path.join(state_dir(), sub)
            os.makedirs(d, exist_ok=True)
            tmp = os.path.join(d, "work-loop-tick.tmp-%d" % os.getpid())
            with open(tmp, "w") as fh:
                fh.write(text)
            os.replace(tmp, os.path.join(d, "work-loop-tick"))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="work_loop.py", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd")
    n = sub.add_parser("next", help="print the next ticket id for a lane (exit 3 = none)")
    n.add_argument("--lane", required=True)
    n.add_argument("--peek", action="store_true", help="choose but do not record the offer")
    n.add_argument("--json", action="store_true")
    n.set_defaults(fn=cmd_next)
    l = sub.add_parser("lanes", help="list the lane registry")
    l.set_defaults(fn=cmd_lanes)
    t = sub.add_parser("tick", help="feed every idle lane once (schedule every ~10 min)")
    t.add_argument("--dry-run", action="store_true", help="say what would happen; write nothing")
    t.set_defaults(fn=cmd_tick)
    th = sub.add_parser("touch", help="record that a lane is active now (call from a hook)")
    th.add_argument("--lane", required=True)
    th.set_defaults(fn=cmd_touch)
    h = sub.add_parser("hold", help="stop feeding a lane until a time, or clear the hold")
    h.add_argument("--lane", required=True)
    h.add_argument("--minutes", type=int)
    h.add_argument("--reason", default="")
    h.add_argument("--clear", action="store_true")
    h.set_defaults(fn=cmd_hold)
    ln = sub.add_parser("lane", help="lane registry: add NAME [--cmd WORD ...] | remove NAME | list")
    ln.add_argument("op", choices=["add", "remove", "list"])
    ln.add_argument("name", nargs="?", default="")
    ln.add_argument("--cmd", action="append", help="delivery program and arguments, one word per --cmd (no shell)")
    ln.add_argument("--alias", action="append")
    ln.add_argument("--project", action="append")
    ln.set_defaults(fn=cmd_lane)
    args = ap.parse_args(argv)
    if not getattr(args, "fn", None):
        ap.print_help()
        return 2
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
