#!/usr/bin/env python3
"""groomer: tidy a new to-do item with plain rules. No model call, no network, never raises.

Given the title of a new item (and optionally where it came from and the open items already on the list) it can:
  * write a finish line ("done means ...") when the item has none, from the title shape, then the kind, then the source
  * suggest a kind (bug / data / test-gap / polish / process) and a grade, as SUGGESTIONS only
  * find near-duplicates among open items (word overlap), so machine-made repeats can be folded into one
  * propose one strict VERIFY line when the title names one https page and one quoted marker (needs the verify-watch pattern)

Modes, set by files in $HUBKIT_HOME/state (a missing state folder is shadow, never live):
  off      file groom-off exists      -> nothing happens (kill switch)
  shadow   file groom-shadow exists   -> only counts are written to groom-events.jsonl, the item is not changed
  live     neither file               -> finish line filled when empty, groomed_* fields set, repeats reported

Telemetry is counts and ids only, never titles.

CLI:  groomer.py mode
      groomer.py groom --title "..." [--source S] [--done-means "..."]     prints the JSON result
      groomer.py similar --title "..." --items FILE.json                    FILE = [{"id","title","status","source"}]
A repeat is NEVER folded when: the new item's source or asked_by is a person (PROTECTED_SOURCES / PROTECTED_ASKERS), its grade is 9 or 10,
it is filed under its own ticket (--own-ticket), is a gate note (--gate-note), or has an origin (--origin); a candidate item whose
source or asked_by is a person is never a fold target.
Env: PROTECTED_ASKERS (comma list, default operator), HUBKIT_HOME (default ~/.claude/hubkit), GROOM_STATE_DIR (override), MACHINE_SOURCES (comma list, default
     alarm,worker-followup,gate-note), PROTECTED_SOURCES (comma list: asked by a person, never folded; default
     operator-chat,operator-tap,inbound-mail)
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
import time
import unicodedata
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOME = Path(os.environ.get("HUBKIT_HOME") or Path.home() / ".claude" / "hubkit")
FOLD_THRESHOLD = 0.7
MIN_TOKENS = 3
MAX_TITLE = 500
EVENTS_NAME = "groom-events.jsonl"


def _csv(name: str, default: str) -> frozenset:
    return frozenset(x.strip() for x in (os.environ.get(name) or default).split(",") if x.strip())


def machine_sources() -> frozenset:
    return _csv("MACHINE_SOURCES", "alarm,worker-followup,gate-note")


def protected_sources() -> frozenset:
    return _csv("PROTECTED_SOURCES", "operator-chat,operator-tap,inbound-mail")


def protected_askers() -> frozenset:
    """Values of an item's `asked_by` that mean a person asked for it: never folded, never a fold target."""
    return _csv("PROTECTED_ASKERS", "operator")


def is_protected(source, asked_by=None) -> bool:
    return source in protected_sources() or str(asked_by or "").strip().lower() in protected_askers()


def fold_grade_ok(grade) -> bool:
    """Only items graded 8 or lower (or not graded yet) may be folded away; 9-10 are never folded.
    A grade that is not a whole number counts as high (fail closed)."""
    if grade is None or grade == "":
        return True
    try:
        return int(grade) <= 8
    except (TypeError, ValueError):
        return False


DONE_TEMPLATES = {
    "alarm": "The alarm line stays absent for 24 hours and a newer recovery signal exists.",
    "gate-note": "The gate finding is fixed on the parent item and the next gate run passes.",
    "worker-followup": "The follow-up change is landed and its targeted check passes.",
    "inbound-mail": "The sender's question is answered or the reported problem is fixed and checked.",
    "operator-tap": "The decision is applied and the result is visible where it was made.",
    "operator-chat": "What was asked for is done and the operator can see it working.",
    "other": "The change is landed and a check on the real thing passes.",
}
KIND_DONE = {
    "bug": "The broken behavior no longer happens and a check on the real thing passes.",
    "data": "The data is correct against its source and stays correct on the next run.",
    "test-gap": "A test covers the gap and fails when the behavior is broken.",
    "polish": "The wording or look is changed and checked on the real page.",
}
_KIND_RULES = (
    ("bug", re.compile(r"\b(bug|broken|fail(?:s|ed|ing|ure)?|error|crash|wrong|regress\w*|stuck|(?<![-\w])down|500)\b", re.I)),
    ("data", re.compile(r"\b(data|stale|missing|count|numbers?|score[sd]?|ranking)\b", re.I)),
    ("test-gap", re.compile(r"\b(test|tests|coverage|mutant|assert\w*)\b", re.I)),
    ("polish", re.compile(r"\b(typo|wording|copy|polish|spacing|color|colour|padding|label)\b", re.I)),
)
_SUGGEST_GRADE = {"alarm": 8, "gate-note": 5, "worker-followup": 5, "operator-chat": 7, "operator-tap": 7, "inbound-mail": 7, "other": 5}
# Title-shaped finish lines win over the source template (read tasks and cleanups are not "the change is landed").
_TITLE_RULES = (
    (re.compile(r"^\W*(JUDGE NEEDED)\b", re.I), "A judge decision is written on the item and the held work moves on."),
    (re.compile(r"^\W*(read|pull|look up|find|count|list|measure|fetch|check)\b", re.I),
     "The answer is written on the item with where it came from."),
    (re.compile(r"^\W*(\[REVIEW\]\s*)?(trim|clean ?up|prune|remove|delete|reduce|cut|shrink)\b", re.I),
     "The cleanup is done and the before and after size is shown."),
    (re.compile(r"^\W*(daily|weekly|nightly)\b.*\bcheck\b", re.I),
     "The first check result is written on the item and the repeat check is scheduled."),
    (re.compile(r"^\W*(apply|run|restart|enable|disable|turn on|turn off)\b", re.I),
     "The step is done and a check on the real thing confirms it took effect."),
    (re.compile(r"^\W*(\[REVIEW\]\s*)?review\b", re.I),
     "The review result is written down and any change it calls for is landed or ruled out."),
)


# ---------------------------------------------------------------- state / modes

def state_dir() -> Path:
    return Path(os.environ.get("GROOM_STATE_DIR") or HOME / "state")


def mode(state: Path | None = None) -> str:
    """'off' | 'shadow' | 'live'. A missing state folder is shadow, never live. Any error is off."""
    try:
        state = state or state_dir()
        if not state.is_dir():
            return "shadow"
        if (state / "groom-off").exists():
            return "off"
        if (state / "groom-shadow").exists():
            return "shadow"
        return "live"
    except Exception:
        return "off"


def emit(event: str, source: str, state: Path | None = None, **counts) -> None:
    """One counts-only line. Never raises. No titles, no text."""
    try:
        state = state or state_dir()
        if not state.is_dir():
            return
        row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "event": event, "source": source}
        for k, v in counts.items():
            if isinstance(v, (int, bool)) or (isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,24}", v)):
                row[k] = v
        fd = os.open(str(state / EVENTS_NAME), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(fd, (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8"))
        finally:
            os.close(fd)
    except Exception:
        pass


# ---------------------------------------------------------------- rules

def tokens(title: str) -> frozenset:
    t = unicodedata.normalize("NFKC", str(title or "")[:MAX_TITLE]).casefold()
    return frozenset(re.findall(r"[a-z0-9]+", t))


def jaccard(a: frozenset, b: frozenset) -> float:
    return (len(a & b) / len(a | b)) if (a or b) else 0.0


def ident_tokens(tok: frozenset) -> frozenset:
    """Tokens carrying a digit (item ids, hashes, timestamps). Titles that differ in these name different things
    even when every other word matches."""
    return frozenset(x for x in tok if any(c.isdigit() for c in x))


def suggest_kind(title: str) -> str:
    for k, rx in _KIND_RULES:
        if rx.search(title or ""):
            return k
    return "process"


# Follow-ups, test-gap asks and hub fixes get a finish line that quotes the ask, and test-gap titles get the
# gap-check line even when a bug word ("wrong", "fail") is also in the title.
_ASK_SOURCES = ("worker-followup", "other", "gate-note")
_TESTGAP_TITLE = re.compile(r"\b(tests?|pins?|pinned|coverage|mutants?|assert\w*)\b", re.I)   # not "gate"/"pregate": prefixes and tool names
_LEAD_ID = re.compile(r"^\W*[A-Z]-\d+[^:]{0,40}:\s*", re.I)   # a leading "<ticket> r2 gate: " prefix


def ask_subject(title: str, limit: int = 90) -> str:
    """The ask itself, minus leading ticket ids and 'gate note:' prefixes, cut at a word boundary."""
    raw = re.sub(r"\s+", " ", str(title or "")[:MAX_TITLE]).strip()
    t = _LEAD_ID.sub("", raw, count=1).strip(" :,-") or raw
    if len(t) > limit:
        t = t[:limit].rsplit(" ", 1)[0].rstrip(" ,;:-") + "..."
    return t.replace('"', "'")


def done_line(source: str, kind: str | None, title: str | None = None) -> str:
    if title and source != "alarm":
        for rx, line in _TITLE_RULES:
            if rx.search(str(title)[:MAX_TITLE]):
                return line
    if title and source in _ASK_SOURCES:
        subj = ask_subject(title)
        if _TESTGAP_TITLE.search(str(title)[:MAX_TITLE]):
            return (f'A test for "{subj}" passes on the fix and fails when the gap is put back '
                    "(shown by one deliberate break).")
        if source == "worker-followup":
            return f'The follow-up "{subj}" is fixed as written and a check that names it passes.'
        if kind in KIND_DONE and source == "other":
            return f'For "{subj}": {KIND_DONE[kind][0].lower() + KIND_DONE[kind][1:]}'
        if source == "other":
            return f'"{subj}" is landed and a check on the real thing passes.'
    if kind in KIND_DONE and source in ("operator-chat", "operator-tap", "inbound-mail", "other"):
        return KIND_DONE[kind]
    return DONE_TEMPLATES.get(source, DONE_TEMPLATES["other"])


def find_similar(items, title: str, exclude: str | None = None, only_machine: bool = False):
    """[(id, jaccard)] of OPEN/ACTIVE items with word overlap >= FOLD_THRESHOLD, best first.
    items = list of {"id","title","status","source"} (or a dict id -> that without the id).
    Needs >= MIN_TOKENS words on both sides (a 1-2 word title is never judged a duplicate). With only_machine,
    items from a protected source are never matched."""
    if isinstance(items, dict):
        items = [dict(v, id=k) for k, v in items.items()]
    t = tokens(title)
    if len(t) < MIN_TOKENS:
        return []
    out = []
    machine, prot = machine_sources(), protected_sources()
    for r in items:
        if r.get("id") == exclude or r.get("status") not in ("open", "active"):
            continue
        if only_machine and (r.get("source") not in machine or r.get("source") in prot or is_protected(r.get("source"), r.get("asked_by"))):
            continue
        rt = tokens(r.get("title", ""))
        if len(rt) < MIN_TOKENS or ident_tokens(t) != ident_tokens(rt):
            continue
        j = jaccard(t, rt)
        if j >= FOLD_THRESHOLD:
            out.append((r.get("id"), j))
    out.sort(key=lambda x: (-x[1], str(x[0])))
    return out


def _verify_module():
    """The sibling verify-watch pattern, if installed. None when absent: no VERIFY suggestions then."""
    for p in (HERE.parent / "verify-watch" / "verify_watch.py", HOME / "verify-watch" / "verify_watch.py"):
        if p.is_file():
            spec = importlib.util.spec_from_file_location("verify_watch", p)
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
            return m
    return None


def suggest_verify(title, done_means, source) -> str | None:
    try:
        m = _verify_module()
        return m.suggest(title, done_means, source) if m else None
    except Exception:
        return None


# ---------------------------------------------------------------- the one call

def groom(title: str, source: str = "other", done_means: str = "", items=None, *, grade=None, own_ticket: bool = False,
          gate_note: bool = False, origin=None, asked_by=None) -> dict:
    """-> {"mode", "done_means" (only when filled), "kind", "grade", "fold_to" (a live machine repeat), "verify"}.
    Never raises; a failure returns {"mode": "off"}-like empty result so the item is filed as it was."""
    try:
        m = mode()
        res: dict = {"mode": m}
        if m == "off":
            return res
        source = source or "other"
        person = is_protected(source, asked_by)
        kind = suggest_kind(title)
        sim = []
        # Never fold: a person's item, a grade 9-10 item, a follow-up filed under its own ticket or as a gate note,
        # or anything with an origin (it came from a specific place and is not a repeat).
        if (items and source in machine_sources() and not person and not own_ticket and not gate_note and not origin
                and fold_grade_ok(grade)):
            sim = find_similar(items, title, only_machine=True)
        if sim:
            emit("groom_fold", source, mode=m, to=str(sim[0][0]))
            if m == "live":
                res["fold_to"] = sim[0][0]
                return res
        fill = not (done_means or "").strip()
        v = suggest_verify(title, done_means, source)
        if v:
            emit("groom_verify", source, mode=m, would=1)
        if fill:
            emit("groom_fill", source, mode=m, person=person)
        if m == "live":
            res["kind"] = kind
            res["grade"] = _SUGGEST_GRADE.get(source, 5)
            if fill:
                res["done_means"] = done_line(source, kind, title)
            if v:
                res["verify"] = v
        return res
    except Exception:
        return {"mode": "off"}


def main(argv) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="groomer.py")
    ap.add_argument("cmd", choices=["mode", "groom", "similar"])
    ap.add_argument("--title", default="")
    ap.add_argument("--source", default="other")
    ap.add_argument("--done-means", default="")
    ap.add_argument("--items")
    ap.add_argument("--grade")
    ap.add_argument("--own-ticket", action="store_true")
    ap.add_argument("--gate-note", action="store_true")
    ap.add_argument("--origin")
    ap.add_argument("--asked-by")
    a = ap.parse_args(argv[1:])
    if a.cmd == "mode":
        print(mode())
        return 0
    if a.cmd == "groom":
        print(json.dumps(groom(a.title, a.source, a.done_means, grade=a.grade, own_ticket=a.own_ticket, gate_note=a.gate_note,
                               origin=a.origin, asked_by=a.asked_by), ensure_ascii=False))
        return 0
    try:
        items = json.load(open(a.items)) if a.items else []
    except (OSError, ValueError):
        print("groomer: cannot read --items (need a readable JSON file)", file=sys.stderr)
        return 2
    if not isinstance(items, (list, dict)):
        print("groomer: --items must be a JSON list or object", file=sys.stderr)
        return 2
    try:
        print(json.dumps(find_similar(items, a.title)))
    except (AttributeError, TypeError):
        print("groomer: --items rows must be objects with id, title, status, source", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
