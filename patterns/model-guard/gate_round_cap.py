"""Two spawn-time checks used by model-guard.py.

1. Gate-round cap: a bug-gate spawn that would be the 4th (or later) review of the same primary ticket is refused
   unless the ledger holds a note starting `CUT-OR-CONTINUE:` for that ticket written AFTER the previous review spawn.
   Warn-only for the first 7 days (marker file), then it refuses. There is no exemption: every bug-gate spawn
   counts, including one whose brief carries a `REVIEW-MODE:` line (the exemption was the bypass and was cut).
   Hard ceiling: once a ticket has had GATE_HARD_CEILING reviews (default 4) of any result, no note unlocks another;
   ship with notes or cut. Set GATE_HARD_CEILING=0 to switch the ceiling off.
2. Many-deliverables warning: a builder brief with >= DELIV_WARN list items gets a warning (never a block).

Pure functions + file reads only. Every error returns None (fail open). No process is spawned, nothing executed."""
import datetime
import json
import os
import re

ROUND_LIMIT = 3          # prior reviews allowed before the cut-or-continue note is required (so the 4th needs it)
WARN_DAYS = 7
DELIV_WARN = 7
HARD_CEILING_DEFAULT = 4  # reviews of any result per ticket after which no note unlocks another
NOTE_WINDOW = 3          # one CUT-OR-CONTINUE note covers the next NOTE_WINDOW reviews after its timestamp
_TICKET_RE = re.compile(r"\bL-\d{3,5}\b", re.I)
_SECOND_OPINION = re.compile(r"^ {0,3}REVIEW-MODE:[ \t]*(for-and-against|second-opinion)[ \t]*\r?$", re.I | re.M)
_FENCE_OPEN = re.compile(r" {0,3}(`{3,}(?=[^`]*$)|~{3,})")
_REPORT_LINE = re.compile(r"^[ \t]*Report:[^\n]*", re.I)
_LIST_ITEM = re.compile(r"^[ \t]*(?:\d{1,2}[.)]|\([a-hA-H]\)|[a-hA-H][.)])[ \t]+\S", re.M)


def _paths():
    home = os.path.expanduser(os.environ.get("HUBKIT_HOME") or "~/.claude/hubkit")
    return (os.environ.get("GATE_SPAWNS_LOG") or os.path.join(home, "state", "gate-spawns.jsonl"),
            os.environ.get("GATE_CAP_LEDGER") or os.path.join(home, "state", "ledger.jsonl"),
            os.environ.get("GATE_CAP_MARKER") or os.path.join(home, "state", "gate-round-cap-warn-since"))


def _ts(s):
    try:
        return datetime.datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def primary_ticket(desc, prompt):
    """Case-insensitive, upper-cased: description, then the first 300 chars of the prompt, then the whole prompt."""
    desc, prompt = str(desc or ""), str(prompt or "")
    m = _TICKET_RE.search(desc) or _TICKET_RE.search(prompt[:300])
    if m:
        return m.group(0).upper()
    # whole-prompt fallback. Prefer the ticket in the line-1 `Report:` path, then the one named most often
    # (ties: the earliest), so one late passing mention of an older ticket cannot become the primary.
    first = prompt.split("\n", 1)[0] if prompt else ""
    rl = _REPORT_LINE.match(first)
    m = _TICKET_RE.search(rl.group(0)) if rl else None
    if m:
        return m.group(0).upper()
    found = [x.upper() for x in _TICKET_RE.findall(prompt)]
    if not found:
        return None
    counts = {}
    for x in found:
        counts[x] = counts.get(x, 0) + 1
    best = max(counts.values())
    return next(x for x in found if counts[x] == best)


_MAX_CONTAINERS = 32     # deeper nesting is read as plain text (bounded work per line)
_BQ = re.compile(r" {0,3}> ?")
_LI = re.compile(r"( {0,3})([-*+]|\d{1,9}[.)])( *)")
_HR = re.compile(r" {0,3}([-*_])(?: *\1){2,} *$")
_ATX = re.compile(r" {0,3}#{1,6}(?:[ ]|$)")
_SPACES = re.compile(r" *")
_CLOSE = re.compile(r" {0,3}(`{3,}|~{3,})[ \t]*$")
# CommonMark HTML blocks 1-5 (own end condition), 6 and 7 (end at a blank line; 7 cannot interrupt a paragraph).
_HTML_END = (
    (re.compile(r" {0,3}<(?:pre|script|style|textarea)(?:[ \t>]|$)", re.I), re.compile(r"</(?:pre|script|style|textarea)>", re.I)),
    (re.compile(r" {0,3}<!--"), re.compile(r"-->")),
    (re.compile(r" {0,3}<\?"), re.compile(r"\?>")),
    (re.compile(r" {0,3}<![A-Za-z]"), re.compile(r">")),
    (re.compile(r" {0,3}<!\[CDATA\["), re.compile(r"\]\]>")),
)
_HTML6 = re.compile(
    r" {0,3}</?(?:address|article|aside|base|basefont|blockquote|body|caption|center|col|colgroup|dd|details|dialog|dir|div|dl|dt|"
    r"fieldset|figcaption|figure|footer|form|frame|frameset|h[1-6]|head|header|hr|html|iframe|legend|li|link|main|menu|menuitem|"
    r"nav|noframes|ol|optgroup|option|p|param|section|source|summary|table|tbody|td|tfoot|th|thead|title|tr|track|ul)"
    r"(?:[ \t]|/?>|$)", re.I)


_HTML7 = re.compile(
    r" {0,3}(?:<[A-Za-z][A-Za-z0-9-]*(?:[ \t]+[A-Za-z_:][\w:.-]*(?:[ \t]*=[ \t]*(?:[^ \t\"'=<>`]+|'[^']*'|\"[^\"]*\"))?)*[ \t]*/?>"
    r"|</[A-Za-z][A-Za-z0-9-]*[ \t]*>)[ \t]*$")


def strip_fences(text):
    """Drop fenced code blocks and HTML blocks, container-aware: a fence nested in a blockquote or list item
    (any mix, any depth up to _MAX_CONTAINERS) is dropped too, and it ends where its container ends. A closing fence must
    use the opening fence char, be at least as long, carry nothing after it, and sit at the same container depth (a ```
    line inside a ```` block does not close it). An unterminated fence runs to the end of its container. Lines that are
    not fenced are returned unchanged (original bytes), so the REVIEW-MODE column rule still applies to them."""
    out = []
    stack = []        # ('q',) blockquote or ('l', w) list item whose content starts w columns in
    fence = None      # (char, length, depth)
    html = None       # None | 'blank' | compiled end regex
    html_depth = 0
    para = False      # previous line was paragraph text (lazy continuation, list-interrupt rules)
    for raw in str(text or "").split("\n"):
        s = raw.rstrip("\r").expandtabs(4)
        pos, matched = 0, 0
        for c in stack:
            if c[0] == "q":
                m = _BQ.match(s, pos)
                if not m:
                    break
                pos = m.end()
            else:
                ind = _SPACES.match(s, pos).end() - pos
                if pos + ind >= len(s):
                    pass                      # blank line: the item continues
                elif ind >= c[1]:
                    pos += c[1]
                else:
                    break
            matched += 1
        blank = _SPACES.match(s, pos).end() >= len(s)
        if fence is not None:
            if matched >= fence[2]:
                m = _CLOSE.match(s, pos)
                if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= fence[1]:
                    fence = None
                continue
            fence = None                       # its container ended: the fence ended with it
        if html is not None:
            if matched >= html_depth:
                if html == "blank":
                    if blank:
                        html = None
                        out.append(raw)
                    continue
                if html.search(s, pos):
                    html = None
                continue
            html = None
        if matched < len(stack):
            # not every container continues: lazy paragraph continuation, or the unmatched containers close
            lazy = (para and not blank and not _FENCE_OPEN.match(s, pos) and not _BQ.match(s, pos)
                    and not _HR.match(s, pos) and not _ATX.match(s, pos) and not _LI.match(s, pos)
                    and not any(h[0].match(s, pos) for h in _HTML_END) and not _HTML6.match(s, pos))
            if lazy:
                out.append(raw)
                continue
            del stack[matched:]
        # open new containers on the rest of the line
        while len(stack) < _MAX_CONTAINERS:
            m = _BQ.match(s, pos)
            if m:
                stack.append(("q",))
                pos, para = m.end(), False
                continue
            m = _LI.match(s, pos)
            empty = False
            if m and not _HR.match(s, pos):
                empty = m.end() >= len(s)
                if m.group(3) == "" and not empty:
                    m = None                   # "-foo" / "1.foo" is not a list item
                elif para and (empty or (m.group(2)[0].isdigit() and int(m.group(2)[:-1]) != 1)):
                    m = None                   # cannot interrupt a paragraph
            else:
                m = None
            if m:
                gap = len(m.group(3))
                w = len(m.group(1)) + len(m.group(2)) + (gap if 1 <= gap <= 4 else 1)
                stack.append(("l", w))
                pos = pos + (len(m.group(1)) + len(m.group(2)) if empty else w)
                para = False
                continue
            break
        ind = _SPACES.match(s, pos).end() - pos
        if pos + ind >= len(s):
            para = False
            out.append(raw)
            continue
        if ind >= 4:
            out.append(raw)                    # indented code, or paragraph continuation: never a fence or marker host
            continue
        m = _FENCE_OPEN.match(s, pos)
        if m:
            fence = (m.group(1)[0], len(m.group(1)), len(stack))
            para = False
            continue
        started = False
        for start_re, end_re in _HTML_END:
            if start_re.match(s, pos):
                started = True
                if not end_re.search(s, pos + 1):
                    html, html_depth = end_re, len(stack)
                break
        if not started and (_HTML6.match(s, pos) or (not para and _HTML7.match(s, pos))):
            started, html, html_depth = True, "blank", len(stack)
        if started:
            para = False
            continue
        para = not (_HR.match(s, pos) or _ATX.match(s, pos))
        out.append(raw)
    return "\n".join(out)


def is_exempt(prompt):
    """True only for an own-line `REVIEW-MODE: for-and-against|second-opinion` outside any code fence."""
    try:
        return bool(_SECOND_OPINION.search(strip_fences(prompt)))
    except Exception:
        return False


def prior_reviews(ticket, spawns_path):
    """(count, [ts, ...]) of earlier bug-gate spawns whose primary is `ticket` (rows marked exempt count too)."""
    n, stamps = 0, []
    with open(spawns_path, "rb") as f:
        for raw in f:
            if ticket.encode() not in raw:
                continue
            try:
                r = json.loads(raw)
            except Exception:
                continue
            if r.get("subagent_type") == "bug-gate" and r.get("primary") == ticket:
                n += 1
                stamps.append(_ts(r.get("ts")) or 0.0)
    return n, stamps


def has_cut_note(ticket, ledger_path, stamps):
    try:
        f = open(ledger_path, "rb")
    except FileNotFoundError:
        return False  # no ledger = no note: the cap still applies (an unreadable ledger fails open via the caller)
    best = None
    with f:
        for raw in f:
            if ticket.encode() not in raw or b"CUT-OR-CONTINUE" not in raw:
                continue
            try:
                r = json.loads(raw)
            except Exception:
                continue
            if r.get("id") == ticket and r.get("event") == "note" and str(r.get("note", "")).lstrip().startswith("CUT-OR-CONTINUE:"):
                t = _ts(r.get("ts")) or 0.0
                best = t if best is None else max(best, t)
    if best is None:
        return False
    # one note covers the next NOTE_WINDOW reviews after it
    return sum(1 for x in stamps if x > best) < NOTE_WINDOW


def enforcing(marker, now=None):
    """True once the warn-only marker is >= WARN_DAYS old. The marker is created on first use (starts the 7 days)."""
    now = now or datetime.datetime.now().timestamp()
    try:
        age = now - os.stat(marker).st_mtime
    except FileNotFoundError:
        os.makedirs(os.path.dirname(marker) or ".", exist_ok=True)
        with open(marker, "w") as f:
            f.write("gate-round-cap warn-only since %s\n" % datetime.datetime.now().astimezone().isoformat(timespec="seconds"))
        return False
    return age >= WARN_DAYS * 86400


def hard_ceiling():
    """Reviews of any result after which nothing unlocks another (0 = off). A bad value keeps the default."""
    raw = os.environ.get("GATE_HARD_CEILING")
    if raw is not None and raw.strip().isdigit():
        return int(raw.strip())
    return HARD_CEILING_DEFAULT


def check_round_cap(tin, prompt):
    """Returns None, ('warn', msg) or ('block', msg). Fails open (None) on any error."""
    try:
        if str(tin.get("subagent_type") or "").strip().lower() != "bug-gate":
            return None
        desc = str(tin.get("description") or "")
        # no REVIEW-MODE exemption: every bug-gate counts
        t = primary_ticket(desc, prompt)
        if not t:
            return None
        spawns, ledger, marker = _paths()
        n, stamps = prior_reviews(t, spawns)
        if n < ROUND_LIMIT:
            return None
        ceil = hard_ceiling()
        if ceil and n >= ceil:
            msg = ("ceiling %d reached: %s already has %d reviews of any result, and %d is the hard stop; no note or "
                   "ruling lifts it. Ship with notes or cut; never re-file the work as a fresh ticket." % (ceil, t, n, ceil))
            if enforcing(marker):
                return ("block", msg)
            return ("warn", "WARN-ONLY for 7 days from first sight : " + msg)
        if has_cut_note(t, ledger, stamps):
            return None
        msg = ("this would be review round %d of %s (the limit is %d without a decision). Write a ledger note "
               "`CUT-OR-CONTINUE: <cut | continue, why>` on %s "
               "and retry. There is no REVIEW-MODE exemption."
               % (n + 1, t, ROUND_LIMIT, t))
        if enforcing(marker):
            return ("block", msg)
        return ("warn", "WARN-ONLY for 7 days from first sight : " + msg)
    except Exception:
        return None


def check_deliverables(tin, prompt):
    """Warning text for a builder brief that lists many deliverables, else None. Never blocks."""
    try:
        if str(tin.get("subagent_type") or "").strip().lower() != "builder":
            return None
        n = len(_LIST_ITEM.findall(prompt))
        if n >= DELIV_WARN:
            return ("this builder brief lists %d numbered/lettered items. Runs over 60 steps cost about 6x a typical run; "
                    "split it into slices of at most 6 deliverables each . Warning only." % n)
    except Exception:
        pass
    return None
