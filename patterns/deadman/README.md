# deadman

Scheduled jobs that stop and tell nobody.

## What it fixes

A job that crashes, gets unloaded, or never started makes no error. It makes
silence, and silence is easy to miss for days. So this works the other way
round: each job proves it is alive after every successful run, and a separate
check raises an alarm when the proof gets too old.

Alarm on "no success lately", not on errors. A job that fails loudly is the
easy case; the dangerous one is the job that is simply gone.

## What you need

- python3, 3.9 or newer
- macOS or Linux
- nothing else; no other pattern is required

## Install

By hand:

    mkdir -p ~/.claude/hubkit/deadman
    cp deadman.py ~/.claude/hubkit/deadman/

There is no hook to wire. You call it from your scheduled jobs (see
`patterns/schedule-examples` for launchd and cron files).

## Use it

At the end of a job, only if it worked:

    python3 ~/.claude/hubkit/deadman/deadman.py beat nightly-backup --max-age 25h

`--max-age` says how long a gap is normal. Give it a little more than the
schedule (a daily job gets 25h, not 24h).

From a separate scheduler entry, every 15 minutes or so:

    python3 ~/.claude/hubkit/deadman/deadman.py check --all

Prints one line per job and exits 1 if anything is stale. To see everything:

    python3 ~/.claude/hubkit/deadman/deadman.py list

Run the check from a different scheduler entry than the jobs it watches. If the
check lives inside the job, a dead job cannot report itself.

## What counts as stale

All of these alarm, nothing is silent:

- the last beat is older than the limit
- there is no beat at all (never ran, or the file was deleted). `beat --max-age` registers the job,
  so `check --all` alarms for a registered job whose beat file is gone
- the beat file is corrupt, empty or not text. That job alarms and every other job is still checked
- `check --all` has nothing to check (the heartbeats folder is missing, empty or unreadable and no job
  is registered). That alarms too, under the name `deadman-watcher`, and runs your notify command
- the beat is dated in the future (a clock problem)
- a name you check has no known limit (pass `--max-age`)

A job counts as fresh up to and including its limit.

## Order matters when you set it up

Beat each job once by hand, with `--max-age`, before you schedule the check.
Otherwise a brand-new job looks "missing" and alarms on day one. A job that
has never beaten is invisible to `check --all`; check it by name
(`check NAME --max-age 25h`) if you want a never-started job to alarm.

## Switching a job off on purpose

A job that is registered and then removed (its schedule deleted, the project retired) alarms
forever, because a missing beat is exactly what `deadman` is built to notice. Tell it the job
is gone:

    python3 ~/.claude/hubkit/deadman/deadman.py retire nightly-backup

That deletes the job's beat, its registered limit and its alarm stamp. It exits 1 and says so if
no job has that name. Every STALE line also ends with this command, so the fix is on screen
when the alarm arrives. To bring the job back, beat it with `--max-age` again.

## The alarm

An alarm is one line in `~/.claude/hubkit/state/alarms.log`. If you set
`HUBKIT_NOTIFY_CMD`, it is also run as `CMD "<title>" "<reason>"`, for example a
script that sends you a message. The same job alarms at most once every 2
hours (`--realarm 6h` changes that), so a dead job does not flood you. A job
that recovers and fails again alarms immediately. A broken notify command is
reported on screen and never hides the alarm.

Each alarm and each stamp stays inside `~/.claude/hubkit`. Nothing is sent
anywhere unless your own notify command does it.

## Test it

    bash test.sh            # 50 tests in a scratch HOME (about 10 seconds)
    bash test.sh --mutate   # also breaks each guard and checks a test fails

## Lift just this piece

Copy `deadman.py`. Nothing else in this repo is needed.
