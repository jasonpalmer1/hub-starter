# doctor

"My hub isn't working" with no idea why.

## What it fixes

Most hub trouble is quiet: a hook that was copied but never wired in, a hook
pointing at a file that moved, a scheduled job that stopped last Tuesday, a
ledger file with one broken line. Nothing errors. Things just do not happen.

`doctor` looks at all of it in one run and tells you, line by line, what is
fine and what is not, with the exact fix under each problem.

It is read-only. It looks at files and creates nothing, not even the install folder, so
running it twice on a machine with nothing installed says `FAIL home` both times. The one
thing it writes is a single line added to `~/.claude/hubkit/state/events.log` saying it ran,
and only when that folder already exists (a local note, never sent anywhere; `--no-log`
turns even that off).

## What you need

- python3, 3.9 or newer
- macOS or Linux
- nothing else; no other pattern is required. It checks whichever patterns you
  have installed and skips the rest.

## Install

By hand:

    mkdir -p ~/.claude/hubkit/doctor
    cp doctor.py ~/.claude/hubkit/doctor/

## Use it

    python3 ~/.claude/hubkit/doctor/doctor.py

Sample output:

    hubkit doctor
    PASS  python: Python 3.11.3
    PASS  home: install folder ~/.claude/hubkit
    PASS  hooks-enabled: hooks are not switched off
    FAIL  hook report-gate.py: installed but not wired into settings (it will never run)
          fix: add to the "hooks" section of ~/.claude/settings.json: {...}
    PASS  ledger: 14 event(s), every line parses
    FAIL  job nightly-backup: last beat 31.0h ago, limit 25.0h: the job has stopped or is failing
          fix: run the job by hand, read its own log, and check the scheduler entry is loaded
    summary: 9 pass, 2 fail, 0 warn, 1 skip

The exit code is 0 when nothing FAILED and 1 when something did. WARN means
look soon; SKIP means that part is not set up (not a problem).

A job's allowed gap comes from its own last beat, or failing that from the limit
`deadman beat --max-age` remembered for it, the same order `deadman check` uses. A job
with no gap known anywhere is a SKIP, not a PASS.

## Getting help

    python3 ~/.claude/hubkit/doctor/doctor.py --paste

Prints the same report with your home folder shown as `~`, your user name
replaced by `<user>`, and any other folder path reduced to `<path>/name`. It is
meant for pasting into a chat or an email. Read it once before you send it.

## What it checks

| check | fails when |
|---|---|
| python | older than 3.9 |
| home | the install folder is missing or not writable |
| hooks-enabled | settings switch all hooks off |
| hook NAME | an installed hook script is not in settings, is under the wrong event, or points at a file that is not there |
| agent | the capped builder is missing, lacks the cap, or (WARN) also exists in a project folder |
| ledger | any line of the ledger is not valid JSON |
| job NAME | a deadman heartbeat is too old, corrupt, or dated in the future |
| alarms | (WARN) a deadman alarm in the last 24 hours |
| disk | under 5 GiB free (WARN under 20); same settings as disk-guard |
| loop-tick | (only with `work-loop` installed and a lane registered) the tick is over 30 minutes old, corrupt or dated in the future; WARN when it never ran |
| loop-lanes | `state/lanes.json` cannot be read |
| loop-feed | never fails: shows how long ago a ticket was last fed to a lane |
| stop-arm | a `Stop` entry names `stop_arm.py` but the file is not there (FAIL); WARN when no lane is registered. No row if you did not wire it |
| failure-register | (WARN, only with `failure-register` installed) failures are recorded but the register never ran or last ran over 2 hours ago |

Doctor only reads these files; the loop rows exist so a quiet loop is noticed: a lane that is idle because nothing is
wrong looks different from a tick that stopped.

Only your user settings file is read (`~/.claude/settings.json`). If your hooks
live elsewhere, point it there: `--settings /path/to/settings.json`. It does not
look at project-level settings.

## Not checked

Whether a scheduler entry is loaded (it can only see that the job stopped
beating), Windows, and anything inside a hook script itself.

## Test it

    bash test.sh            # 67 tests in a scratch HOME (about 5 seconds)
    bash test.sh --mutate   # also breaks each guard and checks a test fails

Every test breaks exactly one thing in a healthy scratch hub and checks that
exactly that line changes.

## Lift just this piece

Copy `doctor.py`. Nothing else in this repo is needed.
