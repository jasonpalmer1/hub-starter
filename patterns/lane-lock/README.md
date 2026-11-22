# lane-lock

Two workers in one repo, and a ship that keeps being retried after it failed.

## What it fixes

Two things go wrong when several workers share a machine.

1. Two of them start in the same repo at once (two builds in one folder) because
   nothing on disk said the repo was taken. Reading a note is not a lock.
2. A worker keeps retrying a deploy into a lane whose last two gate checks both
   failed. Each gate report says FAIL, but nothing stops the next worker.

`lane-lock` is a named claim per lane, plus a breaker that trips after repeated
FAILs and stays tripped until a person resets it. A lane is any name you pick:
a repo, a ticket scope, a site.

## What you need

- python3, 3.9 or newer
- macOS or Linux (it checks process ids with `ps`)
- nothing else; no other pattern is required

## Install

By hand:

    mkdir -p ~/.claude/hubkit/lane-lock
    cp lane-lock.py ~/.claude/hubkit/lane-lock/

There is no hook to wire. Workers and scripts call it. Everything it stores lives
under `~/.claude/hubkit/state/lanes`.

## Use it

Take a lane before you build (pass the pid of the long-lived caller, `$$` from a shell):

    python3 ~/.claude/hubkit/lane-lock/lane-lock.py claim my-repo --owner builder-7 --pid $$ --reason "L-0001 build"

If someone else holds it you get `REFUSED ... held by 'builder-5' ... still alive` and
exit code 2. Give it back when you finish:

    python3 ~/.claude/hubkit/lane-lock/lane-lock.py release my-repo --owner builder-7

Report each gate result (a gate is a bug check or a playtest). `--gate` takes exactly one of
`bug-gate`, `playtest` or `other`, anything else is a usage error:

    lane-lock.py record-fail my-repo --ticket L-0001 --gate bug-gate --detail "login breaks"
    lane-lock.py record-pass my-repo --ticket L-0001 --gate bug-gate

Two FAILs in a row (set `LANE_BREAKER_THRESHOLD` to change that) trip the breaker.
From then on every `claim` on that lane is refused with exit code 3 until:

    lane-lock.py reset my-repo --by judge --reason "see reports/judge/my-repo-ruling.md"

A later PASS does not reopen the lane (it is recorded but the FAIL count is kept). A pass
report is not a person reading the diff, so only `reset` does, and it writes who and why
to the alarms log. Resetting a tripped lane needs `--by judge` or `--by hub` AND a ruling
file cited in `--reason`, as `reports/judge/<name>.md` or `reports/<ticket>/judge-r<N>.md`
under `$LANE_LOCK_REPORTS` (default `$HUBKIT_HOME/reports`). The file must be a real file
(a symlink out of the folder is refused), carry a `Verdict:` or `Ruling:` line, name the lane
or a failed ticket, and be newer than the trip. The lane name must appear as a whole word on a line that also says "lane" (`Lane: main`), so a ruling for `deploy-main` or prose about "the main branch" does not name lane `main`; CRLF and a leading BOM are tolerated. A refused reset prints REFUSED, exits 2 and
writes an alarm. A lane that never tripped can be reset by anyone. Name and file date can
both be faked by someone who can write the reports folder, so this is friction and an audit
trail, not a signature.

`status my-repo` and `list` show who holds what and whether the breaker is tripped.
They never create a lane.

## Exit codes

| code | meaning |
|------|---------|
| 0 | ok |
| 1 | usage or internal error (missing --pid, bad lane name, a stuck lock) |
| 2 | held by another live owner, or the owner record is unreadable |
| 3 | breaker tripped (also a breaker record that cannot be read) |

On native Windows (not WSL) every command exits 1 with a message pointing to `docs/WINDOWS.md`,
before it reads or writes anything. There, `os.kill(pid, 0)` is not a liveness check and can end
the process, so the tool refuses instead of guessing. Under WSL2 it runs as on Linux.

## Dead holders and stuck lanes

- A holder whose process is gone is cleared on the next claim ("self-healed").
- A pid that is alive but started at a different time than when the claim was made
  is a different process that reused the number. That also counts as gone.
- If a claim can never be released by its owner, `force-release my-repo --by you
  --reason "owner crashed"` clears it. It is logged.
- A record that exists but cannot be read (cut off, not text) fails closed: the
  lane counts as held by an unknown owner, or as tripped. It is never treated as
  free. `force-release` (for owner.json) or `reset` (for breaker.json) recovers.

## Test it

    bash test.sh            # 37 tests in a scratch HOME (about 10 seconds)
    bash test.sh --mutate   # also breaks each guard and checks a test fails

## Lift just this piece

Copy `lane-lock.py`. Nothing else in this repo is needed.
