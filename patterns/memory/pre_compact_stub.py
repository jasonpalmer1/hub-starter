#!/usr/bin/env python3
"""PreCompact hook: write a small, mechanical stub BEFORE Claude Code compacts the chat.

Reads the hook JSON on stdin, writes $HUBKIT_HOME/compact-stubs/<session id>.md and always exits 0
(a memory aid must never block a compaction). The stub holds only fields that this script validates
itself, so no free text from the chat and no path ever reaches the next session:

  session_id   must be a UUID-shaped or plain id token, else "unknown"
  written      this script's own clock
  tickets      ids matching TICKET_RE found in the tail of the transcript (sorted, capped)
  newest_log   file name of the newest /log entry in the memory folder (name shape checked)
  logged       yes when a log carries this session's "logged-session:" footer, else NO

The stub exists to say one thing to the next session: "a compaction happened; read the real log".
Env: HUBKIT_HOME (default ~/.claude/hubkit), MEMORY_DIR (default from memory_init.py), TICKET_RE
(default L-\\d{4}).
"""
import json
import os
import re
import sys
import time

MAX_TAIL = 400_000
MAX_TICKETS = 20
ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{5,63}")
LOG_RE = re.compile(r"\d{4}-\d{2}-\d{2}-[\w.-]+\.md")


def home_dirs():
    home = os.environ.get("HOME", "")
    hub = os.environ.get("HUBKIT_HOME") or os.path.join(home, ".claude", "hubkit")
    mem = os.environ.get("MEMORY_DIR")
    if not mem:
        slug = re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(home))
        mem = os.path.join(home, ".claude", "projects", slug, "memory")
    return hub, mem


def valid_id(v):
    return v if isinstance(v, str) and ID_RE.fullmatch(v) else "unknown"


def tail(path, n=MAX_TAIL):
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - n))
            return fh.read().decode("utf-8", "ignore")
    except (OSError, TypeError, ValueError):
        return ""


def newest_log(mem, sid):
    conv = os.path.join(mem, "conversations")
    try:
        names = [f for f in os.listdir(conv) if LOG_RE.fullmatch(f) and os.path.isfile(os.path.join(conv, f))]
    except OSError:
        return "none", False
    names.sort(key=lambda f: os.path.getmtime(os.path.join(conv, f)), reverse=True)
    logged = False
    for f in names[:30]:
        if sid != "unknown" and ("logged-session: %s" % sid) in tail(os.path.join(conv, f), 20000):
            logged = True
            break
    return (names[0] if names else "none"), logged


def main():
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        if not isinstance(payload, dict):
            payload = {}
    except ValueError:
        payload = {}
    hub, mem = home_dirs()
    sid = valid_id(payload.get("session_id"))
    try:
        ticket_re = re.compile(os.environ.get("TICKET_RE") or r"L-\d{4}")
    except re.error:
        ticket_re = re.compile(r"L-\d{4}")
    tp = payload.get("transcript_path")
    found = sorted({m for m in ticket_re.findall(tail(tp)) if isinstance(m, str)})[:MAX_TICKETS] if isinstance(tp, str) else []
    log, logged = newest_log(mem, sid)
    body = ["# compact stub (written by the PreCompact hook; mechanical, no chat text)",
            "- session_id: %s" % sid,
            "- written: %s" % time.strftime("%Y-%m-%dT%H:%M:%S"),
            "- tickets: %s" % (", ".join(found) or "none"),
            "- newest_log: %s" % log,
            "- logged: %s" % ("yes" if logged else "NO (run /log before more work)"),
            ""]
    try:
        d = os.path.join(hub, "compact-stubs")
        os.makedirs(d, exist_ok=True)
        tmp = os.path.join(d, ".%s.tmp-%d" % (sid, os.getpid()))
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write("\n".join(body))
        os.replace(tmp, os.path.join(d, sid + ".md"))
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:   # never block a compaction
        sys.exit(0)
