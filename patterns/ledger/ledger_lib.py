"""Shared read / fold / write logic for the ledger CLI and the banner hook.

Not an entry point: imported by ./ledger and ./ledger-banner-hook.py, which
live in the same directory. Run `ledger --help` for the beginner guide.

The ledger is one append-only JSON-lines file. Every change is a new line
(an "event"); an item's current state is the fold of all its lines in file
order. Nothing is ever rewritten in place.
"""
from __future__ import annotations

import contextlib
import fcntl  # POSIX only (macOS, Linux; on Windows use WSL)
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

# State lives under $HUBKIT_HOME (default ~/.claude/hubkit). LEDGER_PATH
# overrides the file itself, so tests and experiments can point every command
# at a scratch copy and never touch real state. Read once at import: every CLI
# call is a fresh process, so there is no staleness risk.
HUBKIT_HOME = Path(os.environ.get("HUBKIT_HOME") or (Path.home() / ".claude" / "hubkit"))
LEDGER_PATH = Path(os.environ.get("LEDGER_PATH") or (HUBKIT_HOME / "state" / "ledger.jsonl"))
LOCK_PATH = Path(str(LEDGER_PATH) + ".lock")
HIGHWATER_PATH = Path(str(LEDGER_PATH) + ".highwater")
VALID_STATUSES = ("open", "active", "needs-operator", "done", "dropped")
VALID_ASKED_BY = ("operator", "hub", "worker")
VALID_EVENTS = {"created", "status", "proof", "ack", "surfaced", "note", "owner", "reviewed", "project", "triage"}
# Law-mix: triage classes the mix counts. Anything else, or no class at all,
# is counted as "unclassified". Kept generic: no lane names, card ids or site fields.
MIX_CLASSES = ("urgent", "important", "low-hanging")
MIX_UNCLASSIFIED = "unclassified"
# The only row fields a kit ledger row may carry. A fixture or export row with any other
# key (a lane name, a card id, a site field, a person's name) is not kit-generic.
GENERIC_ROW_FIELDS = frozenset({"id", "event", "title", "status", "class", "size", "ts", "lesson", "proof", "note"})
STALE_HOURS = 48
MAX_SURFACE = 5

# Proof types that are recorded and shown, but do not by themselves let an item
# be marked done: a claim about a metric is not evidence the work happened.
DONE_GATE_EXCLUDED_PROOF_TYPES = frozenset({"metric-claim", "metric-claim-verdict"})


@contextlib.contextmanager
def ledger_lock():
    """Exclusive flock on the sidecar `<ledger>.lock` file.

    Held across "read the file, pick the next id, append" so two concurrent
    `ledger add` calls can never be handed the same id. Commands that append
    an event for an already-known id hold it across that single append.
    """
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def claim_actor() -> dict | None:
    """Best-effort identity of the process making a claim (who, not just when).

    Claude Code sets CLAUDE_PID and CLAUDE_CODE_SESSION_ID for the lifetime of
    a session; every Bash call and subagent inherits them. Recording them on
    a `status active` event lets a reader tell a live claim from one whose
    session is gone. Returns None outside Claude Code: callers must treat that
    as "unknown claimant", never as "dead claimant".
    """
    pid_raw = os.environ.get("CLAUDE_PID")
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if not pid_raw and not sid:
        return None
    actor: dict = {}
    if pid_raw:
        try:
            actor["pid"] = int(pid_raw)
        except ValueError:
            pass
    if sid:
        actor["session_id"] = sid
    return actor or None


def parse_ts(ts: str) -> datetime:
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return datetime.now(timezone.utc)


def read_events() -> list[dict]:
    """All events in file order. Refuses (raises) on a malformed line rather
    than silently under-counting."""
    if not LEDGER_PATH.exists():
        return []
    events = []
    with LEDGER_PATH.open("r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                raise ValueError("Malformed ledger JSON; refusing incomplete counts")
            if not isinstance(value, dict):
                raise ValueError("Ledger record must be an object")
            events.append(value)
    return events


def append_event(event: dict) -> None:
    validate_event(event)
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event, ensure_ascii=False)
    # O_APPEND: each write is atomic for lines under the platform pipe buffer,
    # so concurrent sessions never interleave mid-line.
    fd = os.open(str(LEDGER_PATH), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, (line + "\n").encode("utf-8"))
    finally:
        os.close(fd)


def _read_highwater() -> int:
    """Highest id ever ISSUED, independent of what the file currently holds."""
    try:
        return int(HIGHWATER_PATH.read_text().strip() or 0)
    except Exception:
        return 0


def _write_highwater(n: int) -> None:
    try:
        HIGHWATER_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(str(HIGHWATER_PATH) + ".tmp")
        tmp.write_text(str(n))
        os.replace(str(tmp), str(HIGHWATER_PATH))
    except Exception:
        pass


def has_done_gate_proof(rec: dict) -> bool:
    """True iff `rec` has at least one proof that COUNTS toward the done gate.

    metric-claim / metric-claim-verdict entries stay visible on the record but
    are excluded here. A bare non-dict proof entry cannot be a metric claim
    (the CLI only writes typed {type, ref} dicts), so it counts.
    """
    for p in rec.get("proof", []):
        if isinstance(p, dict):
            if p.get("type") not in DONE_GATE_EXCLUDED_PROOF_TYPES:
                return True
        else:
            return True
    return False


def next_id(events: list[dict]) -> str:
    """Allocate the next id, monotonically, never reusing one.

    Taking max(ids in the file) + 1 would reuse ids whenever the file gets
    shorter than it once was (a git checkout, stash or merge that rewinds it),
    handing an in-use id to a different item. A sidecar high-water mark
    records the highest id ever issued and only moves up. Callers hold
    ledger_lock() across read, next_id and append.
    """
    max_n = 0
    for e in events:
        eid = e.get("id", "")
        if eid.startswith("L-"):
            try:
                max_n = max(max_n, int(eid[2:]))
            except ValueError:
                continue
    max_n = max(max_n, _read_highwater())
    new_n = max_n + 1
    _write_highwater(new_n)
    return f"L-{new_n:04d}"


def normalize_status(value):
    value = str(value or "").lower().replace("_", "-")
    return {"working": "active", "in-progress": "active", "planning": "active"}.get(value, value)


# A title once carried a live API key into the ledger and its caches. Refuse key-shaped text in any
# string field at write time. The message never echoes the value.
_KEY_SHAPED = re.compile(
    r"\bre_[A-Za-z0-9_]{24,}|\bsk-[A-Za-z0-9_-]{20,}|\bgh[pousr]_[A-Za-z0-9]{30,}"
    r"|\bxox[abprs]-[A-Za-z0-9-]{10,}|\bAKIA[0-9A-Z]{16}\b"
    r"|\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.|-----BEGIN [A-Z ]*PRIVATE KEY-----"
)


_FILER_BAD = re.compile(r"[^A-Za-z0-9._:+-]")


def _filer_safe(raw, fallback="unknown") -> str:
    """Clean a filer value to a short plain token; a key-shaped one is recorded as `redacted`, never stored."""
    raw = str(raw or "")
    if _KEY_SHAPED.search(raw):
        return "redacted"
    out = _FILER_BAD.sub("", raw)[:60]
    return "redacted" if _KEY_SHAPED.search(out) else (out or fallback)


def filer() -> dict:
    """Who filed a new item, recorded on its created event as `filed_by` {script, worker}.
    script = LEDGER_FILER if a script names itself, else `unknown`. worker = LEDGER_FILER_WORKER if set (an agent
    type such as builder), else `claude-session` inside a Claude Code session and `script` outside one.
    Never raises, never reads process arguments (they can carry titles or secrets)."""
    worker = os.environ.get("LEDGER_FILER_WORKER") or ("claude-session" if os.environ.get("CLAUDECODE") else "script")
    return {"script": _filer_safe(os.environ.get("LEDGER_FILER")), "worker": _filer_safe(worker)}


def _has_key_shaped(val, depth=0) -> bool:
    """True when `val`, or anything nested in it (dict keys and values, list/tuple items), is key-shaped text.
    `filed_by` is a dict, and the old check only read top-level strings and flat lists.
    Nesting deeper than 20 levels is refused as if it were key-shaped (fail closed, never a crash)."""
    if isinstance(val, str):
        return bool(_KEY_SHAPED.search(val))
    if depth > 20:
        return isinstance(val, (dict, list, tuple))
    if isinstance(val, dict):
        return any(_has_key_shaped(k, depth + 1) or _has_key_shaped(v, depth + 1) for k, v in val.items())
    if isinstance(val, (list, tuple)):
        return any(_has_key_shaped(v, depth + 1) for v in val)
    return False


def _refuse_key_shaped(event):
    if not isinstance(event, dict):
        return
    for field, val in event.items():
        if _has_key_shaped(val):
            raise ValueError(
                "Refused: field %r looks like a secret/API key. Secrets never go in the ledger; "
                "describe it without the value." % field)


def normalize_class(value) -> str:
    """Map a raw triage class to one of MIX_CLASSES, else MIX_UNCLASSIFIED. Never raises."""
    v = re.sub(r"[\s_]+", "-", str(value or "").strip().lower()) if isinstance(value, (str, int, float)) else ""
    return v if v in MIX_CLASSES else MIX_UNCLASSIFIED


def non_generic_fields(row) -> list[str]:
    """Keys on a row that are outside the kit-generic set (sorted). A non-dict row has none."""
    if not isinstance(row, dict):
        return []
    return sorted(k for k in row if k not in GENERIC_ROW_FIELDS)


def mix_counts(records: dict[str, dict]) -> dict[str, int]:
    """Law-mix: items per triage class (urgent / important / low-hanging / unclassified).

    Computed from the fold every time, never stored on a row. Missing or unknown
    classes land in "unclassified"; an empty ledger gives all zeros.
    """
    counts = {c: 0 for c in (*MIX_CLASSES, MIX_UNCLASSIFIED)}
    for rec in records.values():
        counts[normalize_class(rec.get("triage_class"))] += 1
    return counts


def mix_line(records: dict[str, dict]) -> str:
    c = mix_counts(records)
    return "Mix: " + " · ".join(f"{k} {c[k]}" for k in (*MIX_CLASSES, MIX_UNCLASSIFIED))


def validate_event(event):
    _refuse_key_shaped(event)
    if not isinstance(event, dict) or not str(event.get("id", "")).startswith("L-"):
        raise ValueError("Ledger event requires an id")
    kind = event.get("event")
    if kind not in VALID_EVENTS:
        raise ValueError("Unknown ledger event type")
    if kind == "status" and event.get("status") not in VALID_STATUSES:
        raise ValueError("Invalid ledger status")
    if kind == "created" and not event.get("title"):
        raise ValueError("Created event requires title")
    if kind == "proof" and not event.get("proof"):
        raise ValueError("Proof event requires evidence")
    if kind in {"note", "reviewed"} and not event.get("note"):
        raise ValueError("Note/reviewed requires text")
    if kind == "owner" and not event.get("owner"):
        raise ValueError("Owner event requires owner")
    if kind == "triage" and not str(event.get("class") or "").strip():
        raise ValueError("Triage event requires a class")
    if kind == "project" and not str(event.get("project") or "").strip():
        raise ValueError("Project event requires a non-empty project")


def fold(events: list[dict]) -> dict[str, dict]:
    """Fold events into one record per id, in file order, last write wins.

    Problems never raise and never rewrite history: they are recorded under
    the record's `schema_issues`. Unknown events never advance updated_at.
    """
    records: dict[str, dict] = {}
    for e in events:
        eid = e.get("id")
        if not eid:
            continue
        rec = records.setdefault(eid, {
            "id": eid, "title": "", "status": "open", "asked_on": None,
            "asked_by": "hub", "owner": "unowned", "trigger": "", "project": None,
            "proof": [], "last_surfaced": None, "operator_ack": False,
            "created_at": None, "updated_at": None, "notes": [],
            "schema_issues": [], "reviewed_upto": 0,
            "triage_class": None, "size": None, "lesson": None,
        })
        ts, ev = e.get("ts"), e.get("event")
        text = e.get("note")
        if ev == "created" and rec["title"] and e.get("title") != rec["title"]:
            # Same id, different title: the id was reused. Start the record
            # over so the old task's proof is never credited to the new one.
            rec["schema_issues"].append({"ts": ts, "event": ev, "reason": "reused id; older task remains in raw history"})
            rec.update(title="", status="open", project=None, proof=[], notes=[],
                       operator_ack=False, created_at=None, updated_at=None, owner="unowned")
        if ev == "project":
            # Append-only field change. The last project event wins; it never
            # advances updated_at (re-filing must not reset the stale nag).
            if str(e.get("project") or "").strip():
                rec["project"] = e["project"]
            continue
        if rec["project"] and e.get("project") and rec["project"] != e["project"]:
            rec["schema_issues"].append({"ts": ts, "event": ev, "reason": "cross-project id; event retained in raw ledger, not applied"})
            continue
        if e.get("lesson") and ev != "created":
            rec["lesson"] = str(e["lesson"])
        if ev == "triage":
            # Last triage wins. Class is kept raw; mix_counts normalises it at read time.
            rec["triage_class"] = e.get("class")
            if e.get("size"):
                rec["size"] = str(e["size"])
        elif ev == "created":
            rec["title"] = e.get("title") or ""
            rec["created_at"] = rec["created_at"] or ts
            for field in ("asked_by", "asked_on", "owner", "trigger", "project"):
                if e.get(field) is not None:
                    rec[field] = e[field]
            if isinstance(e.get("filed_by"), dict):   # who filed it: set at creation only
                rec["filed_by"] = {k: str(e["filed_by"].get(k) or "unknown") for k in ("script", "worker")}
        elif ev == "status":
            status = normalize_status(e.get("status"))
            if status not in VALID_STATUSES:
                rec["schema_issues"].append({"ts": ts, "event": ev, "reason": "invalid status"})
                continue
            rec["status"] = status
            if e.get("owner"):
                rec["owner"] = e["owner"]
            if e.get("proof"):
                rec["proof"].append(e["proof"])
            if text:
                rec["notes"].append({"ts": ts, "text": text})
        elif ev == "proof":
            if e.get("proof"):
                rec["proof"].append(e["proof"])
        elif ev == "ack":
            rec["operator_ack"] = bool(e.get("operator_ack", True))
        elif ev == "surfaced":
            rec["last_surfaced"] = ts
        elif ev == "owner":
            # Who holds an item and whether it is being worked (status active)
            # are separate questions, so this never touches status.
            if e.get("owner"):
                rec["owner"] = e["owner"]
        elif ev == "reviewed":
            # A human looked at the issues so far. Marks by position in the
            # append-only file, so an issue appended later lands past the
            # mark and re-flags. Deliberately does not advance updated_at.
            rec["reviewed_upto"] = len(rec["schema_issues"])
            if text:
                rec["notes"].append({"ts": ts, "text": text})
            continue
        elif ev == "note":
            if text:
                rec["notes"].append({"ts": ts, "text": text})
        else:
            rec["schema_issues"].append({"ts": ts, "event": ev, "reason": "unsupported event"})
            continue
        if ts:
            rec["updated_at"] = ts
    return records


def is_stale(rec: dict) -> bool:
    if rec["status"] not in ("open", "active"):
        return False
    updated = rec.get("updated_at")
    if not updated:
        return False
    return datetime.now(timezone.utc) - parse_ts(updated) > timedelta(hours=STALE_HOURS)


def needs_you(rec: dict) -> bool:
    """True only when the operator's own input is genuinely required.

    Finished work is not a decision: a done item never waits for an ack.
    (`ledger ack` still records one; it just never gates a display bucket.)
    """
    return rec["status"] == "needs-operator"


def unreviewed_issues(rec: dict) -> list[dict]:
    """The schema_issues that still need a human look (what the banner counts).

    A done or dropped item's history is settled by its status. On a live item
    only issues recorded after its last `reviewed` event count.
    """
    if rec.get("status") in ("done", "dropped"):
        return []
    return rec.get("schema_issues", [])[rec.get("reviewed_upto", 0):]


def banner_lines(records: dict[str, dict]) -> list[str]:
    """`Open: N · Needs you: M`, then WORKING or IDLE. Shared by `ledger
    banner` and the prompt hook so both render from the same fold."""
    items = list(records.values())
    open_n = sum(1 for r in items if r["status"] in ("open", "active", "needs-operator"))
    needs_you_n = sum(1 for r in items if needs_you(r))
    working_n = sum(1 for r in items if r["status"] == "active")
    lines = [f"Open: {open_n} · Needs you: {needs_you_n}"]
    lines.append(f"WORKING: {working_n}" if working_n > 0 else "IDLE")
    issues = sum(bool(unreviewed_issues(r)) for r in items)
    if issues:
        lines.append(f"Ledger history needs review: {issues} IDs; counts are provisional")
    return lines


def surface_lines(records: dict[str, dict], limit: int = MAX_SURFACE) -> list[str]:
    """Items to raise now, one line each, capped at `limit`.

    Order: needs-operator, then 48h-stale open/active items, then operator
    asks that were never started.
    """
    items = sorted(records.values(), key=lambda r: r["id"])
    printed: set[str] = set()
    lines: list[str] = []

    for r in items:
        if r["status"] == "needs-operator":
            lines.append(f"NEEDS-OPERATOR {r['id']}: {r['title']} (owner: {r['owner']})")
            printed.add(r["id"])

    for r in items:
        if r["id"] not in printed and is_stale(r):
            lines.append(f"STALE 48h+ {r['id']}: {r['title']} - still owned by {r['owner']}?")
            printed.add(r["id"])

    for r in items:
        if r["id"] not in printed and r["asked_by"] == "operator" and r["status"] == "open":
            lines.append(f"NEVER-STARTED {r['id']}: {r['title']} (asked {r['asked_on']})")
            printed.add(r["id"])

    return lines[:limit]
