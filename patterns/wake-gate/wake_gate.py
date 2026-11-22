#!/usr/bin/env python3
"""wake-gate: decides whether a script may wake the operator, or only log.

A finding is {kind, severity, summary, source} (optional: dead_lane_asks, subject).
  WAKE  only if the key "kind|source[|subject]" has not woken in the last 24 h AND
        (severity is exactly "crit" OR the finding is a dead lane holding operator asks).
  LOG   everything else: info and warn never wake, a repeat inside 24 h never wakes.

  python3 wake_gate.py --kind K --source S --severity crit --summary "one line" [--dead-lane-asks] [--subject X]
Exit 0 and prints "WAKE <key>"; exit 1 and prints "LOG <key> (reason)". Never crashes, never raises.
If the memory file cannot be written the verdict is LOG (a wake that cannot be remembered would
repeat on every call) and the finding is printed loudly on stderr so a crit is never silently lost.

State: $HUBKIT_HOME/state/wake-seen.json  {key: epoch of last WAKE}, written atomically.
Event: one `wake.decision` line per call in $HUBKIT_HOME/state/events.jsonl.
Env (tests): WAKE_GATE_NOW fixed clock (epoch seconds). Python 3.9, standard library only.
"""
import argparse
import contextlib
import fcntl
import json
import os
import re
import sys
import tempfile
import time

WINDOW_S = 24 * 3600
KEEP_S = 7 * 24 * 3600
DEAD_LANE_KIND = "dead-lane-holding-operator-asks"


def state_dir():
    home = os.environ.get("HUBKIT_HOME") or os.path.join(os.path.expanduser("~"), ".claude", "hubkit")
    return os.path.join(home, "state")


def one_line(text, limit):
    return re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f\x7f]", " ", str(text if text is not None else ""))).strip()[:limit]


def make_key(finding):
    parts = [one_line(finding.get("kind"), 80), one_line(finding.get("source"), 80)]
    if finding.get("subject"):
        parts.append(one_line(finding["subject"], 80))
    return "|".join(parts)


def emit(event, **fields):
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time())), "event": event}
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


def read_seen(path):
    """-> (dict key->epoch, corrupt flag). Missing file is fine; unreadable/corrupt is {} + flag."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}, False
    except (OSError, ValueError):
        return {}, True
    if not isinstance(data, dict):
        return {}, True
    clean = {}
    for k, v in data.items():
        if isinstance(k, str) and isinstance(v, (int, float)) and not isinstance(v, bool) and v == v:
            clean[k] = float(v)
    return clean, len(clean) != len(data)


def is_new(last, now):
    """True when this key has not woken within the window. A stamp in the future (clock moved
    backwards) still blocks while it is within one window of now; a far-future stamp is bogus."""
    if last is None:
        return True
    age = now - last
    if age >= 0:
        return age >= WINDOW_S
    return -age > WINDOW_S


def write_seen(path, seen):
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".wake-seen.", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(seen, fh, indent=1, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def gate(finding, now=None):
    """Decide one finding. Returns {"verdict": "WAKE"|"LOG", "key": str, "reason": str}. Never raises."""
    try:
        return _gate(finding, now)
    except Exception as e:  # last resort: never crash a caller, never swallow a crit
        key = "?"
        try:
            key = make_key(finding)
            print("wake-gate: UNRECORDED %s: %s (internal error %s)" % (
                one_line(finding.get("severity"), 10), one_line(finding.get("summary"), 200), e.__class__.__name__),
                file=sys.stderr)
        except Exception:
            pass
        return {"verdict": "LOG", "key": key, "reason": "internal error"}


def _gate(finding, now):
    if not isinstance(finding, dict):
        finding = {}
    if now is None:
        env = os.environ.get("WAKE_GATE_NOW")
        try:
            now = float(env) if env else time.time()
        except ValueError:
            now = time.time()
    key = make_key(finding)
    sev = one_line(finding.get("severity"), 10).lower()
    summary = one_line(finding.get("summary"), 300)
    dead = bool(finding.get("dead_lane_asks")) or one_line(finding.get("kind"), 80) == DEAD_LANE_KIND
    eligible = sev == "crit" or dead
    path = os.path.join(state_dir(), "wake-seen.json")
    verdict, reason = "LOG", ""
    if not eligible:
        reason = "severity %s never wakes" % (sev or "unset")
    else:
        try:
            os.makedirs(state_dir(), exist_ok=True)
            with open(os.path.join(state_dir(), "wake-seen.json.lock"), "a") as lockfh:
                fcntl.flock(lockfh, fcntl.LOCK_EX)
                seen, corrupt = read_seen(path)
                if corrupt:
                    print("wake-gate: wake-seen.json was corrupt; ignoring bad entries", file=sys.stderr)
                if not is_new(seen.get(key), now):
                    reason = "already woke inside 24h"
                else:
                    seen[key] = now
                    seen = {k: v for k, v in seen.items() if -KEEP_S < now - v < KEEP_S}
                    write_seen(path, seen)
                    verdict, reason = "WAKE", "new crit key" if sev == "crit" else "dead lane holding operator asks"
        except (OSError, ValueError) as e:
            reason = "memory unwritable (%s); not waking" % (getattr(e, "strerror", None) or e.__class__.__name__)
            print("wake-gate: UNRECORDED %s %s: %s" % (sev, key, summary), file=sys.stderr)
    emit("wake.decision", verdict=verdict, key=key, reason=reason, severity=sev,
         source=one_line(finding.get("source"), 80), summary=summary[:120])
    return {"verdict": verdict, "key": key, "reason": reason}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Decide WAKE or LOG for one finding.")
    ap.add_argument("--kind", required=True)
    ap.add_argument("--source", default="script")
    ap.add_argument("--severity", default="info")
    ap.add_argument("--summary", default="")
    ap.add_argument("--subject", default="")
    ap.add_argument("--dead-lane-asks", action="store_true")
    try:
        a = ap.parse_args(argv)
    except SystemExit as e:
        return 2 if e.code else 0
    r = gate({"kind": a.kind, "source": a.source, "severity": a.severity, "summary": a.summary or a.kind,
              "subject": a.subject, "dead_lane_asks": a.dead_lane_asks})
    if r["verdict"] == "WAKE":
        print("WAKE %s" % r["key"])
        return 0
    print("LOG %s (%s)" % (r["key"], r["reason"]))
    return 1


if __name__ == "__main__":
    sys.exit(main())
