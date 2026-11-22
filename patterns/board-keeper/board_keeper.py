#!/usr/bin/env python3
"""board-keeper: keeps a card board honest against the to-do list, with plain code. No model call, no spawning.

Each card sits in a column (stage) and links to one or more items. Compare the column with the status of the linked items:
  FIX   a card in an open column whose linked items are ALL closed (at least one done, none open) moves to `done`.
  MOVE  forward only, one legal edge at a time, from work signals on the OPEN linked items:
        claimed and owned -> todo; owner is a live worker -> building; waiting -> blocked; has a VERIFY line -> verifying.
        A jump that is not on EDGES (backward, or skipping columns) is flagged "illegal-move-blocked", never made.
  FLAG  (never changed) a done card with open items; a building card whose open items all belong to dead owners;
        a linked item the list does not know (such a card is never moved).

Safety:
  * one run at a time (flock on <state>/board-keeper.lock); a second run exits at once.
  * if the attempt line or the move line cannot be written, NOTHING moves and the run exits non-zero (fail closed).
  * the board is re-read right before each write; only that card changes, and only if it is still in the column we read.
  * never closes, edits or deletes an item. It reads the items file and writes only the board file, the log and the flags.
  * daily caps: 25 to-done fixes and 100 moves of any kind per day, counted from the log (a crash loop cannot exceed them);
    an unreadable log counts as "cap reached".
  * kill switch: file <state>/board-keeper-off -> exit at once, nothing read, nothing written but one log line;
    it is checked again before every single move.
  * the items file looks truncated (fewer than MIN_ITEMS rows, or not valid JSON) -> flag-only cycle, no changes.
  * a person's move (any mover other than board-keeper) holds that card for 24 hours; `undo` reverses the latest move
    and holds the card the same way.
  * a card id must match [a-z0-9][a-z0-9-]{0,47}; anything else is never touched.

Files (JSON):
  board   $HUBKIT_HOME/state/board.json   {"cards": [{"id": "c1", "stage": "todo", "linked": ["L-0001"]}]}
  items   $HUBKIT_HOME/state/items.json   {"L-0001": {"status": "active", "owner": "w1", "wait": false, "verify": false}}
  live    $HUBKIT_HOME/state/live-owners.txt   one live worker name per line (missing, empty or older than 24 h = unknown, no building moves)
Usage:  board_keeper.py [run] [--dry-run] [--json]      one cycle
        board_keeper.py undo CARD                       reverse the latest move of CARD
Env: HUBKIT_HOME, BK_BOARD, BK_ITEMS, BK_STATE_DIR, BK_LIVE_OWNERS, BK_CAP, BK_MOVE_CAP, BK_MIN_ITEMS, BK_NOW (epoch seconds, tests)
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import sys
import time
from pathlib import Path

HOME = Path(os.environ.get("HUBKIT_HOME") or Path.home() / ".claude" / "hubkit")
BOARD = Path(os.environ.get("BK_BOARD") or HOME / "state" / "board.json")
ITEMS = Path(os.environ.get("BK_ITEMS") or HOME / "state" / "items.json")
STATE = Path(os.environ.get("BK_STATE_DIR") or HOME / "state" / "board-keeper")
LIVE_OWNERS = Path(os.environ.get("BK_LIVE_OWNERS") or HOME / "state" / "live-owners.txt")
CAP = int(os.environ.get("BK_CAP") or 25)
MOVE_CAP_PER_DAY = int(os.environ.get("BK_MOVE_CAP") or 100)
MIN_ITEMS = int(os.environ.get("BK_MIN_ITEMS") or 1)
UNDO_HOLD = 24 * 3600
LIVE_OWNER_MAX_AGE = 24 * 3600     # a live-owners file older than this is unknown, not trusted
STAGE_ORDER = ["ideas", "grooming", "todo", "building", "blocked", "verifying", "review", "done"]
# The only edges the automatic mover may take. To-done from any open column is the proof-based close.
EDGES = frozenset({("grooming", "todo"), ("todo", "building"), ("todo", "blocked"), ("todo", "verifying"), ("building", "blocked"),
                   ("building", "verifying"), ("blocked", "verifying")}) | frozenset((s, "done") for s in STAGE_ORDER[:-1])
FIXABLE = frozenset({"todo", "grooming", "building", "blocked", "verifying", "review"})
CLOSED = frozenset({"done", "dropped"})
PARKED = frozenset({"backlog", "someday"})
GENERIC_OWNER_RE = re.compile(r"^(hub|run-\d[\w-]*|lead-chat[\w-]*|unowned)$", re.I)   # a shared seat, not a worker on that item
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")
KILL, LOG, MOVES, FLAGS = "board-keeper-off", "board-keeper.log", "moves.jsonl", "flags.json"


def now() -> float:
    return float(os.environ.get("BK_NOW") or time.time())


def day_key(t: float | None = None) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(now() if t is None else t))


def log(entry: dict) -> bool:
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        entry = dict(entry, ts=time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now())))
        with open(STATE / LOG, "a") as f:
            f.write(json.dumps(entry, sort_keys=True) + "\n")
        return True
    except OSError:
        return False


def count_today(actions) -> int:
    """Attempts of the given kinds logged today. A log that cannot be read counts as the cap (fail closed)."""
    n, today = 0, day_key()
    try:
        f = open(STATE / LOG)
    except FileNotFoundError:
        return 0
    except OSError:
        return 10 ** 9
    with f:
        for line in f:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if isinstance(e, dict) and e.get("action") in actions and str(e.get("ts", ""))[:10] == today:
                n += 1
    return n


def fixes_today() -> int:
    return count_today(("fix",))


def moves_today() -> int:
    return count_today(("fix", "move"))


def read_items():
    """-> (items dict, trustworthy bool). Not valid JSON, not an object, or too few rows = not trustworthy."""
    try:
        d = json.loads(ITEMS.read_text())
    except (OSError, ValueError):
        return {}, False
    if not isinstance(d, dict) or len(d) < MIN_ITEMS:
        return {}, False
    return {k: v for k, v in d.items() if isinstance(k, str) and isinstance(v, dict)}, True


def read_board():
    try:
        d = json.loads(BOARD.read_text())
        cards = d.get("cards") if isinstance(d, dict) else None
        return (d, cards) if isinstance(cards, list) else (None, None)
    except (OSError, ValueError):
        return None, None


def live_owners() -> set | None:
    """Names of live workers, or None (= unknown: no building moves, no dead-owner flags) when the file is missing,
    older than 24 h, or empty."""
    try:
        if now() - LIVE_OWNERS.stat().st_mtime > LIVE_OWNER_MAX_AGE:
            return None
        names = {l.strip() for l in LIVE_OWNERS.read_text().splitlines() if l.strip()}
        return names or None
    except OSError:
        return None


def person_held():
    """-> (held, undone). held = cards whose newest moves.jsonl line was made by anyone but board-keeper less than 24 h ago.
    undone = the subset whose newest line is an undo (also holds the to-done fix). Corrupt lines are skipped."""
    last = {}
    try:
        with open(STATE / MOVES) as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if isinstance(e, dict) and isinstance(e.get("card"), str):
                    last[e["card"]] = e
    except OSError:
        return set(), set()
    held, undone = set(), set()
    for c, e in last.items():
        try:
            if str(e.get("who")) != "board-keeper" and now() - float(e.get("at", 0)) < UNDO_HOLD:
                held.add(c)
                if e.get("kind") == "undo":
                    undone.add(c)
        except (TypeError, ValueError):
            pass
    return held, undone


def log_move(entry: dict) -> bool:
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        entry = dict(entry, at=int(now()), ts=time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now())))
        with open(STATE / MOVES, "a") as f:
            f.write(json.dumps(entry, sort_keys=True) + "\n")
        return True
    except OSError:
        return False


def _linked(c):
    l = c.get("linked")
    if isinstance(l, str):
        try:
            l = json.loads(l)
        except ValueError:
            return []
    return [t for t in l if isinstance(t, str)] if isinstance(l, list) else []


def classify(cards, status, owner, live):
    """-> (fixes, flags). Pure function of its inputs."""
    fixes, flags = [], []
    for c in cards:
        cid, stage = c.get("id"), c.get("stage")
        linked = _linked(c)
        if not isinstance(cid, str) or not ID_RE.match(cid) or not linked:
            continue
        sts = {t: status.get(t) for t in linked}
        unknown = sorted(t for t, s in sts.items() if s is None)
        known = {t: s for t, s in sts.items() if s is not None}
        open_t = sorted(t for t, s in known.items() if s not in CLOSED and s not in PARKED)
        done_t = [t for t, s in known.items() if s == "done"]
        if unknown:
            flags.append({"card": cid, "stage": stage, "kind": "unknown-item", "items": unknown})
            continue
        if stage in FIXABLE and not open_t and done_t and all(s in CLOSED for s in known.values()):
            fixes.append({"card": cid, "from": stage, "to": "done", "why": "all %d linked items are closed" % len(known), "items": sorted(known)})
        elif stage == "done" and open_t:
            flags.append({"card": cid, "stage": stage, "kind": "done-but-open", "items": open_t})
        elif stage == "building" and open_t and live is not None:
            owners = {t: owner.get(t) for t in open_t if known[t] in ("active", "working")}
            if owners and all((o or "") not in live for o in owners.values()):
                flags.append({"card": cid, "stage": stage, "kind": "dead-owner", "items": sorted(owners)})
    return fixes, flags


def item_signal(tid, status, owner, live, info):
    """One open item -> the column its work signals point at, or None (open and unclaimed)."""
    i = info.get(tid) or {}
    s, o = status.get(tid), (owner.get(tid) or "unowned")
    if i.get("verify"):
        return "verifying"
    if i.get("wait"):
        return "blocked"
    if s == "active" and o != "unowned":
        return "building" if (live is not None and o in live and not GENERIC_OWNER_RE.match(o)) else "todo"
    return None


def classify_moves(cards, status, owner, live, skip, info):
    """-> (moves, flags). Forward edges only; pure. `skip` = card ids already handled (fixes, held cards)."""
    moves, flags = [], []
    for c in cards:
        cid, stage = c.get("id"), c.get("stage")
        if cid in skip or not isinstance(cid, str) or not ID_RE.match(cid) or stage not in STAGE_ORDER or stage in ("ideas", "review", "done"):
            continue
        linked = _linked(c)
        if not linked or any(status.get(t) is None for t in linked):
            continue                        # an unknown item could be anything: no move
        open_t = [t for t in linked if status[t] not in CLOSED and status[t] not in PARKED]
        sig = {t: item_signal(t, status, owner, live, info) for t in open_t}
        sig = {t: s for t, s in sig.items() if s}
        if not sig:
            continue
        vals = set(sig.values())
        target = "blocked" if vals == {"blocked"} else min(vals - {"blocked"}, key=STAGE_ORDER.index)
        if target == stage:
            continue
        if (stage, target) in EDGES:
            moves.append({"card": cid, "from": stage, "to": target, "why": "item signals: " + ", ".join("%s=%s" % (t, sig[t]) for t in sorted(sig)), "items": sorted(sig)})
        elif STAGE_ORDER.index(target) > STAGE_ORDER.index(stage):
            flags.append({"card": cid, "stage": stage, "kind": "illegal-move-blocked", "to": target, "items": sorted(sig)})
    return moves, flags


def kill_on() -> bool:
    return (STATE / KILL).exists()


def _write_board(doc) -> bool:
    try:
        tmp = BOARD.with_name(BOARD.name + ".tmp")
        tmp.write_text(json.dumps(doc, indent=1))
        os.replace(tmp, BOARD)
        return True
    except OSError:
        return False


def _set_stage(cid, expect_from, stage) -> bool:
    """Move ONE card. The board is re-read right now and only this card is changed, and only if it still sits in
    `expect_from`: a person's edit made since our first read is never overwritten (stale-snapshot guard)."""
    doc, cards = read_board()
    if cards is None:
        return False
    for c in cards:
        if c.get("id") == cid:
            if c.get("stage") != expect_from:
                return False
            c["stage"] = stage
            return _write_board(doc)
    return False


def run(dry=False) -> dict:
    out = {"fixed": [], "moved": [], "flags": [], "skipped": None, "error": None}
    if kill_on():
        log({"action": "kill-switch"})
        out["skipped"] = "kill-switch"
        return out
    lockf = None
    if not dry:
        try:
            STATE.mkdir(parents=True, exist_ok=True)
            lockf = open(STATE / "board-keeper.lock", "w")
            fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log({"action": "skip", "why": "another run holds the lock"})
            out["skipped"] = "another run in progress"
            return out
        except OSError:
            pass                            # no lock file = the log-based caps still hold
        if not log({"action": "run-start"}):  # cannot write the log = cannot count the cap = move nothing
            out["error"] = "log-unwritable"
            out["skipped"] = "log-unwritable (nothing moved)"
            return out
    try:
        return _run(dry, out)
    finally:
        if lockf:
            lockf.close()


def _run(dry, out) -> dict:
    doc, cards = read_board()
    if cards is None:
        log({"action": "board-unreadable"})
        out["skipped"] = "board-unreadable"
        return out
    items, ok = read_items()
    status = {k: v.get("status") for k, v in items.items()}
    owner = {k: v.get("owner") for k, v in items.items()}
    live = live_owners()
    fixes, flags = classify(cards, status, owner, live)
    held, undone = person_held()
    if not ok:
        out["flags"] = flags
        out["skipped"] = "items-unreadable (flag-only cycle)"
        log({"action": "items-unreadable"})
        return out
    for f in [f for f in fixes if f["card"] in undone]:       # a person's undo beats the proof-based close
        flags.append({"card": f["card"], "stage": f["from"], "kind": "undone-by-hand", "items": f["items"]})
    fixes = [f for f in fixes if f["card"] not in undone]
    left = min(CAP - fixes_today(), MOVE_CAP_PER_DAY - moves_today())
    for f in fixes:
        if left <= 0:
            flags.append(dict(f, kind="cap-reached"))
            continue
        if kill_on():                                         # re-checked before EVERY action
            flags.append(dict(f, kind="kill-switch"))
            continue
        left -= 1
        if dry:
            out["fixed"].append(f)
            continue
        # The attempt line and the move line are written BEFORE the move. If either cannot be written, NOTHING moves.
        if not log({"action": "fix", "card": f["card"]}) or not log_move({"card": f["card"], "from": f["from"], "to": "done", "who": "board-keeper", "kind": "fix", "why": f["why"]}):
            out["error"] = "log-unwritable"
            flags.append(dict(f, kind="log-unwritable"))
            out["flags"] = flags
            return out
        if _set_stage(f["card"], f["from"], "done"):
            out["fixed"].append(f)
        else:
            flags.append(dict(f, kind="card-changed-since-read"))
    skip = {f["card"] for f in fixes} | held | undone
    moves, mflags = classify_moves(cards, status, owner, live, skip, items)
    flags += mflags
    for m in moves:
        if moves_today() >= MOVE_CAP_PER_DAY:
            flags.append(dict(m, kind="cap-reached"))
            continue
        if kill_on():
            flags.append(dict(m, kind="kill-switch"))
            continue
        if dry:
            out["moved"].append(m)
            continue
        if not log({"action": "move", "card": m["card"]}) or not log_move({"card": m["card"], "from": m["from"], "to": m["to"], "who": "board-keeper", "kind": "move", "why": m["why"]}):
            out["error"] = "log-unwritable"
            flags.append(dict(m, kind="log-unwritable"))
            break
        if _set_stage(m["card"], m["from"], m["to"]):
            out["moved"].append(m)
        else:
            flags.append(dict(m, kind="card-changed-since-read"))
    out["flags"] = flags
    if not dry:
        try:
            STATE.mkdir(parents=True, exist_ok=True)
            (STATE / FLAGS).write_text(json.dumps(flags, indent=1))
        except OSError:
            pass
    return out


def undo(card: str, who: str = "person") -> int:
    """Put CARD back where its latest logged move found it. The card is then held for 24 hours."""
    last = None
    try:
        with open(STATE / MOVES) as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if isinstance(e, dict) and e.get("card") == card and e.get("kind") in ("fix", "move"):
                    last = e
    except OSError:
        pass
    if not last:
        print("board-keeper: nothing to undo for that card", file=sys.stderr)
        return 1
    if not log_move({"card": card, "from": last["to"], "to": last["from"], "who": who, "kind": "undo", "why": "undo of the latest move"}):
        print("board-keeper: cannot write the move log, nothing undone", file=sys.stderr)
        return 1
    if not _set_stage(card, last["to"], last["from"]):
        print("board-keeper: board unreadable, card missing, or a person moved it since (not forced back)", file=sys.stderr)
        return 1
    print("undone: %s back to %s" % (card, last["from"]))
    return 0


def main(argv) -> int:
    args = argv[1:]
    if args[:1] == ["undo"]:
        if len(args) != 2 or not ID_RE.match(args[1]):
            print("usage: board_keeper.py undo CARD", file=sys.stderr)
            return 2
        return undo(args[1])
    res = run(dry="--dry-run" in args)
    rc = 1 if res.get("error") else 0
    if "--json" in args:
        print(json.dumps(res, indent=1))
    else:
        print("fixed %d, moved %d, flags %d%s" % (len(res["fixed"]), len(res["moved"]), len(res["flags"]), (", skipped: " + res["skipped"]) if res["skipped"] else ""))
    if res.get("error"):
        print("board-keeper: ERROR %s" % res["error"], file=sys.stderr)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv))
