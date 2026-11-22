#!/usr/bin/env python3
"""gate-metrics: read-only numbers from gate verdict files and the gate-spawns index.

  python3 gate_metrics.py [--root DIR] [--index FILE] [--target PCT] [--json]

Verdict files (the convention in bug-gate.md): any file named VERDICT.md or *-VERDICT.md under
$HUBKIT_HOME/bug-gate (or --root), up to 4 folders deep, symlinks ignored. Header lines, first 12 only:
  VERDICT: PENDING|PASS|PASS WITH NOTES|FAIL     (required; PENDING and anything else are not counted)
  TICKET: L-0001        SLICE: S7        ROUND: 1   (TICKET required; SLICE and ROUND optional, ROUND defaults to 1)
A chain is one (ticket, slice). Its FIRST gate is the lowest ROUND (oldest file breaks a tie).
Headline: first-gate FAIL share = chains whose first gate FAILED / chains with a counted first gate.
Spawns: lines of the gate-spawns index ($GATE_SPAWNS_LOG, default $HUBKIT_HOME/state/gate-spawns.jsonl);
corrupt lines (including a subagent_type that is not a string) are counted and skipped.
An unreadable root or index (missing, a folder, no permission) prints one line to stderr and reads as empty.
Exit 0 normally; exit 1 only when --target is given and the share is over it. Python 3.9, standard library only.
"""
import argparse
import json
import os
import re
import sys

COUNTED = ("PASS", "PASS WITH NOTES", "FAIL")
HEAD = re.compile(r"^\W*(VERDICT|TICKET|SLICE|ROUND)\s*:\s*(.*?)\s*$", re.I)
TICKET = re.compile(r"^L-\d{3,5}$")


def home():
    return os.path.expanduser(os.environ.get("HUBKIT_HOME") or "~/.claude/hubkit")


def verdict_files(root):
    out = []
    base = root.rstrip(os.sep).count(os.sep)
    for dp, dn, fn in os.walk(root, followlinks=False):
        if dp.count(os.sep) - base >= 4:
            dn[:] = []
        for f in fn:
            p = os.path.join(dp, f)
            if (f == "VERDICT.md" or f.endswith("-VERDICT.md")) and not os.path.islink(p):
                out.append(p)
    return sorted(out)


def parse(path):
    """-> dict(verdict, ticket, slice, round, mtime) or None when unreadable or not a counted verdict."""
    try:
        with open(path, errors="replace") as fh:
            lines = [next(fh, "") for _ in range(12)]
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    h = {}
    for ln in lines:
        m = HEAD.match(ln)
        if m and m.group(1).upper() not in h:
            h[m.group(1).upper()] = m.group(2)
    v = re.sub(r"\W+$", "", (h.get("VERDICT") or "").upper()).strip()
    t = (h.get("TICKET") or "").upper()
    if v not in COUNTED or not TICKET.match(t):
        return {"verdict": v or "NONE", "counted": False, "mtime": mtime}
    r = re.match(r"\d{1,3}", h.get("ROUND") or "")
    return {"verdict": v, "counted": True, "ticket": t, "slice": (h.get("SLICE") or "").upper(),
            "round": int(r.group(0)) if r else 1, "mtime": mtime}


def spawns(index):
    n = {"bug-gate": 0, "builder": 0, "corrupt": 0}
    try:
        with open(index, errors="replace") as fh:
            for ln in fh:
                if not ln.strip():
                    continue
                try:
                    t = json.loads(ln).get("subagent_type")
                except (ValueError, AttributeError):
                    n["corrupt"] += 1
                    continue
                if t is not None and not isinstance(t, str):
                    n["corrupt"] += 1
                    continue
                if t in n:
                    n[t] += 1
    except OSError as e:
        print("gate_metrics: could not read index %s (%s)" % (index, e.strerror or e.__class__.__name__),
              file=sys.stderr)
    return n


def compute(root, index):
    gates = [g for g in (parse(p) for p in verdict_files(root)) if g]
    counted = [g for g in gates if g["counted"]]
    chains = {}
    for g in counted:
        chains.setdefault((g["ticket"], g["slice"]), []).append(g)
    firsts = [min(c, key=lambda g: (g["round"], g["mtime"])) for c in chains.values()]
    fails = sum(1 for g in firsts if g["verdict"] == "FAIL")
    return {"gates": len(counted), "not_counted": len(gates) - len(counted), "chains": len(chains),
            "first_gate_fails": fails, "first_gate_total": len(firsts),
            "first_gate_fail_pct": round(100.0 * fails / len(firsts), 1) if firsts else None,
            "spawns": spawns(index)}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--root")
    ap.add_argument("--index")
    ap.add_argument("--target", type=float)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    root = a.root or os.path.join(home(), "bug-gate")
    index = a.index or os.environ.get("GATE_SPAWNS_LOG") or os.path.join(home(), "state", "gate-spawns.jsonl")
    if not (os.path.isdir(root) and os.access(root, os.R_OK | os.X_OK)):
        print("gate_metrics: could not read root %s (not a readable folder)" % root, file=sys.stderr)
    r = compute(root, index)
    pct = r["first_gate_fail_pct"]
    over = a.target is not None and pct is not None and pct > a.target
    if a.json:
        print(json.dumps(dict(r, over_target=over), sort_keys=True))
    else:
        s = r["spawns"]
        print("gates %d (%d not counted) in %d chains" % (r["gates"], r["not_counted"], r["chains"]))
        print("first-gate FAIL share: %s" % ("none yet" if pct is None else
              "%d/%d = %s%%" % (r["first_gate_fails"], r["first_gate_total"], pct)))
        print("spawns: bug-gate %d, builder %d (%d corrupt index lines)" % (s["bug-gate"], s["builder"], s["corrupt"]))
        if over:
            print("OVER TARGET %s%%" % a.target)
    return 1 if over else 0


if __name__ == "__main__":
    sys.exit(main())
