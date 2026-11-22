#!/usr/bin/env python3
"""Duplicate-worker check: is another live worker already on this ticket?

Reads an Agent tool input as JSON on stdin ({"description": ..., "prompt": ...}) and
reports any LIVE subagent, in any chat session on this machine, whose primary ticket
ids overlap the new brief's primary ticket ids.

Primary ids = ticket ids found in the description plus the first 300 characters of the
prompt. Long briefs mention many other tickets deep in the text; those are context, not
the job, and are ignored. A ticket id looks like L-0001 (a letter L, a dash, four digits,
which is what the ledger pattern hands out). Change the shape with DUP_TICKET_RE.

"Live" = the worker's transcript was modified within DUP_LIVE_MIN minutes (default 15)
AND its last message is not a finished assistant turn (stop_reason end_turn).

Exit 0: no clash.   Exit 3: clash, one line per clash on stdout.
Bad or empty input exits 0 (nothing to check).

Where it looks: $DUP_PROJECTS_ROOT, default ~/.claude/projects, for
  */<session>/subagents/agent-<id>.jsonl   (the transcript)
  */<session>/subagents/agent-<id>.meta.json   (optional, has the description)
"""
import glob
import json
import os
import re
import sys
import time

HEAD_CHARS = 300
DEFAULT_TICKET_RE = r"\bL-\d{4}\b"


def ticket_rx():
    try:
        return re.compile(os.environ.get("DUP_TICKET_RE") or DEFAULT_TICKET_RE)
    except re.error:
        return re.compile(DEFAULT_TICKET_RE)


def live_minutes():
    try:
        return float(os.environ.get("DUP_LIVE_MIN") or "15")
    except ValueError:
        return 15.0


def projects_root():
    return os.path.expanduser(os.environ.get("DUP_PROJECTS_ROOT") or "~/.claude/projects")


_SKIP_LINE = re.compile(r"^[\s>*`#-]*(?:DUP-OK|(?:exclusions?|exclude[sd]?)\s*:)", re.I)


def drop_context_lines(text):
    """Ids on a line STARTING with DUP-OK or an exclusion label (Exclusions: / Exclude: / Excluded:) are context
    ("do not touch L-0002"), never a claim on that ticket, so they never make a clash."""
    return "\n".join(l for l in str(text or "").splitlines() if not _SKIP_LINE.search(l))


def primary_ids(description, prompt):
    d = drop_context_lines(description)
    p = drop_context_lines(prompt)[:HEAD_CHARS]
    return set(ticket_rx().findall(f"{d} {p}"))


def _tail_records(path, nbytes=65536):
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - nbytes))
        chunk = f.read().decode("utf-8", "ignore")
    out = []
    for line in chunk.splitlines():
        try:
            out.append(json.loads(line))
        except Exception:
            pass
    return out


def is_finished(path):
    """True when the last real message is the assistant ending its turn."""
    for rec in reversed(_tail_records(path)):
        if rec.get("type") not in ("assistant", "user"):
            continue
        m = rec.get("message") or {}
        return rec.get("type") == "assistant" and m.get("stop_reason") == "end_turn"
    return False


def first_brief(path):
    with open(path, encoding="utf-8", errors="ignore") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("type") == "user":
                c = (rec.get("message") or {}).get("content")
                if isinstance(c, list):
                    c = " ".join(x.get("text", "") for x in c if isinstance(x, dict))
                return c or ""
    return ""


def live_workers(now=None):
    now = now or time.time()
    cutoff = now - live_minutes() * 60
    for path in glob.glob(os.path.join(projects_root(), "*", "*", "subagents", "agent-*.jsonl")):
        try:
            mt = os.stat(path).st_mtime
        except OSError:
            continue
        if mt < cutoff or is_finished(path):
            continue
        meta = {}
        try:
            with open(path[: -len(".jsonl")] + ".meta.json") as f:
                meta = json.load(f)
        except Exception:
            pass
        yield {
            "agent": os.path.basename(path)[len("agent-"): -len(".jsonl")],
            "session": path.split(os.sep)[-3][:8],
            "description": meta.get("description", ""),
            "ids": primary_ids(meta.get("description", ""), first_brief(path)),
            "age_min": round((now - mt) / 60, 1),
        }


def clashes(description, prompt):
    new = primary_ids(description, prompt)
    if not new:
        return []
    out = []
    for w in live_workers():
        for t in sorted(new & w["ids"]):
            out.append(f"{t} is held by live worker {w['agent']} (session {w['session']}, "
                       f"\"{w['description']}\", active {w['age_min']} min ago)")
    return out


def main():
    try:
        tin = json.load(sys.stdin)
        lines = clashes(tin.get("description", ""), tin.get("prompt", ""))
    except Exception:
        return 0
    for line in lines:
        print(line)
    return 3 if lines else 0


if __name__ == "__main__":
    sys.exit(main())
