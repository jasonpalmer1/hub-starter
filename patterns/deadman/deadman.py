#!/usr/bin/env python3
"""deadman: notice a scheduled job that has quietly stopped.

A job that dies makes no error, only silence. So the job proves it is alive: each
successful run calls `deadman beat NAME`. A separate, independent check calls
`deadman check NAME` and raises ONE alarm when the last beat is too old, missing,
unreadable or dated in the future (never silent).

  deadman beat NAME [--max-age 25m]    record "NAME just succeeded". With --max-age the
                                       allowed gap is remembered for `check --all` and doctor.
  deadman check NAME [NAME...] [--max-age 25m] [--realarm 2h]
  deadman check --all [--realarm 2h]   every beat that has a remembered max-age
  deadman list                         every beat and its age
  deadman retire NAME                  stop watching NAME for good (removes its beat, registered
                                       limit and alarm stamp). Use it when a job is switched off on
                                       purpose; otherwise its missing beat alarms forever.

Exit codes for check: 0 all fresh, 1 something stale or missing, 2 usage error.
Ages: 90s, 25m, 3h, 1d, or a bare number of seconds.

Files (all under $HUBKIT_HOME, default ~/.claude/hubkit):
  state/heartbeats/NAME        one line: EPOCH [MAX_AGE_SECONDS]
  state/registry/NAME          the max age (seconds) of a job registered by `beat --max-age`;
                               `check --all` checks every registered job, so a deleted beat alarms
  state/deadman/NAME.alarm     epoch of the last alarm for NAME (limits repeats)
  state/alarms.log             one line per alarm
  state/events.log             local log of what ran (never sent anywhere)

When an alarm fires and $HUBKIT_NOTIFY_CMD is set, it is run as
  $HUBKIT_NOTIFY_CMD "<title>" "<reason>"
(split like a shell would, no shell involved). A failing notify command never hides the alarm.
DEADMAN_NOW (epoch seconds) replaces the clock; it exists for tests.
"""
import datetime
import os
import re
import shlex
import subprocess
import sys
import time

NAME_RX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
AGE_RX = re.compile(r"^(\d+)([smhd]?)$")
UNIT = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}
DEFAULT_REALARM = 7200


def home():
    return os.path.expanduser(os.environ.get("HUBKIT_HOME") or "~/.claude/hubkit")


def state():
    return os.path.join(home(), "state")


def beats_dir():
    return os.path.join(state(), "heartbeats")


def registry_dir():
    return os.path.join(state(), "registry")


def registered():
    """Names of every job registered by `beat --max-age` (unreadable folder: none)."""
    try:
        return sorted(n for n in os.listdir(registry_dir()) if NAME_RX.match(n) and ".tmp-" not in n)
    except OSError:
        return []


def registered_limit(name):
    try:
        with open(os.path.join(registry_dir(), name)) as f:
            raw = f.read().strip()
    except (OSError, ValueError):
        return None
    return int(raw) if re.fullmatch(r"[0-9]+", raw) and int(raw) > 0 else None


def alarm_dir():
    return os.path.join(state(), "deadman")


def now():
    raw = os.environ.get("DEADMAN_NOW")
    if raw and raw.isdigit():
        return int(raw)
    return int(time.time())


def parse_age(text):
    m = AGE_RX.match((text or "").strip().lower())
    if not m or int(m.group(1)) <= 0:
        return None
    return int(m.group(1)) * UNIT[m.group(2)]


def human(sec):
    sec = max(0, int(sec))
    if sec >= 86400:
        return "%.1fd" % (sec / 86400)
    if sec >= 3600:
        return "%.1fh" % (sec / 3600)
    if sec >= 60:
        return "%dm" % (sec // 60)
    return "%ds" % sec


def event(line):
    try:
        os.makedirs(state(), exist_ok=True)
        ts = datetime.datetime.now().isoformat(timespec="seconds")
        with open(os.path.join(state(), "events.log"), "a") as f:
            f.write("%s %s\n" % (ts, line))
    except OSError:
        pass


def read_beat(name):
    """(epoch, max_age or None) or (None, reason)."""
    path = os.path.join(beats_dir(), name)
    try:
        with open(path) as f:
            raw = f.read()
    except FileNotFoundError:
        return None, "no heartbeat recorded (never beat, or the file was deleted)"
    except OSError as e:
        return None, "heartbeat unreadable (%s)" % e.strerror
    except ValueError:
        return None, "heartbeat file is corrupt (not text)"
    parts = raw.split()
    if not parts or len(parts) > 2 or not all(re.fullmatch(r"[0-9]+", p) for p in parts):
        return None, "heartbeat file is corrupt"
    return int(parts[0]), (int(parts[1]) if len(parts) == 2 and int(parts[1]) > 0 else None)


def cmd_beat(name, max_age):
    os.makedirs(beats_dir(), exist_ok=True)
    final = os.path.join(beats_dir(), name)
    tmp = final + ".tmp-%d" % os.getpid()
    line = str(now()) + (" %d" % max_age if max_age else "")
    with open(tmp, "w") as f:
        f.write(line + "\n")
    os.replace(tmp, final)
    if max_age:
        os.makedirs(registry_dir(), exist_ok=True)
        rtmp = os.path.join(registry_dir(), name + ".tmp-%d" % os.getpid())
        with open(rtmp, "w") as f:
            f.write("%d\n" % max_age)
        os.replace(rtmp, os.path.join(registry_dir(), name))
    print("beat %s" % name)
    return 0


def judge(name, max_age):
    """Returns (ok, message, effective max_age)."""
    epoch, extra = read_beat(name)
    if epoch is None:
        return False, "%s: %s" % (name, extra), max_age
    limit = max_age or extra or registered_limit(name)
    if not limit:
        return False, "%s: no max age known (pass --max-age, or beat with --max-age once)" % name, None
    t = now()
    if epoch > t:
        return False, "%s: last beat is dated in the future (clock problem or bad file)" % name, limit
    age = t - epoch
    if age > limit:
        return False, "%s: last beat %s ago, limit %s" % (name, human(age), human(limit)), limit
    return True, "%s: ok, last beat %s ago (limit %s)" % (name, human(age), human(limit)), limit


def alarm(name, reason, realarm, title=None):
    """Log + notify once per realarm window. Returns a short note for the terminal."""
    stamp = os.path.join(alarm_dir(), name + ".alarm")
    try:
        with open(stamp) as f:
            last = f.read().strip()
        if last.isdigit() and 0 <= now() - int(last) < realarm:
            return "alarm already sent %s ago, not repeating" % human(now() - int(last))
    except (OSError, ValueError):
        pass
    try:
        os.makedirs(alarm_dir(), exist_ok=True)
        with open(stamp, "w") as f:
            f.write(str(now()) + "\n")
    except OSError as e:
        print("deadman: cannot write alarm stamp (%s); alarm may repeat" % e.strerror, file=sys.stderr)
    try:
        os.makedirs(state(), exist_ok=True)
        with open(os.path.join(state(), "alarms.log"), "a") as f:
            f.write("%s deadman ALARM %s\n" % (datetime.datetime.now().isoformat(timespec="seconds"), reason))
    except OSError as e:
        print("deadman: cannot write alarms.log (%s)" % e.strerror, file=sys.stderr)
    event("deadman_alarm %s" % name)
    note = "alarm logged"
    cmd = os.environ.get("HUBKIT_NOTIFY_CMD", "").strip()
    if cmd:
        try:
            r = subprocess.run(shlex.split(cmd) + [title or "deadman: %s is not beating" % name, reason],
                               capture_output=True, text=True, timeout=20)
            note += ", notified" if r.returncode == 0 else ", notify command failed (exit %d)" % r.returncode
        except (OSError, ValueError, subprocess.SubprocessError) as e:
            note += ", notify command failed (%s)" % type(e).__name__
    return note


def clear_alarm(name):
    try:
        os.remove(os.path.join(alarm_dir(), name + ".alarm"))
    except OSError:
        pass


def cmd_check(names, max_age, realarm, every):
    if every:
        # Every REGISTERED job (so a deleted beat alarms), plus any beat file that carries a
        # limit or is corrupt. A beat with no limit anywhere cannot be judged (see list).
        try:
            found = sorted(n for n in os.listdir(beats_dir()) if NAME_RX.match(n) and ".tmp-" not in n)
            problem = "" if found else "the heartbeats folder is empty"
        except OSError as e:
            found, problem = [], "the heartbeats folder is missing or unreadable (%s)" % (e.strerror or "error")
        reg = registered()
        names = set(reg)
        for n in found:
            epoch, extra = read_beat(n)
            if epoch is None or isinstance(extra, int):
                names.add(n)
        names = sorted(names)
        if names:
            clear_alarm("deadman-watcher")
        else:
            reason = ("watcher: nothing to check, %s and no job is registered "
                      "(beat each job once with --max-age to register it)") % (problem or "no beat carries a max age")
            print("STALE deadman " + reason + " - " +
                  alarm("deadman-watcher", reason, realarm, "deadman: the watcher has nothing to watch"))
            return 1
    bad = 0
    for name in names:
        ok, msg, _ = judge(name, max_age)
        if ok:
            print(msg)
            clear_alarm(name)
        else:
            bad = 1
            msg += " (switched off on purpose? run: deadman.py retire %s)" % name
            print("STALE " + msg + " - " + alarm(name, msg, realarm))
    return bad


def cmd_retire(name):
    gone = []
    for path in (os.path.join(beats_dir(), name), os.path.join(registry_dir(), name),
                 os.path.join(alarm_dir(), name + ".alarm")):
        try:
            os.remove(path)
            gone.append(path)
        except FileNotFoundError:
            pass
        except OSError as e:
            print("deadman: cannot remove %s (%s)" % (path, e.strerror), file=sys.stderr)
            return 1
    if not gone:
        print("deadman: nothing to retire, no job named %s is known" % name, file=sys.stderr)
        return 1
    event("deadman_retire %s" % name)
    print("retired %s" % name)
    return 0


def cmd_list():
    try:
        found = [n for n in os.listdir(beats_dir()) if NAME_RX.match(n) and ".tmp-" not in n]
    except OSError:
        found = []
    names = sorted(set(found) | set(registered()))
    if not names:
        print("no heartbeats yet")
    for n in names:
        epoch, extra = read_beat(n)
        if epoch is None:
            print("%-24s %s" % (n, extra))
        else:
            print("%-24s last beat %s ago, limit %s" % (
                n, human(now() - epoch) if epoch <= now() else "(future)", human(extra) if extra else "not set"))
    return 0


def usage():
    print(__doc__)
    return 2


def main(argv):
    if not argv or argv[0] in ("-h", "--help", "help"):
        return usage()
    cmd, rest = argv[0], argv[1:]
    names, max_age, realarm, every = [], None, DEFAULT_REALARM, False
    it = iter(rest)
    for a in it:
        if a in ("--max-age", "--realarm"):
            v = parse_age(next(it, ""))
            if v is None:
                print("deadman: %s needs an age like 25m, 3h or 90s" % a, file=sys.stderr)
                return 2
            if a == "--max-age":
                max_age = v
            else:
                realarm = v
        elif a == "--all":
            every = True
        elif a.startswith("-"):
            return usage()
        else:
            names.append(a)
    for n in names:
        if not NAME_RX.match(n):
            print("deadman: bad name %r (letters, digits, . _ - only, up to 64)" % n, file=sys.stderr)
            return 2
    if cmd == "beat":
        if len(names) != 1 or every:
            return usage()
        return cmd_beat(names[0], max_age)
    if cmd == "check":
        if every == bool(names):
            return usage()
        return cmd_check(names, max_age, realarm, every)
    if cmd == "retire":
        if len(names) != 1 or every or max_age is not None:
            return usage()
        return cmd_retire(names[0])
    if cmd == "list":
        return cmd_list()
    return usage()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
