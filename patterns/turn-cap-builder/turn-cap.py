#!/usr/bin/env python3
"""Worker turn cap: stops a subagent from running forever.

Wired through the frontmatter hooks of every agent role file (PreToolUse and PostToolUse), so no settings.json
edit is needed. Worker types with no role file are covered once the same two hooks are registered in settings.json;
a call seen through both wirings is counted once (first_sight). See agents/builder.md.

Caps per worker type (tool calls; a type not listed gets the default 115): TURN_CAP_CAPS, default
"bug-gate=120,researcher=100,builder=115,judge=60,playtester=60". The nudge comes 20 calls before the cap
(builder: TURN_CAP_NUDGE, default 90).

Per worker (agent_id), counting tool calls (the count runs slightly early, which is fine):
  PreToolUse : count the call. At the worker's cap and beyond, refuse every tool call (exit 2) except the ways
               a worker hands off: Write/Edit of a file under the reports folder, `test -s <path under reports>`,
               and SubagentHandback. Prints nothing on the normal path (no tokens spent per turn).
  Poll credit: a call whose whole shape is a pure bounded poll (a single `until ...; do sleep N; done`
               loop with a Bash timeout of 270000 ms or less, run in the foreground) or a Monitor call earns
               one credit, at most 40 per worker (<agent_id>.credits). Credits only defer the nudge
               (nudge when count - credits >= TURN_CAP_NUDGE, or count >= TURN_CAP_BLOCK - 10). They never
               lift the block: every call still counts toward it.
  PostToolUse: at TURN_CAP_NUDGE (default 90) net of credits, once per worker, tell it to write its
               handoff now.
Exempt: agent types listed in TURN_CAP_EXEMPT (default none: no worker type is exempt),
and any event with no agent_id (the main chat). Fails OPEN (exit 0) on any internal error:
a bug here must never stop real work.

State: $TURN_CAP_STATE_DIR, default $HUBKIT_HOME/state/turns (HUBKIT_HOME defaults to
~/.claude/hubkit). <agent_id> is an append-only file with one byte per call; the count is
the file size, so parallel tool calls cannot lose an increment (no read-modify-write, no
lock). <agent_id>.nudged is created exclusively so the nudge fires once. Files older than
24 hours are pruned, at most once an hour.

Reports folder allowed past the cap: $TURN_CAP_REPORTS_DIR, default $HUBKIT_HOME/reports.

Unverified: whether Claude Code passes a PostToolUse additionalContext note to a subagent.
The block at the cap is the guaranteed path and does not depend on the nudge.
"""
import json
import os
import re
import sys
import time


def _int_env(name, default):
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        return default


CAP_NUDGE = _int_env("TURN_CAP_NUDGE", 90)     # the builder's nudge point
CAP_BLOCK = _int_env("TURN_CAP_BLOCK", 115)    # the default cap
CAP_SAVE = _int_env("TURN_CAP_SAVE", 60)
DEFAULT_CAPS = "bug-gate=120,researcher=100,builder=115,judge=60,playtester=60"
WARN_BEFORE = 20
EXEMPT = {t.strip() for t in (os.environ.get("TURN_CAP_EXEMPT") or "").split(",") if t.strip()}
HANDBACK_TOOLS = ("SubagentHandback",)
_TEST_S = re.compile(r"test -s ((?:~|/)[A-Za-z0-9_./~-]+)")
BLOCK_MSG = ("turn cap reached: write handoff.md (done / next / files / commands / open questions) "
             "and your report, then stop")


def _caps():
    out = {}
    for part in (os.environ.get("TURN_CAP_CAPS") or DEFAULT_CAPS).split(","):
        k, _, v = part.partition("=")
        try:
            out[k.strip().lower()] = int(v)
        except ValueError:
            pass
    return out


def cap_for(agent_type):
    if os.environ.get("TURN_CAP_BLOCK"):   # an explicit single cap wins over the per-type table
        return CAP_BLOCK
    return _caps().get(str(agent_type or "").strip().lower(), CAP_BLOCK)


def nudge_for(agent_type):
    t = str(agent_type or "").strip().lower()
    if t == "builder":
        return CAP_NUDGE
    return cap_for(t) - WARN_BEFORE


def hubkit_home():
    return os.path.expanduser(os.environ.get("HUBKIT_HOME") or "~/.claude/hubkit")


def state_dir():
    return os.environ.get("TURN_CAP_STATE_DIR") or os.path.join(hubkit_home(), "state", "turns")


def reports_dir():
    return os.environ.get("TURN_CAP_REPORTS_DIR") or os.path.join(hubkit_home(), "reports")


def prune(d):
    marker = os.path.join(d, ".pruned")
    now = time.time()
    try:
        if now - os.stat(marker).st_mtime < 3600:
            return
    except OSError:
        pass
    open(marker, "a").close()
    os.utime(marker, None)
    for n in os.listdir(d):
        p = os.path.join(d, n)
        try:
            if n != ".pruned" and now - os.stat(p).st_mtime > 86400:
                os.unlink(p)
        except OSError:
            pass


def _under_reports(fp):
    fp = os.path.realpath(os.path.expanduser(fp)) if fp else ""
    return bool(fp) and fp.startswith(os.path.realpath(reports_dir()) + os.sep)


def allowed_write(tool, ti):
    """The calls an over-cap worker may still make: write its handoff/report, check the report exists, hand back."""
    if tool in HANDBACK_TOOLS:
        return True
    if not isinstance(ti, dict):
        return False
    if tool in ("Write", "Edit"):
        return _under_reports(ti.get("file_path") or "")
    if tool == "Bash":
        m = _TEST_S.fullmatch(str(ti.get("command") or "").strip())
        return bool(m) and _under_reports(m.group(1))
    return False


def first_sight(path, ev):
    """True when this hook run is the first to see this tool call. The same call can arrive twice (role-file wiring
    and settings wiring); the second sighting must not count again. No tool_use_id = always count."""
    tid = "".join(c for c in str(ev.get("tool_use_id") or "") if c.isalnum() or c in "-_")[:80]
    if not tid:
        return True
    try:
        os.close(os.open("%s.t.%s" % (path, tid), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644))
        return True
    except FileExistsError:
        return False
    except OSError:
        return True


CREDIT_CEILING = 40  # most polls per worker that may defer the nudge
MAX_POLL_TIMEOUT_MS = 270000
_POLL_RE = re.compile(r"^\s*(until|while)\s[^\n;&|`$()<>]*;\s*do\s+sleep\s+\d+(\.\d+)?\s*;\s*done\s*$")


def is_poll(ev):
    """True for Monitor, or a foreground Bash call that is only a bounded poll loop. Anything odd means no credit."""
    try:
        tool, ti = ev.get("tool_name"), ev.get("tool_input")
        if tool == "Monitor":
            return True
        if tool != "Bash" or not isinstance(ti, dict) or ti.get("run_in_background"):
            return False
        t = ti.get("timeout")
        if isinstance(t, bool) or not isinstance(t, (int, float)) or not 0 < t <= MAX_POLL_TIMEOUT_MS:
            return False
        cmd = ti.get("command")
        return isinstance(cmd, str) and bool(_POLL_RE.match(cmd))
    except Exception:
        return False


def credits(path):
    try:
        return min(os.stat(path + ".credits").st_size, CREDIT_CEILING)
    except OSError:
        return 0


def grant_credit(ev, path):
    """Append one credit byte for a pure poll, up to the ceiling. Never touches the main counter."""
    try:
        if credits(path) < CREDIT_CEILING and is_poll(ev):
            fd = os.open(path + ".credits", os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
            try:
                os.write(fd, b".")
            finally:
                os.close(fd)
    except Exception:
        pass


def main():
    ev = json.load(sys.stdin)
    aid = ev.get("agent_id")
    if not aid or ev.get("agent_type") in EXEMPT:
        return 0
    aid = "".join(c for c in str(aid) if c.isalnum() or c in "-_")[:80]
    if not aid:
        return 0
    atype = ev.get("agent_type")
    cap, nudge = cap_for(atype), nudge_for(atype)
    d = state_dir()
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, aid)
    phase = ev.get("hook_event_name")
    if phase == "PreToolUse":
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            if first_sight(path, ev):
                os.write(fd, b".")
                fresh = True
            else:
                fresh = False
            n = os.fstat(fd).st_size
        finally:
            os.close(fd)
        if fresh:
            grant_credit(ev, path)  # a pure poll may defer the NUDGE only; every call still counts
        try:
            prune(d)
        except Exception:
            pass
        if n >= cap and not allowed_write(ev.get("tool_name"), ev.get("tool_input")):
            sys.stderr.write(BLOCK_MSG + "\n")
            return 2
    elif phase == "PostToolUse":
        n = os.stat(path).st_size
        # Message only, once per worker, never a block: a save-progress check at CAP_SAVE net steps.
        if cap >= 100 and CAP_SAVE <= n - credits(path) < nudge:
            try:
                os.close(os.open(path + ".save60", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644))
                print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse",
                      "additionalContext": "STEP-%d CHECK: ~%d tool calls used. Commit and push what works now and "
                      "note progress; if more than a third of the scope remains, plan to write handoff.md "
                      "(done / next / files / commands / open questions) rather than push on. "
                      "Message only, nothing is blocked." % (CAP_SAVE, n)}}))
            except FileExistsError:
                pass
        if n - credits(path) >= nudge or n >= max(nudge, cap - 10):
            try:
                os.close(os.open(path + ".nudged", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644))
            except FileExistsError:
                return 0
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse",
                  "additionalContext": "TURN CAP NUDGE: ~%d of %d tool calls used; write handoff.md now "
                  "(done / next / files / commands / open questions), then finish." % (n, cap)}}))
    return 0


if __name__ == "__main__":
    try:
        rc = main()
    except Exception:
        rc = 0  # fail open
    sys.exit(rc)
