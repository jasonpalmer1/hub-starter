#!/usr/bin/env python3
"""lane-lock: one owner per lane, plus a gate-fail circuit breaker.

A lane is any name you pick for a unit of work that only one worker may touch at a time
(usually a repo, or a ticket's scope). Two things go wrong without a mechanical lock: two
workers build in the same repo at once because nothing on disk said the lane was taken, and a
worker keeps retrying a ship into a lane whose last gate results were both FAIL.

  lane-lock claim <lane> --owner NAME --pid PID [--session ID] [--reason TEXT]
  lane-lock release <lane> --owner NAME
  lane-lock force-release <lane> --by NAME --reason TEXT
  lane-lock status <lane>
  lane-lock list
  lane-lock record-fail <lane> --ticket ID --gate bug-gate|playtest|other [--detail TEXT] [--verdict FILE] [--tip SHA]
  lane-lock record-pass <lane> --ticket ID --gate bug-gate|playtest|other [--detail TEXT]
  lane-lock reset <lane> --by judge|hub|NAME --reason TEXT

Exit codes: 0 ok, 1 usage or internal error, 2 lane held by another live owner (also: a corrupt
owner record, which looks the same to a caller; also a REFUSED reset, which is printed as
"REFUSED"), 3 breaker tripped (claim refused, also a corrupt breaker record).

Ownership
  - `claim` needs --pid, the pid of the long-lived caller (a build job, a session). It is required
    because the CLI's own pid is dead the moment the command returns, which would let a second
    claim succeed one second later. Pass $$ from a shell.
  - A holder whose pid is gone is cleared automatically on the next claim ("self-healed").
  - A pid that is alive but whose process start time no longer matches the one recorded at claim
    time is a different process that reused the pid, and is treated as gone too. The start time is
    read with `ps` under a pinned TZ and locale, so the same live process always gives the same
    fingerprint. A fingerprint in an unknown format is never proof of death: the holder is assumed
    alive and an alarm line says so.
  - Only the owner can `release`. `force-release` is the always-available override for a lane stuck
    on a dead owner that can never come back; it names who and why in the alarms log.
  - A record that exists but cannot be read (cut off, not text, not an object) FAILS CLOSED: the
    lane counts as held by an unknown owner (owner.json) or as tripped (breaker.json), a warning is
    printed and an alarm line written. Only a missing file means "free".

Breaker
  - `record-fail` counts consecutive FAILs per lane. At LANE_BREAKER_THRESHOLD (default 2) the
    breaker TRIPS: an alarm line is written and every `claim` is refused (exit 3).
  - `record-pass` zeroes the streak of an untripped lane. On a TRIPPED lane it clears nothing: the row
    is tagged while_tripped and the streak is kept, so a PASS cannot launder the count.
  - `reset` of a tripped lane (or any lane with 2+ FAILs since the last real PASS, a corrupt breaker.json,
    or an odd-typed `tripped` value) needs BOTH `--by judge` or `--by hub` AND a judge ruling cited in
    --reason as `reports/judge/<name>.md` or `reports/<failed ticket>/judge-r<N>.md` (under
    $LANE_LOCK_REPORTS). The file must be a real file inside that folder (symlinks resolved; a symlinked
    folder pointing elsewhere is refused), hold a Verdict or Ruling line outside code fences, name the lane
    or a failed ticket, and be no older than the trip. A trailing period after the name is refused. A
    refused reset exits 2, prints REFUSED and writes an alarm line. A tripped lane is floored at 2 FAILs,
    so a history that rolled off the 20-row window cannot make the reset easy. Both `--by` and file mtime
    are forgeable by anyone who can write the reports folder; the guard is friction plus an audit trail
    (the reset row records ruled=true), not a signature.
  - A lane that was never tripped (0 or 1 FAILs, or a PASS since) can still be reset by anyone.

Files (all under $HUBKIT_HOME, default ~/.claude/hubkit, every one overridable):
  LANE_LOCK_ROOT          default $HUBKIT_HOME/state/lanes/<lane>/{owner.json,breaker.json}
  LANE_LOCK_ALARMS_LOG    default $HUBKIT_HOME/state/alarms.log   (same file deadman writes)
  LANE_BREAKER_THRESHOLD  default 2
  LANE_LOCK_REPORTS       default $HUBKIT_HOME/reports   (where cited judge rulings live)
  LANE_LOCK_WAIT_S        default 5   (how long to wait for the short per-lane critical section)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

LANE_RX = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")   # used with fullmatch: "$" would let a trailing newline through
GATES = ["bug-gate", "playtest", "other"]
# Bookkeeping dirs under the lanes root that are NOT lanes. An exact-name set, never a prefix test: a near-miss such as
# ".keylocks2" must stay visible (a prefix match would hide a real, possibly tripped, lane).
NON_LANE_DIRS = frozenset({".keylocks"})


def hubkit_home() -> Path:
    return Path(os.path.expanduser(os.environ.get("HUBKIT_HOME") or "~/.claude/hubkit"))


def root() -> Path:
    return Path(os.path.expanduser(os.environ.get("LANE_LOCK_ROOT") or str(hubkit_home() / "state" / "lanes")))


def alarms_path() -> Path:
    return Path(os.path.expanduser(os.environ.get("LANE_LOCK_ALARMS_LOG") or str(hubkit_home() / "state" / "alarms.log")))


def threshold() -> int:
    try:
        return max(1, int(os.environ.get("LANE_BREAKER_THRESHOLD") or 2))
    except ValueError:
        return 2


def lock_wait() -> float:
    try:
        return float(os.environ.get("LANE_LOCK_WAIT_S") or 5.0)
    except ValueError:
        return 5.0


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def append_alarm(line: str) -> None:
    try:
        p = alarms_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a") as f:
            f.write(f"{now_iso()} [lane-lock] {line}\n")
    except OSError:
        pass  # logging an alarm must never crash the caller


WINDOWS_MSG = ("lane-lock error: native Windows is not supported (it cannot check whether a holder is "
               "alive). Run the kit inside WSL2: see docs/WINDOWS.md")


def _refuse_on_windows():
    """On native Windows os.kill(pid, 0) is not a liveness probe: it can terminate the process.
    So on win32 nothing here may reach os.kill or ps; refuse before either."""
    if sys.platform == "win32":
        raise NotImplementedError(WINDOWS_MSG)


def pid_alive(pid) -> bool:
    """Liveness only: does some process have this pid. It cannot tell a crashed holder from an
    unrelated process that reused the pid; pid_is_same_process does."""
    _refuse_on_windows()
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just not ours
    except OSError:
        return False


# Tag on the fingerprint. Bump it if the fingerprint or the `ps` call ever changes, so a record
# written under another scheme is never compared byte for byte with a new one.
START_KEY_FMT = "lstart-C-UTC-v1"
_PS_ENV = {"TZ": "UTC", "LC_ALL": "C", "LANG": "C"}   # passed to ps only, never to our own environ


def proc_start_key(pid):
    """{"fmt": ..., "value": <ps start time>} for a live pid, else None (cannot verify)."""
    _refuse_on_windows()
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    try:
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True,
                             text=True, timeout=2, env={**os.environ, **_PS_ENV})
    except (OSError, subprocess.SubprocessError):
        return None
    value = out.stdout.strip() if out.returncode == 0 else ""
    return {"fmt": START_KEY_FMT, "value": value} if value else None


def _recognized(x) -> bool:
    return (isinstance(x, dict) and x.get("fmt") == START_KEY_FMT
            and isinstance(x.get("value"), str) and bool(x.get("value")))


def pid_is_same_process(pid, expected) -> bool:
    """True when pid is alive and, if a current-format start time was recorded, still matches it."""
    if not pid_alive(pid):
        return False
    if not expected:
        return True            # no fingerprint recorded: liveness is all we have
    if not _recognized(expected):
        append_alarm(f"pid {pid}: recorded start_key is in an unrecognized or legacy format "
                     f"(not {START_KEY_FMT!r}); identity is UNKNOWN so the holder is assumed alive. "
                     f"Re-claim the lane once it is safe, to refresh the fingerprint.")
        return True
    current = proc_start_key(pid)
    if current is None or not _recognized(current):
        return True
    return current == expected


class CritSectionError(RuntimeError):
    """The per-lane critical section could not be entered within its bounded wait."""


class _CritSection:
    """Short mkdir lock around one lane's read-modify-write, so two concurrent calls never
    interleave. Held for milliseconds, not for the length of a build. The wait is bounded: a
    wrong-type path at the lock location fails at once, and a stuck lock is stolen once and
    then given up on, never retried without end."""

    def __init__(self, lane_dir: Path, lockdir: Path | None = None):
        self.lockdir = lockdir if lockdir is not None else lane_dir / ".critsec.lock"

    def __enter__(self):
        deadline = time.time() + lock_wait()
        stole = False
        while True:
            try:
                self.lockdir.mkdir(parents=True, exist_ok=False)
                return self
            except FileExistsError:
                if self.lockdir.exists() and not self.lockdir.is_dir():
                    raise CritSectionError(
                        f"lane-lock critsec path {self.lockdir} exists but is not a directory. "
                        f"Not touching it automatically; inspect it, remove it by hand, retry.")
                if time.time() > deadline:
                    if stole:
                        raise CritSectionError(
                            f"lane-lock critsec lock {self.lockdir} did not clear after "
                            f"{lock_wait()}s and one steal attempt; giving up instead of hanging.")
                    try:
                        self.lockdir.rmdir()
                        stole = True
                    except OSError as e:
                        raise CritSectionError(
                            f"lane-lock critsec lock {self.lockdir} timed out and could not be "
                            f"cleared ({type(e).__name__}: {e}).") from e
                    continue
                time.sleep(0.05)

    def __exit__(self, *exc):
        try:
            self.lockdir.rmdir()
        except OSError:
            pass


_TICKET_TAIL = re.compile(r"^(.*?)([a-z][a-z0-9]*)-0*([0-9]{1,9})$")


def lane_key(lane: str) -> str:
    """Canonical key of a lane spelling: case folded, and the zero padding of a trailing ticket-style number folded
    (`repo-L-0007`, `Repo-l-7` and `repo-L-7` share one key). Two spellings with one key are ONE lane to the breaker."""
    s = str(lane).strip().lower()
    m = _TICKET_TAIL.match(s)
    return (m.group(1) + m.group(2) + "-" + str(int(m.group(3)))) if m else s


class _KeyLock(_CritSection):
    """One short lock per canonical lane key, taken before breaker dirs are resolved, so two callers using different
    spellings of one lane serialise instead of each creating its own dir and splitting the FAIL streak.
    Lock order everywhere: key lock first, then the lane-dir lock."""

    def __init__(self, lane: str):
        import hashlib
        name = "n-" + hashlib.sha1(lane_key(lane).encode("utf-8", "replace")).hexdigest()[:20]
        super().__init__(root(), root() / ".keylocks" / name)


def lane_dir(lane: str, create: bool = True) -> Path:
    d = root() / lane
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def write_json_atomic(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(path)


def owner_path(d: Path) -> Path:
    return d / "owner.json"


def breaker_path(d: Path) -> Path:
    return d / "breaker.json"


def _fresh_breaker() -> dict:
    return {"fail_streak": 0, "tripped": False, "tripped_at": None, "history": []}


def _read_record(path: Path):
    """(parsed object, None) | (None, None) when the file is missing | (None, reason) when it exists
    but is cut off, not text, or not a JSON object."""
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None, None
    except (OSError, ValueError) as e:
        return None, type(e).__name__
    try:
        data = json.loads(text)
    except ValueError as e:
        return None, f"{type(e).__name__}: {e}"
    if not isinstance(data, dict):
        return None, "not a JSON object"
    return data, None


def read_breaker_failing_closed(d: Path):
    """(breaker, was_corrupt). Missing file: healthy default. Unreadable file: treated as TRIPPED."""
    data, why = _read_record(breaker_path(d))
    if why is None:
        return (data if data is not None else _fresh_breaker()), False
    msg = (f"lane={d.name} breaker.json is CORRUPT ({why}); failing CLOSED (the lane counts as "
           f"TRIPPED) until a person inspects it or runs `reset`. A safety stop, never a self-heal.")
    print(f"WARNING: {msg}", file=sys.stderr)
    append_alarm(msg)
    return {"fail_streak": max(2, threshold()), "tripped": True, "tripped_at": now_iso(), "history": []}, True


def _tripped(b: dict, corrupt: bool) -> bool:
    return bool(corrupt or b.get("tripped"))


def breaker_states(lane: str) -> list:
    """EVERY lane dir holding a breaker.json whose key equals the key of `lane`, as (dir, breaker, corrupt), canonical
    first: tripped (a corrupt file counts as tripped) before untripped, then highest fail_streak, then dir name. The first
    entry is the ONE dir that record-fail writes; status and claim look at all of them and use the worst state (fail
    closed). Read-only: creates nothing."""
    key = lane_key(lane)
    try:
        names = sorted(n for n in os.listdir(root()) if n not in NON_LANE_DIRS and os.path.lexists(root() / n / "breaker.json"))
    except OSError:
        names = []
    out = []
    for n in names:
        if lane_key(n) == key:
            b, corrupt = read_breaker_failing_closed(root() / n)
            out.append((root() / n, b, corrupt))

    def rank(t):
        try:
            fs = int(t[1].get("fail_streak", 0))
        except (TypeError, ValueError):
            fs = 0
        return (0 if _tripped(t[1], t[2]) else 1, -fs, t[0].name)

    out.sort(key=rank)
    return out


def _worst(states: list):
    """(breaker, corrupt) for the worst state across `states`: tripped wins, streak is the MAX (a sum would double count
    FAILs recorded once per spelling)."""
    if not states:
        return _fresh_breaker(), False
    worst = dict(states[0][1])
    streaks = []
    for x in states:
        try:
            streaks.append(int(x[1].get("fail_streak", 0) or 0))
        except (TypeError, ValueError):
            streaks.append(0)
    worst["fail_streak"] = max(streaks)
    worst["tripped"] = any(_tripped(x[1], x[2]) for x in states)
    return worst, any(x[2] for x in states)


def read_owner_failing_closed(d: Path):
    """(owner, was_corrupt). Missing file: unheld ({}). Unreadable file: held by an UNKNOWN owner."""
    data, why = _read_record(owner_path(d))
    if why is None:
        return (data if data is not None else {}), False
    msg = (f"lane={d.name} owner.json is CORRUPT ({why}); failing CLOSED (held by an UNKNOWN owner, "
           f"claims and releases refused) until a person inspects it or runs `lane-lock "
           f"force-release {d.name} --by <name> --reason <text>`. A safety stop, never a self-heal.")
    print(f"WARNING: {msg}", file=sys.stderr)
    append_alarm(msg)
    return {"_corrupt": True}, True


def _claim_refused(args, states: list) -> bool:
    """Print the REFUSED line and return True when ANY breaker dir of this lane's key is tripped (or corrupt)."""
    breaker, breaker_corrupt = _worst(states)
    if not breaker.get("tripped"):
        return False
    if len(states) > 1:
        print(f"NOTE lane key {lane_key(args.lane)} has breaker dirs {[x[0].name for x in states]}: the worst state binds")
    note = (" NOTE: breaker.json was CORRUPT on this read and is treated as tripped (fail "
            "closed); inspect it, then `reset` once the lane's real health is confirmed."
            if breaker_corrupt else "")
    print(f"REFUSED lane={args.lane}: breaker TRIPPED at {breaker.get('tripped_at')} "
          f"(fail_streak reached {breaker.get('fail_streak')}); run `lane-lock reset "
          f"{args.lane} --by <name> --reason <text>` after someone has actually read the "
          f"diff, not before.{note}")
    return True


def cmd_claim(args) -> int:
    if args.pid is None:
        print("lane-lock error: --pid is required for `claim`. Pass the pid of the long-lived caller "
              "(--pid $$ from a shell, or a build job's own pid). The CLI's own pid is already dead "
              "when the command returns, so omitting it would protect nothing.", file=sys.stderr)
        return 1
    if args.pid <= 0:
        print(f"lane-lock error: --pid must be a positive integer (got {args.pid}); pass the real pid "
              f"of the long-lived caller.", file=sys.stderr)
        return 1
    with _KeyLock(args.lane):     # read-only pre-check first: a refused claim must not leave an empty lane dir behind
        if _claim_refused(args, breaker_states(args.lane)):
            return 3
    d = lane_dir(args.lane)
    with _KeyLock(args.lane), _CritSection(d):
        if _claim_refused(args, breaker_states(args.lane)):    # authoritative, under both locks: worst state of every spelling
            return 3
        op, owner_corrupt = read_owner_failing_closed(d)
        if owner_corrupt:
            print(f"REFUSED lane={args.lane}: owner.json is CORRUPT, so the lane is held by an UNKNOWN "
                  f"owner. Run `lane-lock force-release {args.lane} --by <name> --reason <text>`, "
                  f"then claim again.")
            return 2
        holder_live = bool(op) and pid_is_same_process(op.get("pid"), op.get("start_key"))
        if op and op.get("owner") != args.owner and holder_live:
            print(f"REFUSED lane={args.lane}: held by '{op.get('owner')}' (pid {op.get('pid')}, claimed "
                  f"{op.get('claimed_at')}, reason: {op.get('reason', '')!r}), still alive")
            return 2
        record = {"lane": args.lane, "owner": args.owner, "pid": args.pid,
                  "start_key": proc_start_key(args.pid), "session": args.session or "",
                  "claimed_at": now_iso(), "reason": args.reason or ""}
        write_json_atomic(owner_path(d), record)
        healed = (" (previous holder's pid was gone or reused by a different process; self-healed)"
                  if op and not holder_live and op.get("owner") != args.owner else "")
        print(f"CLAIMED lane={args.lane} owner={args.owner} pid={record['pid']}{healed}")
        return 0


def cmd_release(args) -> int:
    d = lane_dir(args.lane)
    with _CritSection(d):
        op, owner_corrupt = read_owner_failing_closed(d)
        if owner_corrupt:
            print(f"REFUSED release lane={args.lane}: owner.json is CORRUPT, cannot verify you are the "
                  f"recorded owner. Run `lane-lock force-release {args.lane} --by <name> "
                  f"--reason <text>` instead.")
            return 2
        if not op:
            print(f"OK lane={args.lane}: already unheld")
            return 0
        if op.get("owner") != args.owner:
            print(f"REFUSED release lane={args.lane}: current owner is '{op.get('owner')}', not "
                  f"'{args.owner}'. A peer never releases another owner's claim (use `force-release` "
                  f"if the owner can never come back).")
            return 2
        owner_path(d).unlink(missing_ok=True)
        print(f"RELEASED lane={args.lane} by owner={args.owner}")
        return 0


def cmd_force_release(args) -> int:
    """Works even when owner.json cannot be read at all: that is its whole job. Always logged."""
    d = lane_dir(args.lane)
    with _CritSection(d):
        op, why = _read_record(owner_path(d))
        if op is None and why is None:
            print(f"OK lane={args.lane}: already unheld (force-release was a no-op)")
            return 0
        prior_owner = str(op.get("owner", "(unknown)")) if op else "(unknown, owner.json was corrupt)"
        prior_pid = op.get("pid", "(unknown)") if op else "(unknown)"
        owner_path(d).unlink(missing_ok=True)
    line = (f"lane={args.lane} FORCE-RELEASED by {args.by}: was held by '{prior_owner}' "
            f"(pid {prior_pid}), reason: {args.reason!r}")
    print(f"OK {line}")
    append_alarm(line)
    return 0


def cmd_status(args) -> int:
    d = lane_dir(args.lane, create=False)     # asking never creates a lane
    op, owner_corrupt = read_owner_failing_closed(d)
    states = breaker_states(args.lane)       # worst state across every spelling of this lane
    breaker, breaker_corrupt = _worst(states)
    if len(states) > 1 or (states and states[0][0] != d):
        print(f"lane={args.lane} breaker lane resolved to {[x[0].name for x in states]!r}")
    if owner_corrupt:
        print(f"lane={args.lane} owner=UNKNOWN (owner.json is CORRUPT, failing closed; run "
              f"`force-release {args.lane} --by <name> --reason <text>` to clear it)")
    elif op:
        if pid_is_same_process(op.get("pid"), op.get("start_key")):
            state = "alive"
        elif pid_alive(op.get("pid")):
            state = "DEAD (pid reused by a different process; will self-heal on next claim)"
        else:
            state = "DEAD (will self-heal on next claim)"
        print(f"lane={args.lane} owner={op.get('owner')} pid={op.get('pid')} ({state}) "
              f"claimed_at={op.get('claimed_at')} reason={op.get('reason', '')!r}")
    else:
        print(f"lane={args.lane} owner=(none, unheld)")
    if breaker.get("tripped"):
        note = " (breaker.json was CORRUPT, failing closed)" if breaker_corrupt else ""
        print(f"lane={args.lane} breaker=TRIPPED at {breaker.get('tripped_at')} "
              f"fail_streak={breaker.get('fail_streak')}{note}")
    else:
        print(f"lane={args.lane} breaker=closed fail_streak={breaker.get('fail_streak', 0)}")
    return 0


def cmd_list(args) -> int:
    r = root()
    lanes = sorted(p for p in r.iterdir() if p.is_dir() and p.name not in NON_LANE_DIRS) if r.is_dir() else []
    if not lanes:
        print("(no lanes yet)")
        return 0
    for d in lanes:
        cmd_status(argparse.Namespace(lane=d.name))
    return 0


def _history(breaker: dict, ticket: str, gate: str, result: str, detail: str) -> None:
    h = breaker.get("history")
    h = h if isinstance(h, list) else []
    h.append({"ts": now_iso(), "ticket": ticket, "gate": gate, "result": result, "detail": detail})
    breaker["history"] = h[-20:]          # bounded


def _fail_block_fields(rec: dict, verdict, tip) -> None:
    """Optional --verdict FILE / --tip SHA on a FAIL row. --tip stores the commit the gate judged (a fix-round brief can then
    be checked against it). --verdict stores block "ok" or "missing": a FAIL verdict file should carry a `VERDICT:` line and a
    `SAW:` line (what the gate actually looked at); saw_hash is a short hash of the SAW text. A missing or unreadable file
    NEVER stops the FAIL being recorded; any error leaves the row as it was."""
    try:
        if tip:
            rec["tip"] = str(tip).strip().lower()[:40]
        if not verdict:
            return
        import hashlib
        with open(verdict, errors="replace") as f:
            txt = f.read(200000)
        found = {}
        for m in re.finditer(r"^[\s>#*_|-]*(VERDICT|SAW)\s*\**\s*:\s*\**\s*(\S.*)$", txt, re.I | re.M):
            found.setdefault(m.group(1).upper(), m.group(2).strip())
        rec["block"] = "ok" if {"VERDICT", "SAW"} <= set(found) else "missing"
        if rec["block"] == "ok":
            rec["saw_hash"] = hashlib.sha1(" ".join(found["SAW"].lower().split()).encode()).hexdigest()[:12]
    except Exception:
        pass


def cmd_record_fail(args) -> int:
    with _KeyLock(args.lane):      # resolve INSIDE the key lock so spellings cannot split the streak
        states = breaker_states(args.lane)
        if states:
            d, breaker, _ = states[0]     # canonical: tripped, else highest streak, else name
        else:
            d = lane_dir(args.lane)
            breaker, _ = read_breaker_failing_closed(d)
        try:
            streak = int(breaker.get("fail_streak", 0))
        except (TypeError, ValueError):
            streak = 0
        breaker["fail_streak"] = streak + 1
        _history(breaker, args.ticket, args.gate, "FAIL", args.detail or "")
        _fail_block_fields(breaker["history"][-1], getattr(args, "verdict", None), getattr(args, "tip", None))
        just_tripped = False
        if breaker["fail_streak"] >= threshold() and not breaker.get("tripped"):
            breaker["tripped"], breaker["tripped_at"], just_tripped = True, now_iso(), True
        write_json_atomic(breaker_path(d), breaker)
    if just_tripped:
        msg = (f"lane={args.lane} BREAKER TRIPPED: {breaker['fail_streak']} consecutive {args.gate} "
               f"FAILs (ticket={args.ticket}, latest detail={args.detail!r}); further `lane-lock claim` "
               f"on this lane is refused until an explicit reset")
        print(f"TRIPPED {msg}")
        append_alarm(msg)
        return 3
    print(f"OK lane={args.lane}: recorded FAIL #{breaker['fail_streak']} (threshold {threshold()}) "
          f"for {args.gate} on ticket {args.ticket}")
    return 0


def cmd_record_pass(args) -> int:
    with _KeyLock(args.lane):      # a PASS reaches every breaker dir of the lane, so no sibling keeps a stale streak
        states = breaker_states(args.lane)
        if not states:
            d0 = lane_dir(args.lane)
            b0, c0 = read_breaker_failing_closed(d0)
            states = [(d0, b0, c0)]
        shown = None
        for d, breaker, corrupt in states:
            # A PASS recorded while the lane is TRIPPED must not launder the streak (record-pass is unauthenticated):
            # the row is kept but tagged while_tripped, fail_streak is NOT zeroed, and the reset guard ignores the row.
            was_tripped = corrupt or breaker.get("tripped") is True
            if not was_tripped:
                breaker["fail_streak"] = 0
            _history(breaker, args.ticket, args.gate, "PASS", args.detail or "")
            if was_tripped:
                breaker["history"][-1]["while_tripped"] = True
            write_json_atomic(breaker_path(d), breaker)
            if shown is None:
                shown = (was_tripped, breaker)
        was_tripped, breaker = shown
    if was_tripped:
        print(f"OK lane={args.lane}: PASS recorded for {args.gate} on ticket {args.ticket}; breaker still TRIPPED, "
              f"fail_streak kept at {breaker.get('fail_streak')} (a PASS while tripped clears nothing; "
              f"`reset` needs a judge ruling)")
        return 0
    print(f"OK lane={args.lane}: fail_streak reset to 0 for {args.gate} on ticket {args.ticket}")
    return 0


_RULING_WORD = r"(?<![A-Za-z0-9-])(?:CONTINUE|CUT|RESET|DROP|SHIP|HOLD)(?![A-Za-z0-9-])"
_RULING_LABEL = re.compile(r"^[\s>#*_|-]*(?:verdict|ruling|judge ruling)\s*\**\s*:\s*\**\s*\S", re.I)
_JUDGE_REASON = re.compile(r"(?<![A-Za-z0-9_./-])reports/judge/([A-Za-z0-9][A-Za-z0-9._-]*\.md)(?![A-Za-z0-9_./-])")
_TICKET_REASON = re.compile(r"(?<![A-Za-z0-9_./-])reports/([A-Za-z][A-Za-z0-9]*-\d+)/(judge-r\d+\.md)(?![A-Za-z0-9_./-])")
_LOOSE_JUDGE = re.compile(r"reports/(?:judge/|[A-Za-z][A-Za-z0-9]*-\d+/judge-r)\S*")
_TICKET_RX = re.compile(r"[A-Za-z][A-Za-z0-9]*-\d+")


def reports_root() -> Path:
    return Path(os.path.expanduser(os.environ.get("LANE_LOCK_REPORTS") or str(hubkit_home() / "reports")))


def _iso_epoch(v):
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _fails_since_last_pass(breaker: dict):
    """(count, failed tickets): FAIL rows after the last PASS row that was not recorded while tripped, or after the last
    reset that carried a judge ruling (ruled=True). A plain reset never anchors, so reset-then-fail-then-reset cannot
    launder the count. fail_streak is a floor: a hand-trimmed history cannot hide a streak."""
    n, tickets = 0, []
    for h in (breaker.get("history") or []):
        if not isinstance(h, dict):
            continue
        res = str(h.get("result", ""))
        if (res == "PASS" and h.get("while_tripped") is not True) or (h.get("gate") == "reset" and h.get("ruled") is True):
            n, tickets = 0, []
        elif res == "FAIL":
            n += 1
            tickets.append(str(h.get("ticket", "")))
    try:
        streak = int(breaker.get("fail_streak", 0))
    except (TypeError, ValueError):
        streak = 0
    return max(n, streak), tickets


def _ruling_text_ok(txt: str) -> bool:
    """A verdict or ruling line outside code fences (`Verdict: CONTINUE`, `Ruling: **RESET**`), or a heading or bold table
    cell carrying an upper-case ruling word. A decoy inside a fenced block does not count."""
    fence = False
    for line in str(txt or "").lstrip("﻿").replace("\r\n", "\n").split("\n"):
        if re.match(r"^\s{0,3}(`{3,}|~{3,})", line):
            fence = not fence
            continue
        if fence:
            continue
        if _RULING_LABEL.match(line):
            return True
        if re.match(r"^\s{0,3}#{1,6}\s", line) and re.search(_RULING_WORD, line):
            return True
        if re.match(r"^\s*\|.*\|\s*$", line) and re.search(r"\*\*[^*]*" + _RULING_WORD + r"[^*]*\*\*", line):
            return True
    return False


def _trip_floor(breaker: dict, d: Path, corrupt: bool):
    """The time a ruling must not predate: tripped_at when tripped, else the newest FAIL row's ts. A corrupt breaker.json
    has no trustworthy times, so its own mtime is the floor. None = cannot be established, which fails CLOSED.
    mtime is forgeable (`touch`): this stops stale or unrelated rulings, not a determined caller."""
    if corrupt:
        try:
            return os.stat(breaker_path(d)).st_mtime
        except OSError:
            return None
    t = _iso_epoch(breaker.get("tripped_at")) if breaker.get("tripped") is True else None
    if t is None:
        fails = [h for h in (breaker.get("history") or []) if isinstance(h, dict) and h.get("result") == "FAIL"]
        t = _iso_epoch(fails[-1].get("ts")) if fails else None
    if t is None:
        try:
            t = os.stat(breaker_path(d)).st_mtime
        except OSError:
            return None
    return t


def _token_rx(nm: str):
    """The LANE name as a WHOLE token: not glued to a name character on the left (`deploy-main`, `a.main`, `x/main`) and not
    continued on the right (`main-2`, `main.v2`, `main/x`). Trailing sentence punctuation (`main.`, `main,`) still ends it.
    A name may hold a colon (`project:L-7`); the left boundary then also refuses a glued prefix such as `x:project:L-7`."""
    return re.compile(r"(?<![A-Za-z0-9_.:/@+-])" + re.escape(nm) + r"(?![A-Za-z0-9_@+/-])(?![.:][A-Za-z0-9_])", re.I)


def _ticket_rx(nm: str):
    """A failed ticket: only a name character (letter, digit, _ -) glued to it disqualifies it. A path (`reports/L-1/x.md`),
    `ticket:L-1`, `L-1/L-2`, `proj:L-1:` and `@L-1` all still name it (the base rule)."""
    return re.compile(r"(?<![A-Za-z0-9_-])" + re.escape(nm) + r"(?![A-Za-z0-9_-])", re.I)


_LANE_MARK = re.compile(r"\blanes?\b", re.I)
_LANE_JUNK = re.compile(r"[\s|:=*_`()\[\]\"'>]*")        # between the word lane and the name: `Lane: **`main`**`, `| Lane | main |`
_LANE_SEP = re.compile(r"\s*(?:,|&|\band\b|\bor\b)\s*", re.I)  # `Lanes: deploy, main, hub`
_LANE_ITEM = re.compile(r"[^\s,&|]+")


def _lane_marker_names(ln: str, rx) -> bool:
    """True when the lane token sits right after a `lane`/`lanes` marker (`lane: X`, `lane X`, `lane=X`, `Lane | X`), or in
    a comma/and list that starts there. `in the fast lane the main branch` does not bind (`the` follows the marker)."""
    for m in _LANE_MARK.finditer(ln):
        pos = _LANE_JUNK.match(ln, m.end()).end()
        for _ in range(12):
            if rx.match(ln[pos:]):   # the structure (marker, then junk or a list separator) is the left boundary here
                return True
            it = _LANE_ITEM.match(ln, pos)
            sep = _LANE_SEP.match(ln, it.end()) if it else None
            if not sep:
                break
            pos = sep.end()
    return False


def _unitalic(c: str) -> str:
    """`_main_` / `__main__` is markup around the name; a lone `_main` or `main_` is part of it (lane names may hold `_`)."""
    # linear: a regex with a backreference backtracks cubically on a long underscore run
    n = len(c)
    k = min(n - len(c.lstrip("_")), n - len(c.rstrip("_")), (n - 1) // 2)
    return c[k:n - k] if k >= 1 else c


def _lane_table_names(lines, rx) -> bool:
    """A markdown table whose header holds a `Lane` cell: the lane counts in that column of its data rows."""
    col = None
    for ln in lines:
        t = ln.strip()
        if not t.startswith("|"):
            col = None
            continue
        cells = [_unitalic(re.sub(r"[*`\s]", "", c)) for c in t.strip("|").split("|")]
        if col is None:
            for i, c in enumerate(cells):
                if re.fullmatch(r"lanes?", c, re.I):
                    col = i
            continue
        if re.fullmatch(r"[-:|\s]*", t):
            continue
        if col < len(cells) and rx.search(cells[col]):
            return True
    return False


def _names_lane_or_ticket(txt: str, names, lane: str = "") -> bool:
    """A failed ticket counts as named anywhere (the base rule). The LANE counts only as a whole token next to a lane marker
    (`Lane: main`, `lane main`, `lane=main`, `Lane | main`, a list after one) or in the Lane column of a table, so a lane called
    with a common word is not named by prose such as `the main branch` or `in the fast lane the main branch is fine`."""
    txt = str(txt or "").lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    lines = txt.split("\n")
    for nm in names:
        if not nm:
            continue
        if nm == lane:
            rx = _token_rx(nm)
            if any(_lane_marker_names(ln, rx) for ln in lines) or _lane_table_names(lines, rx):
                return True
        elif _ticket_rx(nm).search(txt):
            return True
    return False


def _judge_evidence(tickets, reason: str, lane: str, floor):
    """(ok, why or the cited file). A reset after 2+ FAILs needs a judge ruling CITED in --reason (no implicit discovery) as
    reports/judge/<name>.md or reports/<failed ticket>/judge-r<N>.md, under $LANE_LOCK_REPORTS. The file must be a real file
    inside that folder (symlinks resolved, a symlinked subfolder pointing elsewhere refused), hold a verdict or ruling line,
    NAME the lane or one of the failed tickets, and have an mtime >= the trip time. Binding is by content and mtime only;
    both are forgeable by whoever can write the reports folder, so this is friction plus an audit trail (the reset row
    records ruled=True). Never raises: any error means no evidence."""
    try:
        reason = reason or ""
        if re.search(r"(^|[\\/\s])\.\.([\\/\s]|$)", reason) or "\x00" in reason:
            return False, "--reason contains a path traversal (..)"
        if floor is None:
            return False, "cannot establish when the lane tripped: failing closed"
        names = [t for t in sorted(set(tickets)) if _TICKET_RX.fullmatch(t)] + [lane]
        rroot = os.path.realpath(reports_root())
        cands = []  # (display, path, folder that must hold it)
        for m in _JUDGE_REASON.finditer(reason):
            cands.append((f"reports/judge/{m.group(1)}", os.path.join(rroot, "judge", m.group(1)), os.path.join(rroot, "judge")))
        for m in _TICKET_REASON.finditer(reason):
            if m.group(1) in names:
                cands.append((f"reports/{m.group(1)}/{m.group(2)}", os.path.join(rroot, m.group(1), m.group(2)), os.path.join(rroot, m.group(1))))
        if not cands:
            hint = ""
            if _LOOSE_JUDGE.search(reason):
                hint = (" The citation looks malformed: the file name must end in .md and be followed by a space or the end of "
                        "--reason; trailing punctuation such as a period right after the name (reports/judge/x.md.) is refused.")
            return False, ("no judge file cited: --reason must name reports/judge/<name>.md or "
                           "reports/<failed ticket>/judge-r<N>.md (no implicit discovery)." + hint)
        why = "no cited ruling qualified"
        for disp, path, cdir in cands:
            f, base = os.path.realpath(path), os.path.realpath(cdir)
            if os.path.dirname(base) != rroot or os.path.dirname(f) != base or not os.path.isfile(f):
                why = f"{disp} does not exist as a regular file inside its folder"
                continue
            with open(f, "r", errors="replace") as fh:
                txt = fh.read(200000)
            if not _ruling_text_ok(txt):
                why = f"{disp} holds no verdict or ruling line"
                continue
            if not _names_lane_or_ticket(txt, names, lane):
                why = f"{disp} does not name this lane ({lane}) or its failed ticket(s)"
                continue
            if os.stat(f).st_mtime < floor:
                why = f"{disp} is older than the trip: it cannot be a ruling on this trip"
                continue
            return True, disp
        return False, why
    except Exception as e:  # noqa: BLE001
        return False, f"judge evidence check failed closed ({type(e).__name__})"


def _reset_plan(args, d: Path, breaker: dict, corrupt: bool) -> dict:
    """Guard for ONE breaker dir. Returns {"refused": why, "fails_n": n} or the plan."""
    was_tripped = breaker.get("tripped") is True
    fails_n, fail_tickets = _fails_since_last_pass(breaker)
    if corrupt or not isinstance(breaker.get("tripped", False), bool):
        fails_n = max(fails_n, 2)   # corrupt file or odd-typed `tripped` ("false", 1): fail CLOSED, a judge is required
    if was_tripped:
        fails_n = max(fails_n, 2)   # a tripped lane is past the threshold even if the history window rolled the FAILs off
    ruled = False
    if fails_n >= 2:
        ok_ev, why = _judge_evidence(fail_tickets, args.reason or "", args.lane, _trip_floor(breaker, d, corrupt))
        if args.by not in ("judge", "hub") or not ok_ev:
            return {"refused": f"--by {args.by!r} is not judge or hub" if args.by not in ("judge", "hub") else why,
                    "fails_n": fails_n}
        ruled = True
    return {"d": d, "breaker": breaker, "was_tripped": was_tripped, "ruled": ruled}


def cmd_reset(args) -> int:
    with _KeyLock(args.lane):      # reset clears EVERY breaker dir of the lane, or none (all guards first, then writes)
        states = breaker_states(args.lane)
        if not states:
            d0 = lane_dir(args.lane)
            b0, c0 = read_breaker_failing_closed(d0)
            states = [(d0, b0, c0)]
        plans = []
        for d, breaker, corrupt in states:
            plan = _reset_plan(args, d, breaker, corrupt)
            if "refused" in plan:
                print(f"REFUSED lane={args.lane}: {plan['fails_n']} FAILs since the last PASS; a tripped breaker stays tripped until a "
                      f"judge rules. Reset needs --by judge|hub AND a judge ruling on file: {plan['refused']}. Cite it in --reason: "
                      f"'reports/judge/<file>.md' or 'reports/<ticket>/judge-r<N>.md'; the file must name this lane or its "
                      f"ticket and be newer than the trip.", file=sys.stderr)
                append_alarm(f"lane={args.lane} reset REFUSED for {args.by!r} after {plan['fails_n']} FAILs: {plan['refused']}")
                return 2
            plans.append(plan)
        for pl in plans:
            breaker = pl["breaker"]
            breaker["tripped"], breaker["tripped_at"], breaker["fail_streak"] = False, None, 0
            _history(breaker, "", "reset", f"RESET by {args.by}", args.reason or "")
            if pl["ruled"]:
                breaker["history"][-1]["ruled"] = True
            write_json_atomic(breaker_path(pl["d"]), breaker)
    was_tripped = any(pl["was_tripped"] for pl in plans)
    if was_tripped:
        line = f"lane={args.lane} breaker RESET by {args.by}: {args.reason!r}"
        if len(plans) > 1:
            line += f" (cleared {len(plans)} dirs: {[pl['d'].name for pl in plans]})"
        print(f"OK {line}")
        append_alarm(line)
    else:
        print(f"OK lane={args.lane}: breaker was not tripped, fail_streak cleared anyway")
    return 0


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse exits 2, which here means "held by another live owner". Usage errors are 1.
        self.print_usage(sys.stderr)
        print(f"lane-lock error: {message}", file=sys.stderr)
        sys.exit(1)


def build_parser() -> argparse.ArgumentParser:
    ap = _Parser(prog="lane-lock")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def lane_cmd(name, func):
        p = sub.add_parser(name)
        p.add_argument("lane")
        p.set_defaults(func=func)
        return p

    p = lane_cmd("claim", cmd_claim)
    p.add_argument("--owner", required=True)
    # Not argparse-required: that would exit 2, which here means "held by another live owner".
    p.add_argument("--pid", type=int, default=None)
    p.add_argument("--session", default="")
    p.add_argument("--reason", default="")
    lane_cmd("release", cmd_release).add_argument("--owner", required=True)
    p = lane_cmd("force-release", cmd_force_release)
    p.add_argument("--by", required=True)
    p.add_argument("--reason", required=True)
    lane_cmd("status", cmd_status)
    sub.add_parser("list").set_defaults(func=cmd_list)
    for name, func in (("record-fail", cmd_record_fail), ("record-pass", cmd_record_pass)):
        p = lane_cmd(name, func)
        p.add_argument("--ticket", required=True)
        p.add_argument("--gate", required=True, choices=GATES)
        p.add_argument("--detail", default="")
        if name == "record-fail":
            p.add_argument("--verdict", default=None, help="the FAIL verdict file; stores block ok|missing and saw_hash")
            p.add_argument("--tip", default=None, help="commit sha the gate judged")
    p = lane_cmd("reset", cmd_reset)
    p.add_argument("--by", required=True)
    p.add_argument("--reason", required=True)
    return ap


def main() -> int:
    if sys.platform == "win32":   # before parsing or touching any state: nothing below may run on native Windows
        print(WINDOWS_MSG, file=sys.stderr)
        return 1
    args = build_parser().parse_args()
    lane = getattr(args, "lane", None)
    if lane is not None and not LANE_RX.fullmatch(lane):
        print(f"lane-lock error: bad lane name {lane!r} (letters, digits, . _ - only, up to 64, "
              f"starting with a letter or digit)", file=sys.stderr)
        return 1
    try:
        return args.func(args)
    except Exception as e:  # never crash unlabeled
        print(f"lane-lock error: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
