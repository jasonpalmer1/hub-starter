"""Ticket lock: one owner per ticket.

Used by model-guard.py (PreToolUse on Agent/Task: acquire) and report-gate.py
(SubagentStop: release + closed-ticket alarm). Every entry point is fail-open:
an error here must never block a spawn or trap a worker.

Lock file: <LOCK_DIR>/<ticket id>.json = {owner, started, session, primary, tickets, key, phash, plen,
handoffs}. Live = file exists AND age < TTL (60 min). No pid liveness: the hook's parent is the
long-lived lead chat, never the worker. A lock is freed by SubagentStop, by the TTL, by
`ticket-lock.py release <id>`, or by a "re-dispatch"/"fix round" spawn from the same session
once the lock is older than 10 min.
A claim is provisional until the worker is seen starting (a stored agent_id, or a subagent transcript of the
claiming session whose opening prompt matches the claim). An unstarted claim blocks other sessions for 120 s only,
and the same session's retry of the same brief refreshes it after 15 s (a spawn another hook blocked leaves no worker).
A claim stamped more than 5 minutes in the future is treated as corrupt, not live. Test seams: TICKET_LOCK_PROJECTS_GLOB, TICKET_LOCK_SESSION_GLOB.
A claim whose spawn the claiming session's own transcript shows was blocked by a PreToolUse hook (an is_error tool_result
for the claim's tool_use_id starting "PreToolUse:<hook> hook error") is never an owner, so a retry or handoff is not a duplicate.
Only ids in the Agent `description` lock. Only builder / general-purpose / claude take locks.
Overrides: DUP-OK anywhere in the prompt (no lock, no check); a line starting HANDOFF-FROM: <owner>.

Env: HUBKIT_HOME (install root, default ~/.claude/hubkit), TICKET_LOCK_DIR, TICKET_LOCK_ALARM_LOG,
TICKET_LOCK_TTL_S (default 3600), TICKET_LOCK_ID_RE (ticket id pattern, default L- plus four digits).
"""
import datetime
import fcntl
import hashlib
import json
import os
import re
import shlex
import time

DEFAULT_ID_RE = r"L-\d{4}"


def _id_re():
    try:
        return re.compile(os.environ.get("TICKET_LOCK_ID_RE") or DEFAULT_ID_RE)
    except re.error:
        return re.compile(DEFAULT_ID_RE)


TICKET_RE = re.compile(r"\b" + _id_re().pattern + r"\b")
HANDOFF_RE = re.compile(r"^[ \t]*HANDOFF-FROM:[ \t]*(\S[^\n]*)", re.M)
LOCK_TYPES = frozenset(("builder", "general-purpose", "claude"))
REDISPATCH_RE = re.compile(r"re-dispatch|fix round", re.I)
REDISPATCH_AGE_S = 600.0
PROVISIONAL_S = 120.0   # a claim with no sign the worker started is only held this long
MIN_REFRESH_S = 15.0    # a same-session retry needs a model round trip; sooner is a duplicate
FUTURE_SKEW_S = 300.0   # a claim stamped further ahead than this is corrupt/skewed: not live


def hubkit_home():
    return os.path.expanduser(os.environ.get("HUBKIT_HOME") or "~/.claude/hubkit")


def lock_dir():
    o = os.environ.get("TICKET_LOCK_DIR")
    if o:
        return o
    ml = os.environ.get("MODEL_GUARD_LOG")  # any test that isolates the guard log is isolated here too
    if ml:
        return os.path.join(os.path.dirname(os.path.abspath(ml)), "ticket-locks")
    return os.path.join(hubkit_home(), "state", "ticket-locks")


def alarm_log():
    o = os.environ.get("TICKET_LOCK_ALARM_LOG")
    if o:
        return o
    ml = os.environ.get("MODEL_GUARD_LOG")
    if ml:
        return os.path.join(os.path.dirname(os.path.abspath(ml)), "ticket-alarms.log")
    return os.path.join(hubkit_home(), "state", "ticket-alarms.log")


def ttl_s():
    try:
        return float(os.environ.get("TICKET_LOCK_TTL_S") or 3600)
    except ValueError:
        return 3600.0


def now():
    o = os.environ.get("TICKET_LOCK_NOW")
    return float(o) if o else time.time()


def tickets_of(desc):
    """Only ids in the Agent description lock; ids quoted in the prompt body never do."""
    seen = []
    for i in TICKET_RE.findall(desc or ""):
        if i not in seen:
            seen.append(i)
    return seen


PROMPT_TICKET_RE = re.compile(r"\bTicket:?[ \t]+(L-\d{4})\b")
_FREE_OWNERS = ("", "unowned", "hub", "none")


def claim_tickets(desc, prompt):
    """ids that lock. The description ids when it has any (unchanged); else the first `Ticket L-NNNN` the
    brief names (a worker whose description carries no id still owns the ticket its brief names)."""
    t = tickets_of(desc)
    if t:
        return t
    m = PROMPT_TICKET_RE.search(prompt or "")
    return [m.group(1)] if m else []


def ledger_claim(tickets, label="worker"):
    """set the ledger owner to `label` for tickets that have no owner (none/unowned/hub/empty). A ticket owned by
    anyone else (live chat, dead chat, worker) is left alone, and manual `ledger claim` is untouched. Fail-open; never
    raises. Returns the ids claimed."""
    done = []
    try:
        import subprocess
        import sys
        lib = os.path.join(hubkit_home(), "ledger")
        if lib not in sys.path:
            sys.path.insert(0, lib)
        import ledger_lib  # type: ignore
        recs = ledger_lib.fold(ledger_lib.read_events())
        if not recs:
            return done
        bin_ = os.environ.get("TICKET_LOCK_LEDGER_BIN") or os.path.join(lib, "ledger")
        for t in tickets:
            r = recs.get(t)
            if not r or r.get("status") not in ("open", "active"):
                continue
            if str(r.get("owner") or "").strip().lower() not in _FREE_OWNERS:
                continue
            rc = subprocess.run([bin_, "claim", t, label], capture_output=True, timeout=5).returncode
            if rc == 0:
                done.append(t)
    except Exception:
        pass
    return done


def norm(prompt):
    return re.sub(r"\s+", " ", prompt or "").strip()


def phash(prompt):
    """(sha256 of the FULL normalized prompt, its length). Release matches a stop transcript whose
    prompt merely starts with this prompt (worker-lessons appends), so no 160-char prefix collision."""
    n = norm(prompt)
    return hashlib.sha256(n.encode("utf-8", "replace")).hexdigest(), len(n)


def _path(t):
    return os.path.join(lock_dir(), f"{t}.json")


def _read(t):
    try:
        with open(_path(t)) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def is_live(lk):
    if not lk:
        return False
    try:
        age = now() - float(lk.get("started") or 0)
        return -FUTURE_SKEW_S <= age < ttl_s()
    except (TypeError, ValueError):
        return False


def _projects_glob():
    o = os.environ.get("TICKET_LOCK_PROJECTS_GLOB")  # test seam; {sid} is the session id
    return o or "~/.claude/projects/*/{sid}/subagents/agent-*.jsonl"


def _first_prompt(path):
    try:
        with open(path) as f:
            rec = json.loads(f.readline())
        c = (rec.get("message") or {}).get("content")
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            return "\n".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in c)
    except Exception:
        pass
    return ""


def has_started(lk):
    """evidence the claimed worker really began: a stored agent_id, or a subagent transcript
    of the claiming session, written after the claim, whose opening prompt matches the claim's hash.
    A spawn another PreToolUse hook blocked leaves no transcript, so its claim stays provisional."""
    try:
        if lk.get("agent_id"):
            return True
        sid, ph, pl = lk.get("session") or "", lk.get("phash"), int(lk.get("plen") or 0)
        if not (sid and ph and pl):
            return False
        import glob
        for fp in glob.glob(os.path.expanduser(_projects_glob().replace("{sid}", glob.escape(sid)))):
            try:
                if os.path.getmtime(fp) < float(lk.get("since") or lk.get("started") or 0):
                    continue
            except OSError:
                continue
            hay = norm(_first_prompt(fp))
            if len(hay) >= pl and hashlib.sha256(hay[:pl].encode("utf-8", "replace")).hexdigest() == ph:
                return True
    except Exception:
        pass
    return False


def _session_glob():
    o = os.environ.get("TICKET_LOCK_SESSION_GLOB")  # test seam; {sid} is the session id
    return o or "~/.claude/projects/*/{sid}.jsonl"


def _sid_ok(sid):
    """A session id is a bare name: no path separator, no "..", no NUL. Anything else is hostile."""
    return bool(sid) and ".." not in sid and not any(c in sid for c in "/\\\0")


def was_blocked(lk):
    """POSITIVE evidence the claiming spawn never launched: the claiming session's transcript
    holds a tool_result for this claim's tool_use_id with is_error true whose text starts with a
    PreToolUse hook block. Anything else (no key, no transcript, no matching record, garbage lines,
    a started worker) is False, so a real earlier dispatch keeps its claim and still alarms."""
    try:
        if has_started(lk):
            return False
        key, sid = str(lk.get("key") or ""), str(lk.get("session") or "")
        if not (key.startswith("toolu_") and sid):
            return False
        if not _sid_ok(sid):
            return False  # a path-shaped session id never reads outside the projects dir; the claim stays held
        import glob
        for fp in glob.glob(os.path.expanduser(_session_glob().replace("{sid}", glob.escape(sid)))):
            with open(fp, errors="replace") as f:
                for line in f:
                    if key not in line or "tool_result" not in line:
                        continue
                    try:
                        c = (json.loads(line).get("message") or {}).get("content")
                    except Exception:
                        continue
                    for b in (c if isinstance(c, list) else []):
                        if (isinstance(b, dict) and b.get("type") == "tool_result"
                                and b.get("tool_use_id") == key and b.get("is_error") is True):
                            t = b.get("content")
                            if isinstance(t, list):
                                t = " ".join(x.get("text", "") for x in t if isinstance(x, dict))
                            if isinstance(t, str) and (re.match(r"\s*PreToolUse:\S+ hook error", t)
                                                       or "Concurrent subagent limit" in t[:300]):
                                return True   # the harness refused the launch at its subagent cap
    except Exception:
        pass
    return False


def provisional_state(lk, n, session_id, ph=None):
    """None if the claim stands (held); 'refresh' if it is the same session's unstarted claim, retried
    with the SAME brief after MIN_REFRESH_S (a retry of a blocked call); 'expired' if unstarted and
    older than PROVISIONAL_S. Parallel same-session spawns and different-brief spawns stay blocked."""
    if has_started(lk):
        return None
    try:
        age = n - float(lk.get("started") or 0)
    except (TypeError, ValueError):
        return "expired"
    if age > PROVISIONAL_S:
        return "expired"
    if session_id and lk.get("session") == session_id and ph and lk.get("phash") == ph and age >= MIN_REFRESH_S:
        return "refresh"
    return None


def _write(t, lk):
    p = _path(t)
    tmp = p + f".tmp{os.getpid()}"
    with open(tmp, "w") as f:
        json.dump(lk, f)
    os.replace(tmp, p)


def _log_alarm(line):
    try:
        with open(alarm_log(), "a") as f:
            f.write(f"{datetime.datetime.now().isoformat(timespec='seconds')} {line}\n")
    except Exception:
        pass


def acquire(tin, session_id="", tool_use_id="", handoff=False):
    """Returns (blocked_message_or_None). Fail-open on any error.
    handoff=True: take over an existing owner without editing the prompt, so
    phash/plen stay those of the brief the worker really gets (has_started and SubagentStop release match)."""
    try:
        stype = str(tin.get("subagent_type") or "general-purpose").strip().lower()
        if stype not in LOCK_TYPES:
            return None
        desc = str(tin.get("description") or "")
        prompt = str(tin.get("prompt") or "")
        if "DUP-OK" in prompt:
            return None
        tickets = claim_tickets(desc, prompt)
        if not tickets:
            return None
        hm = HANDOFF_RE.search(prompt)
        handoff_from = hm.group(1).strip()[:120] if hm else None
        if handoff and not handoff_from:
            handoff_from = "owner-warned-once"
        redispatch = bool(REDISPATCH_RE.search(desc)) and bool(session_id)
        os.makedirs(lock_dir(), exist_ok=True)
        owner = (desc[:80] or stype or "agent")
        fd = os.open(os.path.join(lock_dir(), ".guard"), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            held = {}
            refreshed = {}
            ph, pl = phash(prompt)
            n = now()
            for t in tickets:
                lk = _read(t)
                if not is_live(lk):
                    continue
                if redispatch and lk.get("session") == session_id and n - float(lk.get("started") or 0) > REDISPATCH_AGE_S:
                    continue  # re-dispatch of a dead/stopped worker from the same session
                if was_blocked(lk):
                    continue  # hook-blocked before launch (transcript proof): never an owner
                ps = provisional_state(lk, n, session_id, ph)
                if ps:
                    if ps == "refresh":
                        refreshed[t] = lk.get("since") or lk.get("started")
                    continue  # unstarted claim (blocked by another hook) - retry or lapse
                held[t] = lk
            if held and not handoff_from:
                t, lk = next(iter(held.items()))
                return f"{t} already owned by {lk.get('owner')}; write a handoff or wait"
            lk_new = {
                "owner": owner, "started": n,
                "started_iso": datetime.datetime.fromtimestamp(n).isoformat(timespec="seconds"),
                "session": session_id, "primary": tickets[0], "tickets": tickets,
                "key": tool_use_id or ph, "phash": ph, "plen": pl, "handoffs": [],
            }
            for t in tickets:
                lk = dict(lk_new)
                if t in refreshed:
                    lk["since"] = refreshed[t]  # earlier spawn's transcript still counts as started
                if t in held:
                    prev = held[t]
                    lk["handoffs"] = list(prev.get("handoffs") or []) + [{
                        "at": lk_new["started_iso"], "claimed_from": handoff_from,
                        "previous_owner": prev.get("owner"), "new_owner": owner}]
                    _log_alarm(f"TICKET-HANDOFF {t} from={prev.get('owner')!r} claimed={handoff_from!r} to={owner!r}")
                _write(t, lk)
        finally:
            os.close(fd)
        ledger_claim(tickets)
        return None
    except Exception:
        return None


WARN_ONCE_S = 600.0   # a refused brief that comes back within 10 min goes through


def warn_once(kind, session_id, prompt):
    """True if this (kind, session, brief) has not been refused in the last WARN_ONCE_S (record it now);
    False if it was, so the caller lets it through. Fail-open (False) on any error."""
    try:
        h = hashlib.sha256(f"{kind}|{session_id}|{phash(prompt)[0]}".encode()).hexdigest()[:32]
        os.makedirs(lock_dir(), exist_ok=True)
        p = os.path.join(lock_dir(), ".warned-" + h)
        n = now()
        gfd = os.open(os.path.join(lock_dir(), ".guard"), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(gfd, fcntl.LOCK_EX)
            try:  # stale marks (older than 1 h) are removed; any error here is ignored
                for fn in os.listdir(lock_dir()):
                    if fn.startswith(".warned-"):
                        fp = os.path.join(lock_dir(), fn)
                        if n - os.path.getmtime(fp) > 3600:
                            os.unlink(fp)
            except OSError:
                pass
            try:
                with open(p) as f:
                    if 0 <= n - float(f.read().strip()) < WARN_ONCE_S:
                        return False
            except (OSError, ValueError):
                pass
            with open(p, "w") as f:
                f.write(str(n))
            return True
        finally:
            os.close(gfd)
    except Exception:
        return False


def release(ticket):
    """Manual free (hub, after TaskStop or a crash). Returns True if a lock file was removed."""
    try:
        os.unlink(_path(ticket))
        return True
    except OSError:
        return False


def _is_close(cmd):
    """Ticket ids closed by an executed `ledger status L-N done|closed` command (any path prefix,
    env assignments allowed). Text inside quotes (grep/echo arguments) never matches."""
    out = []
    try:
        lex = shlex.shlex(cmd, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        toks = list(lex)
    except Exception:
        return out
    segs, cur = [], []
    for t in toks:
        if t and set(t) <= set(";&|()"):
            segs.append(cur)
            cur = []
        else:
            cur.append(t)
    segs.append(cur)
    for seg in segs:
        while seg and re.fullmatch(r"[A-Za-z_]\w*=.*", seg[0]):
            seg = seg[1:]
        if (len(seg) >= 4 and os.path.basename(seg[0]) == "ledger" and seg[1] == "status"
                and TICKET_RE.fullmatch(seg[2]) and seg[3] in ("done", "closed")):
            out.append(seg[2])
    return out


def _closed_ids(transcript_path):
    out = set()
    try:
        with open(transcript_path) as f:
            for line in f:
                if "ledger" not in line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                c = (rec.get("message") or {}).get("content")
                if not isinstance(c, list):
                    continue
                for it in c:
                    if isinstance(it, dict) and it.get("type") == "tool_use":
                        cmd = (it.get("input") or {}).get("command")
                        if isinstance(cmd, str):
                            out.update(_is_close(cmd))
    except Exception:
        pass
    return out


def release_and_audit(prompt_text, transcript_path=None, agent_id=""):
    """SubagentStop: free the locks this worker owned and alarm if it closed a ticket
    other than the ones it was briefed on. Fail-open."""
    try:
        d = lock_dir()
        if not prompt_text or not os.path.isdir(d):
            return
        hay = norm(prompt_text)
        mine = []
        for fn in os.listdir(d):
            if not fn.endswith(".json"):
                continue
            lk = _read(fn[:-5])
            if not lk or not lk.get("phash") or not lk.get("plen"):
                continue
            n = int(lk["plen"])
            if len(hay) >= n and hashlib.sha256(hay[:n].encode("utf-8", "replace")).hexdigest() == lk["phash"]:
                mine.append((fn[:-5], lk))
        if not mine:
            return
        briefed = set()
        for t, lk in mine:
            briefed.update(lk.get("tickets") or [t])
        if transcript_path:
            closed = _closed_ids(transcript_path)
            stray = sorted(closed - briefed)
            if stray:
                primary = mine[0][1].get("primary")
                _log_alarm(f"TICKET-MISMATCH briefed={primary} closed={','.join(stray)} agent={agent_id}")
        for t, lk in mine:
            try:
                os.unlink(_path(t))
            except FileNotFoundError:
                pass
    except Exception:
        pass
