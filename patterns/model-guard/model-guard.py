#!/usr/bin/env python3
"""PreToolUse hook: every subagent call must name its model.

Why: a subagent started without a model silently inherits whatever model your main
chat happens to be on. That changes with no warning (you switch the main chat to a
bigger model and every helper gets pricier and slower). This hook makes the choice
deliberate. Any model is fine, including a bigger one than the main chat, as long
as the call says which. What is refused is leaving it unset.

Behaviour (Claude Code sends the tool call as JSON on stdin):
  tool Agent or Task : refuse (exit 2, reason on stderr, which goes back to Claude)
                       when the "model" field is missing, blank, or "inherit".
                       If MODEL_GUARD_ALLOWED is set (comma list, e.g. "haiku,sonnet"),
                       a model outside that list is refused too.
  anything else      : ignored (Workflow too: its agent()/model: count advisory was cut).

Report path (optional, report-gate beside this one): a builder, bug-gate or playtester brief must carry a
usable report path (or the line "Report: none"), the same rule report-gate applies when the worker stops.
Refusing at spawn is cheaper than refusing at stop. A "Report:" line containing ".." or a control character
is refused too. Without the report-gate folder, nothing changes. MODEL_GUARD_REPORT_CHECK=0 switches it off.

Failure modes: a spawn (Agent or Task) fails CLOSED on anything the hook cannot read or classify: an
unreadable or non-object payload, a non-string tool_name, a non-object tool_input, or a crash mid-check
(the call is refused with a message to re-send it as a normal Agent call). Any other tool (Workflow
and the rest) still fails OPEN if this hook itself breaks. A block always leaves with exit 2, even if
stderr is broken. An unwritable log never changes a decision.

Gate index: an allowed builder or bug-gate spawn also appends one line to
$GATE_SPAWNS_LOG (default $HUBKIT_HOME/state/gate-spawns.jsonl). gate-metrics reads it.
It is observability only: if it cannot be written (unwritable, a symlink, a folder), the
spawn still goes through.

Ticket lock: if the ticket-lock pattern is installed beside this one, the last gate before an allowed
spawn takes a per-ticket lock (one owner per ticket; see patterns/ticket-lock). A second builder
for a ticket that is already held is refused, unless the prompt has a line "HANDOFF-FROM: <owner>"
or contains DUP-OK. Without the ticket-lock folder, or on any error inside it, nothing changes.

Gate round cap (optional, gate_round_cap.py beside this file): a bug-gate spawn that would be the 4th
review of one ticket is refused after a 7-day warn-only period unless the ledger has a note starting
"CUT-OR-CONTINUE:" written after the previous review. There is no exemption: a "REVIEW-MODE:" line
counts like any other review. A builder brief with 7 or
more numbered items gets a split-it warning. Warnings only, except the cap after day 7. Fails open.

Gate admission (optional, selfcheck_admit.py beside this file): a bug-gate brief that names a commit after a cue word
(tip, HEAD, sha, commit, at) is checked against the builder's own record for that commit
(<git-common-dir>/selfcheck/<sha> or /pregate/<sha>, exit 0, clean). Warn-only for 7 days after
$HUBKIT_HOME/state/gate-admit-warn-since, then a red or missing record is refused. A whole line
"SELFCHECK-WAIVED: <reason>" lets a spawn through. An unresolvable repo, an unreadable record or any error fails open.

Log: $MODEL_GUARD_LOG, default $HUBKIT_HOME/state/model-guard.log
     (HUBKIT_HOME defaults to ~/.claude/hubkit). One line per decision.
"""
import datetime
import json
import os
import re
import sys


def log_path():
    if os.environ.get("MODEL_GUARD_LOG"):
        return os.environ["MODEL_GUARD_LOG"]
    home = os.path.expanduser(os.environ.get("HUBKIT_HOME") or "~/.claude/hubkit")
    return os.path.join(home, "state", "model-guard.log")


def log(line):
    try:
        p = log_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "a") as f:
            f.write(f"{datetime.datetime.now().isoformat(timespec='seconds')} {line}\n")
    except Exception:
        pass


def spawn_index_path():
    if os.environ.get("GATE_SPAWNS_LOG"):
        return os.environ["GATE_SPAWNS_LOG"]
    home = os.path.expanduser(os.environ.get("HUBKIT_HOME") or "~/.claude/hubkit")
    return os.path.join(home, "state", "gate-spawns.jsonl")


TICKET_RE = re.compile(r"\bL-\d{3,5}\b")
SLICE_RE = re.compile(r"\b[SR]\d{1,2}\b")


def record_gate_spawn(tin, model, session_id):
    """One JSON line per ALLOWED builder or bug-gate spawn, for gate-metrics.
    Observability only: a single O_APPEND write (so same-second spawns never interleave),
    never follows a symlinked index, and ANY failure is swallowed. It cannot change the decision."""
    try:
        stype = str(tin.get("subagent_type") or "").strip().lower()
        if stype not in ("builder", "bug-gate"):
            return
        desc = str(tin.get("description") or "")
        prompt = str(tin.get("prompt") or "")
        tickets = sorted(set(TICKET_RE.findall(desc))) or TICKET_RE.findall(prompt[:400])[:1]
        m = SLICE_RE.search(desc) or SLICE_RE.search(prompt[:400])
        exempt = False
        primary = None
        try:  # the same primary-ticket / exemption rules the round cap uses; fail open to the old behaviour
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            import gate_round_cap as _g
            exempt = stype == "bug-gate" and _g.is_exempt(prompt)
            primary = _g.primary_ticket(desc, prompt)
        except Exception:
            primary = tickets[0] if tickets else None
        rec = {"ts": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
               "session_id": str(session_id or "")[:80], "subagent_type": stype, "model": model,
               "tickets": tickets, "primary": primary, "slice": m.group(0) if m else None, "description": desc[:300]}
        if exempt:
            rec["exempt"] = True
        p = spawn_index_path()
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
        fd = os.open(p, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o644)
        try:
            os.write(fd, (json.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8"))
        finally:
            os.close(fd)
    except BaseException as e:  # noqa: BLE001 - must never block or change the hook decision
        log(f"GATE-SPAWN index write failed (ignored): {type(e).__name__}")


def _lock_module():
    """The ticket_lock library, or None (fail-open: a missing library means no lock)."""
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        home = os.path.expanduser(os.environ.get("HUBKIT_HOME") or "~/.claude/hubkit")
        for d in (os.path.join(here, "..", "ticket-lock"), os.path.join(home, "ticket-lock")):
            if os.path.isfile(os.path.join(d, "ticket_lock.py")):
                sys.path.insert(0, d)
                break
        else:
            return None
        import ticket_lock
        return ticket_lock
    except Exception:
        return None


def ticket_lock_message(tin, session_id, tool_use_id):
    """The ticket-lock refusal text, or None. Fail-open: a missing library or any error returns None."""
    try:
        tl = _lock_module()
        return tl.acquire(tin, str(session_id or ""), str(tool_use_id or "")) if tl else None
    except Exception:
        return None


# One line per ALLOWED spawn of ANY agent type (type + model only), so you can measure which model tiers
# your subagents really use. Separate from the gate-spawn index, which only lists builders and bug-gates.
# Observability only: it never changes the decision and any failure is swallowed.
def model_spawns_path():
    if os.environ.get("MODEL_SPAWNS_LOG"):
        return os.environ["MODEL_SPAWNS_LOG"]
    return os.path.join(os.path.dirname(spawn_index_path()) or ".", "model-spawns.jsonl")


def record_model_spawn(tin, model):
    try:
        rec = {"ts": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
               "type": str(tin.get("subagent_type") or "general-purpose").strip().lower()[:41],
               "model": str(model)[:40]}
        p = model_spawns_path()
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
        fd = os.open(p, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o644)
        try:
            os.write(fd, (json.dumps(rec) + "\n").encode("utf-8"))
        finally:
            os.close(fd)
    except BaseException:  # noqa: BLE001
        pass


OWNER_STOP_WINDOW = 3600  # seconds a refusal counts toward the 3x stop


def owner_stop_path():
    if os.environ.get("MODEL_GUARD_STOP_STATE"):
        return os.environ["MODEL_GUARD_STOP_STATE"]
    home = os.path.expanduser(os.environ.get("HUBKIT_HOME") or "~/.claude/hubkit")
    return os.path.join(home, "state", "model-guard-owner-stop.json")


def owner_refusals(tin, prompt):
    """Record one owner refusal of this exact payload and return how many happened in the last hour, this one
    included. Keyed by a hash of type + description + prompt. 1 on any error (the normal refusal then runs)."""
    try:
        import hashlib
        import time
        key = hashlib.sha256("\x00".join((str(tin.get("subagent_type") or ""), str(tin.get("description") or ""),
                                          prompt)).encode("utf-8", "replace")).hexdigest()[:24]
        now = time.time()
        path = owner_stop_path()
        try:
            with open(path) as fh:
                st = json.load(fh)
            if not isinstance(st, dict):
                st = {}
        except Exception:
            st = {}
        st = {k: [t for t in v if isinstance(t, (int, float)) and now - t < OWNER_STOP_WINDOW]
              for k, v in st.items() if isinstance(v, list)}
        st = {k: v for k, v in st.items() if v}
        st.setdefault(key, []).append(now)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".%d.tmp" % os.getpid()
        with open(tmp, "w") as fh:
            json.dump(st, fh)
        os.replace(tmp, path)
        return len(st[key])
    except BaseException:  # noqa: BLE001
        return 1


ADVICE = []  # warn-only notes, printed once as PreToolUse additionalContext

SPAWN_TOOLS = ("Agent", "Task")

# A spawn brief for these worker types must name where its report goes (when report-gate is installed).
REPORT_TYPES = frozenset(("builder", "bug-gate", "playtester"))
_FENCE_RE = re.compile(r"^[ \t]*(?P<opener>`{3,})[^`\n]*\n.*?(?:^[ \t]*(?P=opener)`*[ \t]*\r?$|\Z)", re.S | re.M)
_HANDOFF_RE = re.compile(r"^[ \t]*HANDOFF-FROM:[ \t]*\S", re.M)
_REPORT_TAG_RE = re.compile(r"^[ \t>\-]*(?:\d+[.)][ \t]+)?report[ \t]*:([^\n]*)$", re.I | re.M)
_DOTDOT_RE = re.compile(r"(?:^|/)\.\.(?:/|$)")
_RG_MOD = []


def report_check_on():
    return (os.environ.get("MODEL_GUARD_REPORT_CHECK", "1").strip().lower() not in ("0", "false", "no", "off"))


def _rg():
    """report-gate.py loaded by file path from the folder beside this one (or $HUBKIT_HOME/report-gate): one source
    of truth for what counts as a report path and for "Report: none". None when it is not installed or cannot load."""
    if not _RG_MOD:
        mod = None
        try:
            import importlib.util
            here = os.path.dirname(os.path.abspath(__file__))
            home = os.path.expanduser(os.environ.get("HUBKIT_HOME") or "~/.claude/hubkit")
            for d in (os.path.join(here, "..", "report-gate"), os.path.join(home, "report-gate")):
                f = os.path.join(d, "report-gate.py")
                if os.path.isfile(f):
                    sp = importlib.util.spec_from_file_location("report_gate_for_model_guard", f)
                    m = importlib.util.module_from_spec(sp)
                    sp.loader.exec_module(m)
                    if hasattr(m, "_is_exempt") and (hasattr(m, "REPORT_PATH_RX") or hasattr(m, "REPORT_PATH_RE")):
                        mod = m
                    break
        except Exception as e:
            log(f"REPORT rg-import-fail {type(e).__name__}")
        _RG_MOD.append(mod)
    return _RG_MOD[0]


def report_path_problem(prompt):
    """None unless report-gate would refuse the worker at stop: no report path anywhere in the brief (and no
    "Report: none"), or a hostile Report: line or path (".." or a control character). Pure text, never touches
    the disk. None (allow) when report-gate is not installed."""
    rg = _rg()
    if rg is None:
        return None
    rx = getattr(rg, "REPORT_PATH_RX", None) or getattr(rg, "REPORT_PATH_RE")
    for raw in _REPORT_TAG_RE.findall(prompt.replace("*", "")):  # hostile spellings are refused even next to a good path
        v = raw.rstrip("\r")
        if any((ord(c) < 32 and c != "\t") or ord(c) == 127 for c in v):
            return "control character in a Report: line"
        if any(_DOTDOT_RE.search(t) for t in v.split() if "/" in t):
            return "'..' in a Report: path"
    matches = [m.group(0) for m in rx.finditer(prompt)]
    if any(_DOTDOT_RE.search(x) for x in matches):
        return "'..' in a report path"
    if rg._is_exempt(prompt) or matches:
        return None
    if any(not raw.strip(" \t`'\"\r") for raw in _REPORT_TAG_RE.findall(prompt)):
        return "empty 'Report:' value and no report path anywhere"
    return "no report path anywhere in the brief"


def allowed_models():
    raw = os.environ.get("MODEL_GUARD_ALLOWED") or ""
    return [m.strip().lower() for m in raw.split(",") if m.strip()]


def decide(data):
    """Returns (exit_code, message_for_stderr or None, log_line)."""
    tool = data.get("tool_name") or ""
    tin = data.get("tool_input") or {}

    if tool not in ("Agent", "Task"):  # Workflow and every other tool pass untouched (the agent()/model: advisory was cut)
        return 0, None, ""

    model = str(tin.get("model") or "").strip().lower()
    desc = str(tin.get("description") or "?")[:80]
    if not model or model == "inherit":
        return (2,
                "Blocked: every subagent call states its model explicitly. Leaving it unset "
                "(or 'inherit') silently takes whatever model the main chat is on. Pick by the "
                "shape of the task (a small fast model for reading and searching, a stronger "
                "one for building and judging) and re-issue this call with a model parameter.",
                f"BLOCKED unset model: {desc}")
    allowed = allowed_models()
    if allowed and model not in allowed:
        return (2,
                f"Blocked: model '{model}' is not in MODEL_GUARD_ALLOWED ({', '.join(allowed)}). "
                "Re-issue this call with one of those.",
                f"BLOCKED not allowed model={model}: {desc}")
    try:  # optional report-path rule (needs report-gate beside this folder); any error fails open
        rtype = str(tin.get("subagent_type") or "").strip().lower()
        rprompt = tin.get("prompt")
        if (report_check_on() and rtype in REPORT_TYPES and isinstance(rprompt, str)
                and not _HANDOFF_RE.search(_FENCE_RE.sub("", rprompt))):
            problem = report_path_problem(rprompt)
            if problem:
                return (2,
                        "Blocked (report path): " + rtype + " brief: " + problem + ". Name where the report goes, e.g. "
                        "'Report: $HUBKIT_HOME/reports/<name>.md' (an absolute .md path with a reports folder, no '..'; "
                        "'Report: none' if it writes none). Without it report-gate blocks the worker at stop.",
                        f"BLOCKED report path {rtype} {problem}: {desc}")
    except Exception as e:
        log(f"REPORT fail-open {type(e).__name__}")
    try:  # optional gate-round cap + many-deliverables warning; any error fails open
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import gate_round_cap
        cap = gate_round_cap.check_round_cap(tin, str(tin.get("prompt") or ""))
        if cap and cap[0] == "block":
            return 2, "Blocked (gate round cap): " + cap[1], "BLOCKED gate round cap: " + desc
        if cap:
            ADVICE.append(cap[1])
        dw = gate_round_cap.check_deliverables(tin, str(tin.get("prompt") or ""))
        if dw:
            ADVICE.append(dw)
    except Exception:
        pass
    try:  # optional gate admission by the builder's own record; any error fails open
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import selfcheck_admit
        adm = selfcheck_admit.check(tin, str(tin.get("prompt") or ""))
        if adm and adm[0] == "block":
            return 2, "Blocked (gate admission): " + adm[1], "BLOCKED gate admission: " + desc
        if adm:
            log(f"SELFCHECK-{adm[0].upper()} {desc} {adm[1]}")
            if adm[0] == "warn" and adm[1].startswith("WARN-ONLY"):
                ADVICE.append(adm[1])
    except Exception:
        pass
    lock_msg = ticket_lock_message(tin, data.get("session_id"), data.get("tool_use_id"))
    if lock_msg:
        # Warn once per (session, brief), then go: the same brief sent again within 10 minutes is a deliberate
        # retry and takes the lock as a handoff. A same-payload refusal that keeps coming back (a retry storm)
        # is stopped after 3 with one plain STOP line. Each part fails open to the plain refusal.
        prompt = str(tin.get("prompt") or "")
        first = True
        tl = _lock_module()
        try:
            first = tl.warn_once("owner", str(data.get("session_id") or ""), prompt) if tl else True
        except Exception as e:
            log(f"OWNER-ONCE fail-open {type(e).__name__}")
        if first:
            n = owner_refusals(tin, prompt)
            if n > 3:
                return (2,
                        "STOP (one owner per ticket): this exact spawn was refused 3 times. Do not re-send it. "
                        "Wait for the owner to finish, or change the brief (add 'HANDOFF-FROM: <owner>').",
                        f"OWNER-STOP same payload refused 3 times: {desc}" if n == 4 else "")
            return (2,
                    "Blocked ONCE (one owner per ticket): " + lock_msg +
                    "\nSend the same brief again and it goes through; or add a line 'HANDOFF-FROM: <owner>' "
                    "after writing the handoff.",
                    f"BLOCKED ticket lock: {lock_msg[:200]}")
        log(f"OWNER-RETRY-ALLOW {desc}")
        try:
            tl.acquire(tin, str(data.get("session_id") or ""), str(data.get("tool_use_id") or ""), handoff=True)
        except Exception as e:
            log(f"OWNER lock fail-open {type(e).__name__}")
    record_gate_spawn(tin, model, data.get("session_id"))
    record_model_spawn(tin, model)
    return 0, None, f"OK model={model}: {desc}"


def _leave(code):
    """A block (exit 2) flushes stderr inside a try, then leaves without interpreter shutdown, so a broken stderr
    cannot turn the block into exit 120 (which Claude Code does not treat as a block)."""
    if code == 2:
        try:
            sys.stderr.flush()
        except BaseException:
            pass
        os._exit(2)
    sys.exit(code)


def _fail_closed(why):
    """The call cannot be read or classified, so it is refused rather than allowed."""
    log(f"FAIL-CLOSED {why}")
    try:
        print("Blocked (model guard): the call could not be read or checked safely (%s), so it is refused "
              "rather than allowed. Re-send it as a normal Agent call with an explicit model." % why, file=sys.stderr)
    except BaseException:
        pass
    _leave(2)


def main():
    try:
        data = json.loads(getattr(sys.stdin, "buffer", sys.stdin).read())
    except BaseException as e:
        _fail_closed("unreadable payload: %s" % type(e).__name__)
    if not isinstance(data, dict):
        _fail_closed("payload is not a JSON object")
    tool = data.get("tool_name")
    if tool is None:
        tool = ""
    if not isinstance(tool, str):
        _fail_closed("tool_name is not a string")
    spawn = tool in SPAWN_TOOLS
    tin = data.get("tool_input")
    if tin is not None and not isinstance(tin, dict):
        if spawn:
            _fail_closed("tool_input is not an object")
        data = dict(data, tool_input={})
    try:
        code, msg, line = decide(data)
    except Exception as e:
        if not spawn:
            log(f"FAIL-OPEN {type(e).__name__}")
            return 0
        _fail_closed("guard error %s on a spawn it has rules for" % type(e).__name__)
    if line:
        log(line)
    if msg:
        try:
            print(msg, file=sys.stderr)
        except BaseException:
            pass
    if ADVICE and code == 0:
        try:
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                  "additionalContext": "model-guard advisory: " + " | ".join(ADVICE)}}))
        except Exception:
            pass
    return code


if __name__ == "__main__":
    _leave(main())
