#!/usr/bin/env python3
"""ticket-lock: inspect / free ticket locks.
  ticket-lock.py release L-0001     free the lock after a stopped or crashed worker
  ticket-lock.py list               show locks (ticket, owner, age)
Honors TICKET_LOCK_DIR and HUBKIT_HOME."""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ticket_lock as tl  # noqa: E402


def main(a):
    if len(a) == 2 and a[0] == "release" and tl.TICKET_RE.fullmatch(a[1]):
        if tl.release(a[1]):
            print("released " + a[1])
        else:
            print("no lock for " + a[1])
        return 0
    if a[:1] == ["list"]:
        d = tl.lock_dir()
        for fn in sorted(os.listdir(d)) if os.path.isdir(d) else []:
            if fn.endswith(".json"):
                lk = tl._read(fn[:-5]) or {}
                age = int((time.time() - float(lk.get("started") or 0)) / 60)
                print(f"{fn[:-5]}\t{lk.get('owner')}\t{age}m\t{'live' if tl.is_live(lk) else 'expired'}")
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
