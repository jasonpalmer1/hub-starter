#!/usr/bin/env python3
"""disk-guard: a read-only "is there room to build?" verdict.

Prints one plain-English line, logs it, and exits with a code:
  0 OK    plenty of room, builds allowed
  1 WARN  getting low, run one build at a time
  2 CRIT  too low (or unreadable), start no new builds

It reads free space and counts running build tools (npm, yarn, vite, ...). It deletes
nothing, kills nothing and writes nothing except its own log line. Safe to run any time.
An unreadable disk is treated as CRIT: a broken check never says "fine".

  python3 disk-guard.py              check the disk that holds $HUBKIT_HOME (or your home)
  python3 disk-guard.py --path DIR   check the disk that holds DIR
Use it before starting a build:
  python3 disk-guard.py; [ $? -eq 2 ] && echo "disk too full, not building"

Settings (environment):
  DISK_GUARD_WARN_GI   WARN below this many GiB free (default 20)
  DISK_GUARD_CRIT_GI   CRIT below this many GiB free (default 5); must not exceed WARN
  DISK_GUARD_PATH      same as --path
  DISK_GUARD_LOG       log file (default $HUBKIT_HOME/state/disk-guard.log)
  DISK_GUARD_CACHE_SECS  how long a comfortable OK verdict is reused (default 1800 = 30 min; 0 = never cache)
  DISK_GUARD_CACHE_MIN_GI  an OK verdict is only cached at or above this many GiB free (default 100)
  DISK_GUARD_CACHE_FILE  where the OK verdict is kept (default $HUBKIT_HOME/state/disk-guard-ok-cache.txt)
  DISK_GUARD_FREE_GI_OVERRIDE, DISK_GUARD_PS_FILE   tests only

Cache: a guard that runs on every build start can fire hundreds of times a day. A comfortable OK
(well above the WARN line) is reused for 30 minutes with no disk read, no process listing and no log
write. WARN, CRIT, an unreadable disk and any OK under the cache floor are NEVER cached, so the
tiers that matter always read the live disk. A cached line ends with "[cached Ns]". Setting any of
DISK_GUARD_FREE_GI_OVERRIDE, DISK_GUARD_PS_FILE, DISK_GUARD_LOG or DISK_GUARD_PATH bypasses the cache
(unless DISK_GUARD_CACHE_FILE is named, which is how the tests exercise it). Undo: DISK_GUARD_CACHE_SECS=0.
"""
import datetime
import os
import shutil
import subprocess
import sys
import time

BUILD_TOOLS = {"npm", "npx", "yarn", "pnpm", "vite", "webpack", "turbo", "next", "cargo", "gradle", "make"}


def home():
    return os.path.expanduser(os.environ.get("HUBKIT_HOME") or "~/.claude/hubkit")


def gi_setting(name, default):
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    if raw.isdigit():
        return int(raw)
    raise ValueError("%s must be a whole number of GiB, got %r" % (name, raw))


def free_gi(path):
    ov = os.environ.get("DISK_GUARD_FREE_GI_OVERRIDE")
    if ov is not None and ov != "":
        return int(ov) if ov.isdigit() else None
    try:
        return shutil.disk_usage(path).free // (1 << 30)
    except (OSError, ValueError):
        return None


def process_lines():
    f = os.environ.get("DISK_GUARD_PS_FILE")
    if f:
        try:
            with open(f) as fh:
                return fh.read().splitlines()
        except OSError:
            return []
    try:
        r = subprocess.run(["ps", "-axo", "command="], capture_output=True, text=True, timeout=10)
        return r.stdout.splitlines()
    except (OSError, subprocess.SubprocessError):
        return []


def count_builds(lines):
    """A process counts when one of its first three words IS a build tool (by program name).
    Matching words, not text, so a path or a file that merely contains 'make' does not count."""
    n = 0
    for ln in lines:
        words = ln.split()[:3]
        if any(os.path.basename(w) in BUILD_TOOLS for w in words):
            n += 1
    return n


def log(line):
    path = os.environ.get("DISK_GUARD_LOG") or os.path.join(home(), "state", "disk-guard.log")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as f:
            f.write("%s %s\n" % (datetime.datetime.now().isoformat(timespec="seconds"), line))
    except OSError:
        pass


def cache_settings(path):
    """(file, secs, min_gi) when the OK cache is on for this call, else None."""
    raw = os.environ.get("DISK_GUARD_CACHE_SECS", "1800")
    mn = os.environ.get("DISK_GUARD_CACHE_MIN_GI", "100")
    if not (raw.isdigit() and mn.isdigit()) or int(raw) == 0:
        return None
    f = os.environ.get("DISK_GUARD_CACHE_FILE")
    if not f:
        if any(os.environ.get(k) for k in ("DISK_GUARD_FREE_GI_OVERRIDE", "DISK_GUARD_PS_FILE",
                                           "DISK_GUARD_LOG", "DISK_GUARD_PATH")):
            return None
        f = os.path.join(home(), "state", "disk-guard-ok-cache.txt")
    return f, int(raw), int(mn)


def cached_line(cs, path):
    """The saved OK line if it is fresh, for the same path, and really an OK line; else None."""
    try:
        age = time.time() - os.stat(cs[0]).st_mtime
        with open(cs[0]) as fh:
            saved_path, _, line = fh.read().rstrip("\n").partition("\t")
    except OSError:
        return None
    if 0 <= age < cs[1] and saved_path == path and " - OK (" in line and line.startswith("disk-guard: "):
        return "%s [cached %ds]" % (line, int(age))
    return None


def save_ok(cs, path, line):
    try:
        os.makedirs(os.path.dirname(cs[0]), exist_ok=True)
        tmp = "%s.tmp.%d" % (cs[0], os.getpid())
        with open(tmp, "w") as fh:
            fh.write("%s\t%s\n" % (path, line))
        os.replace(tmp, cs[0])
    except OSError:
        pass


def main(argv):
    path = os.environ.get("DISK_GUARD_PATH") or None
    it = iter(argv)
    for a in it:
        if a == "--path":
            path = next(it, None)
        else:
            print(__doc__)
            return 2
    if path is None:
        path = home() if os.path.isdir(home()) else os.path.expanduser("~")
    try:
        warn, crit = gi_setting("DISK_GUARD_WARN_GI", 20), gi_setting("DISK_GUARD_CRIT_GI", 5)
        if crit > warn:
            raise ValueError("DISK_GUARD_CRIT_GI (%d) must not be above DISK_GUARD_WARN_GI (%d)" % (crit, warn))
    except ValueError as e:
        print("disk-guard: bad setting: %s" % e)
        return 2
    cs = cache_settings(path)
    if cs:
        hit = cached_line(cs, path)
        if hit:
            print(hit)
            return 0
    free = free_gi(path)
    if free is None:
        line = "disk-guard: could not read free space for %s - treating as CRIT (a broken check never says fine)" % path
        print(line)
        log(line)
        return 2
    builds = count_builds(process_lines())
    if free < crit:
        verdict, code, note = "CRIT", 2, "no new builds"
    elif free < warn:
        verdict, code, note = "WARN", 1, "run one build at a time"
    else:
        verdict, code, note = "OK", 0, "builds allowed"
    line = "disk-guard: %dGi free - %s (%s) - build tools running: %d" % (free, verdict, note, builds)
    print(line)
    log(line)
    if cs and verdict == "OK" and free >= cs[2]:
        save_ok(cs, path, line)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
