#!/usr/bin/env python3
"""work-loop Stop arm (OFF by default): when a registered lane is about to stop, hand it its next ticket.

Wire it only if you want it (see kit/settings.snippet.loop.json), as a Stop hook:

    python3 ~/.claude/hubkit/work-loop/stop_arm.py

The chat names its lane with the environment variable HUBKIT_LANE. Reads the Stop payload as JSON on stdin.
To keep going it prints {"decision":"block","reason":"Next ticket: L-0007 ..."} and exits 0.
To let the chat stop it prints nothing and exits 0. It lets the chat stop when ANY of these holds:

  - the payload is not JSON, or comes from a subagent (it has an agent_id)
  - the file $HUBKIT_HOME/state/loop-pause exists (the operator's off switch)
  - HUBKIT_LANE is unset, malformed, or not in lanes.json (an unregistered chat is never continued)
  - the lane is on hold (`work_loop.py hold --lane`), or its hold file is unreadable
  - the lane already holds a claim: an open or active ticket it owns, or an active ticket nobody owns that this arm or `tick` handed to it
  - the lane already auto-continued 3 times without closing one of those tickets (done, which the ledger only allows with proof)
  - no ticket is eligible (same rules as `work_loop.py next`), or the only ones are needs-operator
  - anything at all goes wrong, including an unreadable ledger (fail open: the chat stops)

The cap is counted in $HUBKIT_HOME/state/stop-arm.json, never from the payload's stop_hook_active flag,
so a payload that says false every time still cannot loop. Events: loop.stop_continue, loop.stop_capped.
Python 3.9, standard library only. POSIX only (fcntl).
"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import work_loop as wl  # noqa: E402

CAP = 3                      # auto-continues in a row without a proof-backed close
MAX_STDIN = 4 * 1024 * 1024  # a huge last_assistant_message must not stall the hook


def decide(payload):
    """Return the next ticket line to continue with, or None to let the chat stop."""
    if not isinstance(payload, dict) or payload.get("agent_id"):
        return None
    sd = wl.state_dir()
    if os.path.exists(os.path.join(sd, "loop-pause")):
        return None
    lane = wl.norm(os.environ.get("HUBKIT_LANE"))
    if not wl.valid_lane(lane):
        return None
    registry = wl.load_registry()
    if registry is None or lane not in registry:
        return None
    entry = registry[lane]
    feed = os.path.join(sd, "loop-feed.json")
    os.makedirs(sd, exist_ok=True)
    with wl.locked("loop-feed.json.lock"):    # the same lock `next` and `tick` take on loop-feed.json
        events, _bad = wl.read_events_tolerant(wl.ledger_path())
        records = wl.ledger_lib.fold(events)
        mine = wl.lane_names(lane, entry)
        hold_until, hold_bad = wl.read_hold(lane)
        if hold_bad or (hold_until is not None and wl.now_s() < hold_until):
            return None          # the operator's per-lane off switch (unreadable hold file counts as a hold)
        state_path = os.path.join(sd, "stop-arm.json")
        try:
            with open(state_path, "r", encoding="utf-8") as fh:
                state = json.load(fh)
            mine_state = state[lane] if isinstance(state.get(lane), dict) else {}
        except (OSError, ValueError, AttributeError):
            state, mine_state = {}, {}
        offers, last_feed = wl.read_feed(feed)
        picked = [t for t in mine_state.get("picked", []) if isinstance(t, str)]
        if any(records.get(t, {}).get("status") == "done" for t in picked):
            picked = []          # a ticket it handed out was closed with proof: the count starts over
        for tid0, rec in records.items():
            owner = wl.norm(rec.get("owner"))
            status = rec.get("status")
            # `ledger claim` writes only an owner, `ledger status active` writes only a status: either one is a claim.
            # An active ticket nobody owns counts only when it is this lane's: handed to it by this arm or by `tick`.
            if (owner in mine and status in ("open", "active")) or (
                    status == "active" and owner in ("", "unowned")
                    and (tid0 in picked or (isinstance(offers.get(tid0), dict) and offers[tid0].get("lane") == lane))):
                return None
        if len(picked) >= CAP:
            wl.emit("loop.stop_capped", lane=lane, count=len(picked))
            return None
        time.sleep(float(os.environ.get("WORK_LOOP_HOLD_S") or 0))   # tests only: widen the race window
        now = wl.now_s()
        tid = wl.pick(records, lane, entry, offers, now)
        if not tid:
            return None
        offers[tid] = {"lane": lane, "at": now}
        offers = {k: v for k, v in offers.items()
                  if isinstance(v, dict) and now - float(v.get("at", 0) or 0) < wl.OFFER_WINDOW_S}
        state = state if isinstance(state, dict) else {}
        state[lane] = {"picked": (picked + [tid])[-CAP:], "at": now}
        wl.write_atomic(feed, {"sent": offers, "last_feed": last_feed})
        wl.write_atomic(state_path, state)
        wl.emit("loop.stop_continue", lane=lane, ticket=tid)
        return "Next ticket: %s %s. Claim it (ledger claim, then ledger status active), do the work, close it with proof." % (
            tid, wl.clean_text(records[tid].get("title", "")))


def main():
    try:
        payload = json.loads(sys.stdin.buffer.read(MAX_STDIN).decode("utf-8", "replace"))
        reason = decide(payload)
    except Exception:
        return 0                 # fail open: any error lets the chat stop
    if reason:
        sys.stdout.write(json.dumps({"decision": "block", "reason": reason}) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
