#!/usr/bin/env python3
"""SessionStart hook: after a compaction, show the new session its compact stub.

Reads the hook JSON on stdin. Looks in $HUBKIT_HOME/compact-stubs/ for the stub of this session_id, or
else the newest stub younger than 6 hours, and prints it as extra context. Prints nothing (exit 0) when
there is none. Never blocks a session start.
"""
import json
import os
import re
import sys
import time

ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{5,63}")
MAX_AGE = 6 * 3600


def main():
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        payload = {}
    sid = payload.get("session_id") if isinstance(payload, dict) else None
    hub = os.environ.get("HUBKIT_HOME") or os.path.join(os.environ.get("HOME", ""), ".claude", "hubkit")
    d = os.path.join(hub, "compact-stubs")
    try:
        files = [f for f in os.listdir(d) if f.endswith(".md") and not f.startswith(".")]
    except OSError:
        return 0
    pick = None
    if isinstance(sid, str) and ID_RE.fullmatch(sid) and (sid + ".md") in files:
        pick = os.path.join(d, sid + ".md")
    else:
        fresh = [os.path.join(d, f) for f in files if time.time() - os.path.getmtime(os.path.join(d, f)) < MAX_AGE]
        if fresh:
            pick = max(fresh, key=os.path.getmtime)
    if not pick:
        return 0
    try:
        with open(pick, encoding="utf-8") as fh:
            text = fh.read(4000)
    except OSError:
        return 0
    msg = text + "\nIf `logged` says NO, run /log now, then read the newest file in the memory conversations folder."
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": msg}}))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
