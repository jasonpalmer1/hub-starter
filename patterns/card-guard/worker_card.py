#!/usr/bin/env python3
"""worker-card: a worker files its OWN decision card, through the card guard, with a cap.

  worker_card.py --ticket L-0001 --header "One line question" --why "what happens if the recommended answer is picked" \
      --option "Recommended answer|what it does" --option "Other answer|what it does" --recommended 0 \
      [--proof "https://... or plain words"] [--run RUNID]

Adds to the guard: a cap of 2 cards per worker run (cards older than 12 hours stop counting), a refusal when the same
decision was already answered ($HUBKIT_HOME/state/answers.jsonl, rows with "header" and "answered_at"), an explicit
recommended option (shown first), and plain-ASCII / size checks. The card text is: the header, a Proof line, a Why line.
The card id is stable (ticket + header), so a retry never posts twice and never spends cap.
Cards are appended to $HUBKIT_HOME/state/cards.jsonl, for your own page or script to show.
Prints "asked <id>" then "Card: <id>" (paste the Card line in the worker's report). Exit 0 ok, 1 refused (reason on stderr).
Env: HUBKIT_HOME, WORKER_CARD_STATE (cap bookkeeping), WORKER_CARD_ANSWERS, WORKER_CARD_QUEUE, WORKER_CARD_RUN, CLAUDE_CODE_SESSION_ID
"""
import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)
import card_guard  # noqa: E402

HOME = os.environ.get("HUBKIT_HOME") or os.path.join(os.path.expanduser("~"), ".claude", "hubkit")
STATE_DIR = os.environ.get("WORKER_CARD_STATE") or os.path.join(HOME, "state", "worker-cards")
ANSWERS = os.environ.get("WORKER_CARD_ANSWERS") or os.path.join(HOME, "state", "answers.jsonl")
QUEUE = os.environ.get("WORKER_CARD_QUEUE") or os.path.join(HOME, "state", "cards.jsonl")
CAP = 2
WINDOW_S = 12 * 3600
MAX_HEADER, MAX_FIELD, MAX_TOTAL = 100, 400, 1200
TICKET_RE = re.compile(r"^L-\d{2,6}(?:-[A-Za-z0-9-]{1,30})?$")
STOP = {"a", "an", "the", "to", "for", "of", "and", "or", "is", "it", "we", "our", "do", "on", "in", "with", "this", "that", "now", "should"}


def norm(h):
    return " ".join(str(h or "").split()).casefold()


def words(h):
    return {w for w in re.findall(r"[a-z0-9]+", norm(h)) if w not in STOP}


def card_id(ticket, header):
    return "wc-" + hashlib.sha1((ticket + "|" + norm(header)).encode()).hexdigest()[:10]


def answered_duplicate(header, path=None):
    """Header of an answered decision that is the same question (equal text, or >=75% of the words shared), else None."""
    hw = words(header)
    try:
        f = open(path or ANSWERS, encoding="utf-8", errors="replace")
    except OSError:
        return None
    with f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if not isinstance(r, dict) or not r.get("answered_at"):
                continue
            h = r.get("header") or ""
            if norm(h) == norm(header):
                return h
            rw = words(h)
            if hw and rw and len(hw & rw) / len(hw | rw) >= 0.75:
                return h
    return None


def input_problem(a):
    """Reason string if the inputs would make a broken card, else None. Nothing typed is echoed back."""
    fields = [("header", a.header), ("why", a.why), ("proof", a.proof or "")] + [("option", o) for o in a.option]
    for name, v in fields:
        if v is None or not isinstance(v, str):
            return "missing " + name
        if any(ord(c) > 126 or ord(c) < 32 for c in v):
            return "%s has non-ASCII or control characters (write plain ASCII; no pasted symbols, quotes or newlines)" % name
    if not (a.header or "").strip():
        return "empty question (--header)"
    if not (a.why or "").strip():
        return "empty --why"
    if not (a.proof or "").strip():
        return "--proof is required (a link, or one plain sentence of facts)"
    pp = card_guard.proof_problem("Proof: " + (a.proof or "").strip())
    if pp:
        return pp
    if len(a.header) > MAX_HEADER:
        return "question longer than %d characters (one short line)" % MAX_HEADER
    for name, v in (("why", a.why), ("proof", a.proof or "")):
        if len(v) > MAX_FIELD:
            return "%s longer than %d characters" % (name, MAX_FIELD)
    if sum(len(x) for _, x in fields) > MAX_TOTAL:
        return "card too long (keep it to one line plus proof)"
    if not TICKET_RE.match(a.ticket or ""):
        return "--ticket must look like L-0001"
    if not 2 <= len(a.option) <= 3:
        return "give 2-3 --option values"
    for o in a.option:
        lab, sep, desc = o.partition("|")
        if not sep or not lab.strip() or not desc.strip():
            return "every --option must be 'label|what it does'"
        if len(lab) > 60 or len(desc) > 200:
            return "an option is too long (label 60, description 200 characters)"
    if a.recommended is None:
        return "no recommended option: give --recommended N (0 = first option) and say why in --why"
    if not 0 <= a.recommended < len(a.option):
        return "--recommended out of range"
    # --why must not argue for a different option than the one marked
    labs = [o.partition("|")[0].strip() for o in a.option]
    rp = card_guard.recommend_problem("Why: " + (a.why or ""), [{"label": l} for l in labs], a.recommended)
    if rp:
        return rp
    return None


def run_key(a):
    k = a.run or os.environ.get("WORKER_CARD_RUN") or ((os.environ.get("CLAUDE_CODE_SESSION_ID") or "nosession") + "-" + a.ticket)
    return re.sub(r"[^A-Za-z0-9_.-]", "_", k)[:80] or "run"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter, allow_abbrev=False)
    ap.add_argument("--ticket", default="")
    ap.add_argument("--header")
    ap.add_argument("--why")
    ap.add_argument("--option", action="append", default=[])
    ap.add_argument("--recommended", type=int, default=None)
    ap.add_argument("--proof", default="")
    ap.add_argument("--run", default=None)
    a = ap.parse_args(argv)
    bad = input_problem(a)
    if bad:
        print("worker-card: refused: " + bad, file=sys.stderr)
        return 1
    if answered_duplicate(a.header):
        print("worker-card: refused: this decision was already answered; act on that answer, do not re-ask", file=sys.stderr)
        return 1
    opts = list(a.option)
    opts.insert(0, opts.pop(a.recommended))          # the recommended option is shown first
    # the card states its own recommendation; the guard refuses it if --why names a different option
    rec_label = opts[0].partition("|")[0].strip()
    ctx = a.header.strip() + "\nRecommended: " + rec_label + "\nProof: " + a.proof.strip() + "\nWhy: " + a.why.strip()
    options = [{"label": o.partition("|")[0].strip(), "description": o.partition("|")[2].strip()} for o in opts]
    problem = card_guard.check(a.header, ctx, options, require_why=True, require_proof=True,
                               recommended=0, check_recommended=True, require_rec_line=True)
    if problem:
        print("worker-card: refused: " + problem, file=sys.stderr)
        return 1
    cid = card_id(a.ticket, a.header)
    os.makedirs(STATE_DIR, exist_ok=True)
    ledger = os.path.join(STATE_DIR, run_key(a) + ".jsonl")
    lock = open(os.path.join(STATE_DIR, ".lock"), "a")
    fcntl.flock(lock, fcntl.LOCK_EX)
    try:
        rows = []
        try:
            with open(ledger) as lf:
                for l in lf:
                    if not l.strip():
                        continue
                    r = json.loads(l)
                    if not isinstance(r, dict) or not r.get("id") or not math.isfinite(float(r.get("t") or 0)):
                        raise ValueError("bad row")
                    r["t"] = float(r.get("t"))
                    rows.append(r)
        except FileNotFoundError:
            rows = []
        except (OSError, ValueError, TypeError):
            # A cap ledger we cannot read in full is NOT an empty ledger: refuse (fail closed), never reset the count.
            print("worker-card: refused: the cap ledger %s is unreadable or has a corrupt line; fix or remove that file (the cap cannot be counted)" % os.path.basename(ledger), file=sys.stderr)
            return 1
        now = time.time()
        rows = [r for r in rows if now - r["t"] < WINDOW_S]
        ids = {r.get("id") for r in rows}
        if cid in ids:
            print("asked %s (already filed this run, no new card)" % cid)
            print("Card: " + cid)
            return 0
        if len(ids) >= CAP:
            print("worker-card: refused: cap reached (%d cards per worker run); put the rest in your report as NEEDS-OPERATOR text" % CAP, file=sys.stderr)
            return 1
        os.makedirs(os.path.dirname(QUEUE), exist_ok=True)
        try:
            with open(ledger, "a") as f:             # the cap line first: if it cannot be written, no card is posted
                f.write(json.dumps({"id": cid, "ticket": a.ticket, "header": a.header.strip(), "t": time.time()}) + "\n")
        except OSError:
            print("worker-card: refused: cannot write the cap ledger", file=sys.stderr)
            return 1
        with open(QUEUE, "a") as q:
            q.write(json.dumps({"id": cid, "ticket": a.ticket, "header": a.header.strip(), "context": ctx,
                                "options": options, "recommended": 0, "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z")}) + "\n")
        print("asked " + cid)
        print("Card: " + cid)
        return 0
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)


if __name__ == "__main__":
    sys.exit(main())
