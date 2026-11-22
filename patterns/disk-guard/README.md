# disk-guard

A full disk that fails with no warning.

## What it fixes

When a disk fills up, builds and file writes fail halfway through and leave
broken half-finished output. The usual first sign is the failure itself. This
gives you a plain verdict before you start work, so a build is held back while
there is still room to clean up.

It only looks. It deletes nothing, kills nothing, and writes nothing except one
line in its own log.

## What you need

- python3, 3.9 or newer
- macOS or Linux
- nothing else; no other pattern is required

## Install

By hand:

    mkdir -p ~/.claude/hubkit/disk-guard
    cp disk-guard.py ~/.claude/hubkit/disk-guard/

There is no hook to wire. Run it by hand, or put it in front of builds.

## Use it

    python3 ~/.claude/hubkit/disk-guard/disk-guard.py

It prints one line and sets an exit code:

    disk-guard: 27Gi free - OK (builds allowed) - build tools running: 3

| exit | word | meaning |
|---|---|---|
| 0 | OK | plenty of room |
| 1 | WARN | getting low: one build at a time |
| 2 | CRIT | too low, or could not be read: no new builds |

In front of a build:

    python3 ~/.claude/hubkit/disk-guard/disk-guard.py
    [ $? -eq 2 ] && { echo "disk too full, not building"; exit 1; }

An unreadable disk counts as CRIT. A check that cannot see should never say
"fine".

## Settings

- `DISK_GUARD_WARN_GI=40` WARN below 40 GiB free (default 20).
- `DISK_GUARD_CRIT_GI=15` CRIT below 15 GiB free (default 5). It cannot be
  higher than the warn number; a bad number is an error (exit 2), not a pass.
- `--path DIR` (or `DISK_GUARD_PATH`) checks the disk that holds DIR.
  Default: the disk holding `~/.claude/hubkit`, else your home folder.

Pick a warn number you would actually want a nudge at. A good rule is the
amount of room you need to keep open at all times; the warning is then your
signal to clear space.

## The build count

The line also says how many build tools (npm, yarn, pnpm, vite, webpack, turbo,
next, cargo, gradle, make) are running. It is information for whoever calls it,
for example "WARN and two builds already running, wait". It never changes the
exit code.

## Test it

    bash test.sh            # 15 tests in a scratch HOME (about 2 seconds)
    bash test.sh --mutate   # also breaks each guard and checks a test fails

## Lift just this piece

Copy `disk-guard.py`. Nothing else in this repo is needed.
