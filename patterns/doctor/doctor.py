#!/usr/bin/env python3
"""doctor: "is my hub set up and healthy?" in one read-only run.

  python3 doctor.py                 print PASS / WARN / FAIL / SKIP for each check, with the fix
  python3 doctor.py --paste         same, with your home folder and user name scrubbed so the
                                    output is safe to paste into a chat or an email
  python3 doctor.py --settings FILE look at this settings file instead of ~/.claude/settings.json

Exit code 0 means no FAIL. 1 means at least one FAIL. WARN and SKIP never fail the run.

It reads files and creates nothing: the only write is one line appended to
$HUBKIT_HOME/state/events.log when that folder already exists (a local note of what ran;
--no-log turns even that off). It never creates the install folder, so a second run on a
machine with nothing installed still says FAIL home. Checks:
  python      Python is new enough (3.9+)
  home        the install folder exists and is writable
  hooks       each installed hook script is wired into settings, under the right event, and the
              file the setting points at exists; hooks are not switched off
  agent       the capped builder agent is where Claude Code will trust it
  ledger      the ledger file parses, every line
  heartbeats  every scheduled job's last beat is fresh (needs deadman beats with --max-age)
  alarms      recent alarms from the deadman check
  disk        free space (DISK_GUARD_WARN_GI / DISK_GUARD_CRIT_GI, default 20 / 5 GiB)
  loop        only when work-loop is installed (or a Stop entry names stop_arm.py): the tick ran in the last 30
              minutes, the last fed ticket, a wired Stop arm points at a file that exists, and the failure
              register ran in the last 2 hours
DOCTOR_NOW (epoch seconds) replaces the clock; it exists for tests.
"""
import calendar
import datetime
import getpass
import json
import os
import re
import shutil
import sys
import time

# script (relative to the install folder) -> hook event it must be wired under
HOOKS = [
    ("model-guard/model-guard.py", "PreToolUse"),
    ("dup-worker-check/dup-worker-hook.py", "PreToolUse"),
    ("report-gate/report-gate.py", "SubagentStop"),
    ("ledger/ledger-banner-hook.py", "UserPromptSubmit"),
]
MATCHERS = {"model-guard.py": "Agent|Task|Workflow", "dup-worker-hook.py": "Agent|Task"}


def home():
    return os.path.expanduser(os.environ.get("HUBKIT_HOME") or "~/.claude/hubkit")


def now():
    raw = os.environ.get("DOCTOR_NOW")
    return int(raw) if raw and raw.isdigit() else int(time.time())


class Report:
    def __init__(self):
        self.rows = []

    def add(self, status, name, detail, fix=None):
        self.rows.append((status, name, detail, fix))

    def counts(self):
        return {s: sum(1 for r in self.rows if r[0] == s) for s in ("PASS", "WARN", "FAIL", "SKIP")}


def snippet(script, event):
    path = "~/.claude/hubkit/" + script
    m = MATCHERS.get(os.path.basename(script))
    block = {"hooks": [{"type": "command", "command": "python3 " + path}]}
    if m:
        block["matcher"] = m
    return json.dumps({"hooks": {event: [block]}})


def check_python(r):
    v = sys.version_info
    if v >= (3, 9):
        r.add("PASS", "python", "Python %d.%d.%d" % v[:3])
    else:
        r.add("FAIL", "python", "Python %d.%d is too old (need 3.9 or newer)" % v[:2],
              "install a newer Python 3 and run the hub's scripts with it")


def check_home(r):
    h = home()
    if not os.path.isdir(h):
        r.add("FAIL", "home", "install folder %s does not exist" % h,
              "create it and copy the patterns you want into it: mkdir -p " + h)
    elif not os.access(h, os.W_OK):
        r.add("FAIL", "home", "install folder %s is not writable" % h, "chmod u+w " + h)
    else:
        r.add("PASS", "home", "install folder %s" % h)


def load_settings(path):
    try:
        with open(path) as f:
            data = json.load(f)
    except FileNotFoundError:
        return None, "settings file %s not found" % path
    except (OSError, ValueError) as e:
        return None, "settings file %s is not valid JSON (%s)" % (path, type(e).__name__)
    if not isinstance(data, dict):
        return None, "settings file %s is not a JSON object" % path
    return data, None


def commands_by_event(settings):
    out = {}
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return out
    for event, groups in hooks.items():
        for g in groups if isinstance(groups, list) else []:
            for h in (g.get("hooks") if isinstance(g, dict) else None) or []:
                if isinstance(h, dict) and isinstance(h.get("command"), str):
                    out.setdefault(event, []).append(h["command"])
    return out


def expand(tok):
    h = home()
    tok = tok.replace("${HUBKIT_HOME}", h).replace("$HUBKIT_HOME", h)
    tok = tok.replace("${HOME}", os.path.expanduser("~")).replace("$HOME", os.path.expanduser("~"))
    return os.path.expanduser(tok)


def check_hooks(r, settings_path):
    installed = [(s, e) for s, e in HOOKS if os.path.isfile(os.path.join(home(), s))]
    if not installed:
        r.add("SKIP", "hooks", "no hook scripts are installed under the install folder yet")
        return
    settings, err = load_settings(settings_path)
    if settings is None:
        if "not found" in err:
            fix = ("create it, or point doctor at the right file with --settings FILE; "
                   "then add each hook block this check prints")
        elif "not valid JSON" in err:
            fix = ("the file exists but is not valid JSON: open it and fix the typo (a missing or extra comma or "
                   "bracket is the usual cause; python3 -m json.tool FILE points at the line), or restore your backup; "
                   "do not replace it, you would lose your other settings. Then rerun doctor")
        else:
            fix = "the file must hold one JSON object ({ ... }); fix it, or point doctor at the right file with --settings FILE"
        r.add("FAIL", "hooks", err, fix)
        return
    if settings.get("disableAllHooks") is True:
        r.add("FAIL", "hooks-enabled", "settings has disableAllHooks set to true, so no hook runs",
              'remove "disableAllHooks" (or set it to false) in ' + settings_path)
    else:
        r.add("PASS", "hooks-enabled", "hooks are not switched off")
    cmds = commands_by_event(settings)
    for script, event in installed:
        base = os.path.basename(script)
        label = "hook %s" % base
        right = [c for c in cmds.get(event, []) if base in c]
        elsewhere = sorted(e for e, cs in cmds.items() if e != event and any(base in c for c in cs))
        if right:
            missing = []
            for c in right:
                for tok in c.split():
                    if tok.endswith(".py") and ("/" in tok) and not os.path.isfile(expand(tok.strip("\"'"))):
                        missing.append(tok)
            if missing:
                r.add("FAIL", label, "wired, but the file it points at does not exist: %s" % missing[0],
                      "fix the path in settings, or copy the script there")
            else:
                r.add("PASS", label, "wired under %s" % event)
        elif elsewhere:
            r.add("FAIL", label, "wired under %s but it must be under %s" % (elsewhere[0], event),
                  "move it. Correct block: " + snippet(script, event))
        else:
            r.add("FAIL", label, "installed but not wired into settings (it will never run)",
                  "add to the \"hooks\" section of %s: %s" % (settings_path, snippet(script, event)))


def check_agent(r):
    cap = os.path.join(home(), "turn-cap-builder", "turn-cap.py")
    if not os.path.isfile(cap):
        r.add("SKIP", "agent", "turn-cap-builder is not installed")
        return
    user = os.path.expanduser("~/.claude/agents/builder.md")
    if not os.path.isfile(user):
        r.add("FAIL", "agent", "turn-cap.py is installed but ~/.claude/agents/builder.md is missing, so no cap applies",
              "copy builder.md from the turn-cap-builder pattern to ~/.claude/agents/builder.md (the user folder, not a project folder)")
        return
    try:
        with open(user) as f:
            text = f.read()
    except OSError:
        text = ""
    if "turn-cap.py" not in text:
        r.add("FAIL", "agent", "~/.claude/agents/builder.md does not call turn-cap.py, so it is not capped",
              "copy the hooks: block from the turn-cap-builder example into your builder agent")
        return
    proj = os.path.join(os.getcwd(), ".claude", "agents", "builder.md")
    if os.path.isfile(proj) and proj != user:
        r.add("WARN", "agent", "a project builder.md exists here; Claude Code skips hooks written in a project "
              "agent until the folder is trusted",
              "keep the capped builder in ~/.claude/agents, or trust this folder first")
    else:
        r.add("PASS", "agent", "capped builder agent is in your user folder")


def check_ledger(r):
    path = os.environ.get("LEDGER_PATH") or os.path.join(home(), "state", "ledger.jsonl")
    if not os.path.isfile(path):
        r.add("SKIP", "ledger", "no ledger file yet (it appears with your first ticket)")
        return
    bad, n = [], 0
    try:
        with open(path, errors="replace") as f:
            for i, line in enumerate(f, 1):
                if not line.strip():
                    continue
                n += 1
                try:
                    if not isinstance(json.loads(line), dict):
                        raise ValueError
                except ValueError:
                    bad.append(i)
    except OSError as e:
        r.add("FAIL", "ledger", "cannot read the ledger (%s)" % e.strerror, "check the file's permissions")
        return
    if bad:
        r.add("FAIL", "ledger", "%d line(s) do not parse, first at line %d" % (len(bad), bad[0]),
              "copy the file somewhere safe, then repair or delete line %d (each line must be one JSON object)" % bad[0])
    else:
        r.add("PASS", "ledger", "%d event(s), every line parses" % n)


def read_beat(path):
    try:
        with open(path) as f:
            parts = f.read().split()
    except (OSError, ValueError):
        return None, None
    if not parts or len(parts) > 2 or not all(re.fullmatch(r"[0-9]+", p) for p in parts):
        return None, None
    return int(parts[0]), (int(parts[1]) if len(parts) == 2 else None)


def ago(sec):
    return "%dm" % (sec // 60) if sec < 7200 else "%.1fh" % (sec / 3600)


def registered_limit(name):
    """The max age (seconds) `deadman beat --max-age` remembered for this job, or None."""
    try:
        with open(os.path.join(home(), "state", "registry", name)) as f:
            raw = f.read().strip()
    except (OSError, ValueError):
        return None
    return int(raw) if re.fullmatch(r"[0-9]+", raw) and int(raw) > 0 else None


def check_heartbeats(r):
    d = os.path.join(home(), "state", "heartbeats")
    try:
        names = {n for n in os.listdir(d) if ".tmp-" not in n}
    except OSError:
        names = set()
    try:
        reg = {n for n in os.listdir(os.path.join(home(), "state", "registry")) if ".tmp-" not in n}
    except OSError:
        reg = set()
    names |= reg
    if loop_owns_tick():
        names.discard("work-loop-tick")      # judged by check_loop, with a clearer fix line
    if not names:
        r.add("SKIP", "heartbeats", "no scheduled jobs are registered with deadman yet")
        return
    for n in sorted(names):
        epoch, limit = read_beat(os.path.join(d, n))
        if limit is None:
            limit = registered_limit(n)       # same order as deadman: the beat's own limit, then the registry's
        label = "job %s" % n
        if epoch is None and n in reg and not os.path.exists(os.path.join(d, n)):
            r.add("FAIL", label, "it is registered with deadman but its heartbeat file is gone",
                  "run the job once, or: deadman.py beat %s --max-age <how long a gap is normal>" % n)
        elif epoch is None:
            r.add("FAIL", label, "its heartbeat file is corrupt",
                  "run the job once, or: deadman.py beat %s --max-age <how long a gap is normal>" % n)
        elif epoch > now():
            r.add("FAIL", label, "its last beat is dated in the future (clock problem)",
                  "check the machine's date, then beat again")
        elif limit is None:
            r.add("SKIP", label, "last beat %s ago, but no max age is recorded" % ago(now() - epoch))
        elif now() - epoch > limit:
            r.add("FAIL", label, "last beat %s ago, limit %s: the job has stopped or is failing" % (ago(now() - epoch), ago(limit)),
                  "run the job by hand, read its own log, and check the scheduler entry is loaded")
        else:
            r.add("PASS", label, "last beat %s ago (limit %s)" % (ago(now() - epoch), ago(limit)))


def check_alarms(r):
    p = os.path.join(home(), "state", "alarms.log")
    try:
        with open(p, errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        r.add("SKIP", "alarms", "no alarms log (nothing has ever alarmed)")
        return
    recent = 0
    for ln in lines:
        try:
            ts = datetime.datetime.fromisoformat(ln.split()[0]).timestamp()
        except (ValueError, IndexError):
            continue
        if now() - ts < 86400:
            recent += 1
    if recent:
        r.add("WARN", "alarms", "%d alarm(s) in the last 24 hours" % recent,
              "read the alarms log in the install folder's state folder; each line names the job")
    else:
        r.add("PASS", "alarms", "no alarms in the last 24 hours")


def check_disk(r):
    try:
        warn = int(os.environ.get("DISK_GUARD_WARN_GI") or 20)
        crit = int(os.environ.get("DISK_GUARD_CRIT_GI") or 5)
    except ValueError:
        warn, crit = 20, 5
    ov = os.environ.get("DOCTOR_FREE_GI_OVERRIDE")
    try:
        path = home() if os.path.isdir(home()) else os.path.expanduser("~")
        free = int(ov) if ov and ov.isdigit() else shutil.disk_usage(path).free // (1 << 30)
    except OSError:
        r.add("FAIL", "disk", "could not read free disk space", "check the disk is mounted and readable")
        return
    if free < crit:
        r.add("FAIL", "disk", "%d GiB free (below %d)" % (free, crit),
              "free space now: empty the trash, delete build output and caches you can regenerate")
    elif free < warn:
        r.add("WARN", "disk", "%d GiB free (below %d)" % (free, warn),
              "clear space soon: delete build output and caches you can regenerate")
    else:
        r.add("PASS", "disk", "%d GiB free" % free)


LOOP_TICK_MAX_S = 30 * 60            # work-loop tick: three missed 10-minute ticks
REGISTER_TICK_MAX_S = 2 * 60 * 60    # failure-register: it is scheduled every 30 minutes


def loop_installed():
    return os.path.isfile(os.path.join(home(), "work-loop", "work_loop.py"))


def loop_lanes():
    """(names, error). names is None when there is no lanes.json, else the lane names in it."""
    p = os.path.join(home(), "state", "lanes.json")
    if not os.path.exists(p):
        return None, None
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        lanes = data.get("lanes") if isinstance(data, dict) else None
        if not isinstance(lanes, dict):
            raise ValueError("no lanes object")
        return sorted(str(k) for k in lanes), None
    except (OSError, ValueError):
        return None, "lanes.json is unreadable"


def loop_owns_tick():
    """True when check_loop judges the work-loop-tick heartbeat, so the generic job row skips it."""
    names, err = loop_lanes()
    return loop_installed() and bool(names)


def last_event_epoch(kind):
    """Epoch seconds of the newest `kind` line in state/events.jsonl, or None. Reads the last 1 MiB."""
    p = os.path.join(home(), "state", "events.jsonl")
    try:
        with open(p, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - (1 << 20)))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return None
    for ln in reversed(lines):
        try:
            rec = json.loads(ln)
            if isinstance(rec, dict) and rec.get("event") == kind:
                return calendar.timegm(time.strptime(rec["ts"], "%Y-%m-%dT%H:%M:%SZ"))
        except (ValueError, KeyError, TypeError, OverflowError):
            continue
    return None


def check_loop(r, settings_path):
    """The always-running loop: is the tick alive, is the optional Stop arm wired to a file that exists."""
    names, err = loop_lanes()
    settings, _ = load_settings(settings_path)
    stop_cmds = []
    if settings is not None:
        stop_cmds = [c for c in commands_by_event(settings).get("Stop", []) if "stop_arm.py" in c]
    if stop_cmds:
        missing = []
        for c in stop_cmds:
            toks = [t.strip("\"'") for t in c.split() if t.strip("\"'").endswith("stop_arm.py")]
            if not any("/" in t and os.path.isfile(expand(t)) for t in toks):
                missing.append(c)
        if missing:
            r.add("FAIL", "stop-arm", "wired under Stop, but the file it points at does not exist",
                  "install it (./install.sh --only work-loop) or remove the Stop entry from " + settings_path)
        elif not names:
            r.add("WARN", "stop-arm", "wired under Stop, but no lane is registered, so it never continues anything",
                  "register a lane: work_loop.py lane add NAME, and start the chat with HUBKIT_LANE=NAME")
        else:
            r.add("PASS", "stop-arm", "wired under Stop, file exists, %d lane(s) registered" % len(names))
    if loop_installed():
        if err:
            r.add("FAIL", "loop-lanes", err, "fix the JSON in state/lanes.json, or delete it and re-add the lanes")
        elif not names:
            r.add("SKIP", "loop-tick", "no lane is registered yet (work_loop.py lane add NAME)")
        else:
            epoch, limit = read_beat(os.path.join(home(), "state", "heartbeats", "work-loop-tick"))
            limit = limit or LOOP_TICK_MAX_S
            if epoch is None and not os.path.exists(os.path.join(home(), "state", "heartbeats", "work-loop-tick")):
                r.add("WARN", "loop-tick", "%d lane(s) registered but the tick has never run" % len(names),
                      "schedule: python3 work_loop.py tick every 10 minutes (see patterns/schedule-examples)")
            elif epoch is None:
                r.add("FAIL", "loop-tick", "its heartbeat file is corrupt", "run work_loop.py tick once by hand")
            elif epoch > now():
                r.add("FAIL", "loop-tick", "its last beat is dated in the future (clock problem)",
                      "check the machine's date, then run work_loop.py tick")
            elif now() - epoch > limit:
                r.add("FAIL", "loop-tick", "last tick %s ago, limit %s: lanes are no longer being fed" % (ago(now() - epoch), ago(limit)),
                      "run work_loop.py tick by hand, then check the scheduler entry is loaded")
            else:
                r.add("PASS", "loop-tick", "last tick %s ago (limit %s)" % (ago(now() - epoch), ago(limit)))
            fed = last_event_epoch("loop.feed")
            if fed is None:
                r.add("SKIP", "loop-feed", "no ticket has been fed to a lane yet")
            else:
                r.add("PASS", "loop-feed", "last ticket fed %s ago" % (ago(now() - fed) if fed <= now() else "0m"))
    if os.path.isfile(os.path.join(home(), "failure-register", "failure_register.py")):
        state = os.path.join(home(), "state")
        tick = os.path.join(state, "failure-register.tick")
        if not os.path.exists(os.path.join(state, "failures.jsonl")) and not os.path.exists(tick):
            r.add("SKIP", "failure-register", "no failures recorded yet")
            return
        try:
            with open(tick, encoding="utf-8") as f:
                at = calendar.timegm(time.strptime(f.read().strip(), "%Y-%m-%dT%H:%M:%SZ"))
        except FileNotFoundError:
            r.add("WARN", "failure-register", "failures are recorded but the register has never run",
                  "schedule: python3 failure_register.py every 30 minutes")
            return
        except (OSError, ValueError, OverflowError):
            r.add("WARN", "failure-register", "its tick file is unreadable", "run failure_register.py once by hand")
            return
        if at > now() or now() - at > REGISTER_TICK_MAX_S:
            r.add("WARN", "failure-register", "last run %s: older than %s or dated in the future" % (
                ago(now() - at) + " ago" if at <= now() else "in the future", ago(REGISTER_TICK_MAX_S)),
                "run failure_register.py by hand and check its scheduler entry")
        else:
            r.add("PASS", "failure-register", "last run %s ago" % ago(now() - at))


def scrub(text):
    """Make output safe to paste: no home path, no user name, no other absolute paths."""
    h = os.path.expanduser("~")
    try:
        user = getpass.getuser()
    except Exception:
        user = ""
    names = {u for u in (os.environ.get("USER"), os.environ.get("LOGNAME"), user, os.path.basename(h))
             if u and len(u) >= 2 and u not in ("home", "root", "Users")}
    text = re.sub(re.escape(h), "~", text, flags=re.IGNORECASE)
    for u in sorted(names, key=len, reverse=True):
        text = re.sub(r"(?<![A-Za-z0-9])" + re.escape(u) + r"(?![A-Za-z0-9])", "<user>", text, flags=re.IGNORECASE)
    return re.sub(r"(?<![\w~$.<>])/(?:[\w.@+-]+/)+([\w.@+-]*)", r"<path>/\1", text)


def render(r):
    out = ["hubkit doctor"]
    for status, name, detail, fix in r.rows:
        out.append("%-5s %s: %s" % (status, name, detail))
        if fix and status in ("FAIL", "WARN"):
            out.append("      fix: " + fix)
    c = r.counts()
    out.append("summary: %d pass, %d fail, %d warn, %d skip" % (c["PASS"], c["FAIL"], c["WARN"], c["SKIP"]))
    return "\n".join(out)


def main(argv):
    paste = nolog = False
    settings_path = os.environ.get("DOCTOR_SETTINGS") or os.path.expanduser("~/.claude/settings.json")
    it = iter(argv)
    for a in it:
        if a == "--paste":
            paste = True
        elif a == "--no-log":
            nolog = True
        elif a == "--settings":
            v = next(it, None)
            if not v:
                print(__doc__)
                return 2
            settings_path = os.path.expanduser(v)
        else:
            print(__doc__)
            return 2
    r = Report()
    for fn in (check_python, check_home):
        fn(r)
    check_hooks(r, settings_path)
    for fn in (check_agent, check_ledger, check_heartbeats, check_alarms, check_disk):
        fn(r)
    check_loop(r, settings_path)
    text = render(r)
    print(scrub(text) if paste else text)
    failed = r.counts()["FAIL"]
    if not nolog:
        try:
            if not os.path.isdir(os.path.join(home(), "state")):
                return 1 if failed else 0           # read-only: never create the folder we just diagnosed
            with open(os.path.join(home(), "state", "events.log"), "a") as f:
                f.write("%s doctor_run fail=%d\n" % (datetime.datetime.now().isoformat(timespec="seconds"), failed))
        except OSError:
            pass
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
