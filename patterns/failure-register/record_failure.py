#!/usr/bin/env python3
"""record_failure: append one failure (or one pass) to $HUBKIT_HOME/state/failures.jsonl.

  python3 record_failure.py --job nightly --step build --reason "build timed out after 300s" --exit 124
  python3 record_failure.py --job nightly --step build --ok          # a pass for that job and step

Secrets and absolute paths are removed before anything is written. One line per call, appended with
O_APPEND. Never raises: on a write problem it prints one line on stderr and exits 1, so call it as
`record_failure.py ... || true` and a logging problem can never fail the job it is watching.
Python 3.9, standard library only.
"""
import argparse
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import failure_register as fr  # noqa: E402

TEXT_LIMIT = 300


def flat(text, limit):
    return re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f\x7f]", " ", fr.scrub(text))).strip()[:limit]


def main(argv=None):
    ap = argparse.ArgumentParser(prog="record_failure")
    ap.add_argument("--job", required=True)
    ap.add_argument("--step", default="run")
    ap.add_argument("--reason", default="")
    ap.add_argument("--exit", type=int, dest="code")
    ap.add_argument("--text", default="")
    ap.add_argument("--ok", action="store_true")
    ap.add_argument("--ts", help="ISO time or epoch seconds (default now)")
    ap.add_argument("--file", help="default $HUBKIT_HOME/state/failures.jsonl")
    args = ap.parse_args(argv)
    ts = args.ts or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    rec = {"ts": ts, "job": flat(args.job, 80), "step": flat(args.step, 80)}
    if args.ok:
        rec["ok"] = True
    else:
        rec["reason"] = flat(args.reason, TEXT_LIMIT)
        if args.code is not None:
            rec["exit"] = args.code
        if args.text:
            rec["text"] = flat(args.text, TEXT_LIMIT)
    path = args.file or os.path.join(fr.default_state(), "failures.jsonl")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, (json.dumps(rec, sort_keys=True) + "\n").encode("utf-8"))
        finally:
            os.close(fd)
    except OSError as ex:
        print("record_failure: could not write %s (%s)" % (path, ex.__class__.__name__), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
