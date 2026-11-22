#!/usr/bin/env python3
"""card-guard: refuse a decision card that would show engineering text to the person who has to answer it.

check(header, context, options, require_why=False, require_proof=False) -> a reason string, or None when the card is fine.

A card is: a short question (header), a few lines of context, and 2 or more options, each with a label AND a plain
description of what it does. The guard refuses ticket ids, commit hashes, card ids, file names, [tags], raw JSON,
ISO timestamps, cut-off sentences, a header over 100 characters, options that are only Yes/No, and (when asked) a
card with no "Proof:" line or a "Why:" line that names no consequence.

CLI:  card_guard.py --header H --context C --option "Label|what it does" --option "..." [--require-why] [--require-proof]
      exit 0 ok, 1 refused (reason on stderr), 2 usage
"""
import re
import sys

# Every rule is checked on the header, the context and on each option label and description.
_RULES = [
    (r"(?i)\bL-\d{2,}\b", "a ticket id like L-0002"),
    (r"(?i)\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}\b(?:\.\.[0-9a-f]{7,40})?", "a commit hash"),
    (r"(?i)\b(?:q|tk)-[a-z0-9]{4,}", "a card id like q-xxxx"),
    (r"(?i)\bslices?\b", 'the word "slice"'),
    (r"(?i)\bmain\b", 'the word "main"'),
    (r"(?i)^\W*ALARM\b", "machine alarm text"),
    (r"(?i)\bk=[0-9a-f]{6,}", "k=<hex> alarm key"),
    (r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}", "an ISO timestamp"),
    (r"\[REDACTED", "a [REDACTED ...] title"),
    (r"\[\[", "internal double-bracket marker text"),
    (r"\.\.\.\s*$", "a cut-off (...) sentence"),
    (r"(?<![\w-])[a-z0-9]+(?:-[a-z0-9]+)+\.(?:py|sh|md|json|jsonl|mjs)\b", "a file name"),
    (r"\[[a-z0-9]+(?:[-_.][a-z0-9]+)+\]|\[[a-z][a-z0-9]{2,}\]", "a [script-name] bracket tag"),
    (r"(?s)^\s*[\[{].*[\]}]\s*$", "a raw JSON blob"),
]
_GENERIC = {"yes, go ahead", "no, hold off"}
MAX_HEADER = 100

_WHY_LINE = re.compile(r"(?m)^[ \t]*Why:[ \t]*(.*)$")
_PROOF_LINE = re.compile(r"(?m)^[ \t]*Proof:[ \t]*(\S.*)$")
# A "Why:" line must state a consequence. No length test: filler passes length tests.
_CONSEQ = re.compile(
    r"(?i)\b(because|otherwise|so that|so we|so you|means|would|will|won't|saves?|costs?|prevents?|stops?|avoids?|"
    r"cuts?|risks?|instead of|or else|if not|keeps?|fixes|breaks?|slows?|wastes?|loses?|lets? (?:us|you|him|her|them))\b")


def why_problem(context):
    ms = _WHY_LINE.findall(context or "")
    if not ms:
        return 'context needs a "Why:" line saying what happens if the recommended answer is picked (and what if not)'
    for line in ms:
        line = line.strip()
        if not line:
            continue
        m = _CONSEQ.search(line)
        if m and len(re.findall(r"[A-Za-z]{2,}", line[m.end():])) >= 1:
            return None
    if all(not x.strip() for x in ms):
        return 'the "Why:" line is empty'
    return 'the "Why:" line must state a consequence (what it saves, prevents, costs or changes), not just a label'


MAX_PROOF = 600
_PLACEHOLDER = {"tbd", "todo", "tba", "n/a", "na", "none", "null", "nil", "nothing", "unknown", "later", "pending", "wip", "xxx",
                "see report", "see my report", "see above", "see below", "see context", "see ticket", "in report", "in the report",
                "no proof", "no proof yet", "to be added", "to do", "to be determined", "coming soon", "proof", "proof here"}
_PROOF_ANY = re.compile(r"(?m)^[ \t]*Proof:[ \t]*(.*)$")


def proof_problem(context):
    """Reason string if the context has no usable Proof line, else None. Placeholder words (TBD, n/a, see my report) and
    anything with fewer than 8 letters/digits do not count; a proof line over MAX_PROOF characters is refused, not cut."""
    ms = _PROOF_ANY.findall(context or "")
    if not ms:
        return 'context needs a "Proof:" line (a link or plain words saying how the facts were checked)'
    best = None
    for raw in ms:
        t = " ".join(raw.split())
        if len(t) > MAX_PROOF:
            return 'the "Proof:" line is longer than %d characters (give the link or one plain sentence)' % MAX_PROOF
        key = re.sub(r"[^\w/ ]", "", t.casefold()).strip()
        if not t or key in _PLACEHOLDER:
            best = best or 'the "Proof:" line is empty or a placeholder (give a link or the real facts)'
            continue
        if len(re.findall(r"[^\W_]", t)) < 8:
            best = best or 'the "Proof:" line is too short to be proof (give a link or one plain sentence)'
            continue
        return None
    return best


# The marked option must be the one the card text recommends, or the card is refused. Anchored: the line must START
# (after an optional bullet/quote/bold) with "Recommended:", so "Not recommended: X" and "I would not recommend X" and
# Proof/Why lines that mention the word are never read as the recommendation.
_REC_LINE = re.compile(r"(?mi)^[ \t]*(?:(?:[-*+>#]+|\d+[.)])[ \t]+)*(?:\*\*|__)?[ \t]*(?:i[ \t]+)?recommend(?:ed|ation)?[ \t]*(?:answer|option)?[ \t]*(?:\*\*|__)?[ \t]*[:\-][ \t]*(?:\*\*|__)?[ \t]*(.+)$")
_SAY_REC = re.compile(r"(?mi)^[ \t]*(?:(?:[-*+>#]+|\d+[.)])[ \t]+)*(?:\*\*|__)?[ \t]*(?:why[ \t]*:[ \t]*)?(?:i|we)[ \t]+(?:strongly[ \t]+)?recommend\b(.*)$")


def _norm(s):
    return " ".join(re.sub(r"[^\w ]+", " ", (s or "").casefold()).split())


def _named(text, labels):
    """Indexes of options whose whole label appears (word boundaries) in text."""
    t = " " + _norm(text) + " "
    return [i for i, l in enumerate(labels) if l and (" " + l + " ") in t]


def recommend_problem(context, options, recommended, require_line=False):
    """Reason string if the marked option is not the one the card text recommends, else None."""
    opts = options or []
    if isinstance(recommended, bool) or not isinstance(recommended, int) or not 0 <= recommended < len(opts):
        return "the recommended option is missing or out of range (say which option you recommend)"
    labels = [_norm(o.get("label")) for o in opts]
    marked = (opts[recommended].get("label") or "").strip()
    bad = 'the card text recommends a different option than the one marked "%s" (the Recommended line and the marked option must match)' % marked
    lines = _REC_LINE.findall(context or "")
    for text in lines:
        n = _named(text, labels)
        if not n:
            return 'the "Recommended:" line must name an option label exactly (marked option is "%s")' % marked
        if recommended not in n:
            return bad
    if require_line and not lines:
        return 'context needs a line "Recommended: %s" naming the marked option' % marked
    for text in _SAY_REC.findall(context or ""):
        n = _named(text, labels)
        if n and recommended not in n:
            return bad
    return None


def check(header, context, options, require_why=False, require_proof=False, recommended=None, check_recommended=False,
          require_rec_line=False):
    h, c = (header or "").strip(), (context or "").strip()
    if not h:
        return "empty header"
    if not c:
        return "empty context"
    if len(h) > MAX_HEADER:
        return "header longer than %d characters (write a short question)" % MAX_HEADER
    for where, text in (("header", h), ("context", c)):
        for rx, what in _RULES:
            if re.search(rx, text):
                return "%s has %s (write it in everyday words)" % (where, what)
    opts = options or []
    if len(opts) < 2:
        return "needs at least 2 options"
    labs = []
    for o in opts:
        lab, desc = (o.get("label") or "").strip(), (o.get("description") or "").strip()
        if not lab or not desc:
            return 'every option needs a label and a plain description of what it does (write --option "Label|what it does")'
        for rx, what in _RULES:
            if re.search(rx, lab) or re.search(rx, desc):
                return "an option has %s" % what
        labs.append(lab.lower())
    if set(labs) <= _GENERIC:
        return "options are only the generic Yes/No (say what each option does)"
    if check_recommended:
        rp = recommend_problem(c, opts, recommended, require_line=require_rec_line)
        if rp:
            return rp
    if require_proof:
        pp = proof_problem(c)
        if pp:
            return pp
    if require_why:
        wp = why_problem(c)
        if wp:
            return wp
    return None


def main(argv):
    import argparse
    ap = argparse.ArgumentParser(prog="card_guard.py")
    ap.add_argument("--header", default="")
    ap.add_argument("--context", default="")
    ap.add_argument("--option", action="append", default=[])
    ap.add_argument("--require-why", action="store_true")
    ap.add_argument("--require-proof", action="store_true")
    a = ap.parse_args(argv[1:])
    opts = []
    for o in a.option:
        lab, _, desc = o.partition("|")
        opts.append({"label": lab, "description": desc})
    why = check(a.header, a.context, opts, a.require_why, a.require_proof)
    if why:
        print("card_guard: refused: " + why, file=sys.stderr)
        return 1
    print("ok")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
