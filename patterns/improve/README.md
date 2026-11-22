# improve

When the queue is empty, the lazy move is to invent work. `improve` refuses that. It looks only at things that are
**measured** to be wrong, turns at most a few of them into tickets, and closes a ticket only when the same
measurement, taken again by this tool, shows it fixed. If nothing measured is failing it says so and stops:

    IDLE: nothing measured is failing

## Use

Run it from a schedule, or from a lane that has just run out of tickets:

    python3 improve.py mine              # queue empty? read the sources, admit a few candidates
    python3 improve.py mine --dry-run    # show what it would do; writes nothing
    python3 improve.py status            # open N/5 | today N/3
    python3 improve.py close im-1a2b3c4d # re-reads the evidence; closes only if it shows a fix
    python3 improve.py close im-1a2b3c4d --by-design "expected on weekends, jobs are paused"
    python3 improve.py ask L-0007        # an operator-asked ticket: always allowed, no caps
    python3 improve.py propose --source failure --key rf-26ba3b   # the same gate for one named signal
    python3 bug_sweep.py run             # smoke-test merged work nobody has swept yet (see Bug sweep below)

`mine` runs only when the ledger holds no open or active ticket other than the ones `improve` made itself. An
operator-asked ticket in the queue means the operator's work goes first and `mine` exits with code 4.

## Where candidates come from (all read from `$HUBKIT_HOME/state`)

| source | evidence file | it fires when |
|---|---|---|
| `failure` | `failure-register.md` (from the `failure-register` pattern) | a signature failed in 3 or more incidents, no CAUSE typed, evidence under 7 days old |
| `target` | `targets.json`, naming number files | a number file misses its threshold |
| `bugsweep` | `bugsweep/NAME.txt`, written by `bug_sweep.py` | the last smoke test of a listed project failed (line 1 is `1`), evidence under 7 days old |

`targets.json` is written by you, never by a worker:

    {"targets": [{"name": "build-secs", "file": "state/build-secs.txt", "op": "max", "threshold": 300}]}

`op` is `max` (the number must stay at or under the threshold) or `min`. The number file is any file under
`$HUBKIT_HOME` whose first word is a number; your own cron job rewrites it. Its modification time is its age.

## What it refuses (each refusal names its reason)

| reason | meaning |
|---|---|
| `no-evidence` | no file, no such signature, or no number to read |
| `stale-evidence` | the evidence is over 7 days old, or stamped in the future |
| `evidence-outside-home` | the path leaves `$HUBKIT_HOME` (a `..` or a symlink counts) |
| `below-threshold` | fewer than 3 incidents, or the target is met |
| `has-cause` | a person already typed a CAUSE, or marked it by-design |
| `duplicate` | already an open candidate for this signature (the id is a hash of source and key) |
| `flapping` | the signal crossed the threshold 3 times in its last 6 readings: decide it by hand |
| `daily-cap` | 3 new candidates already today |
| `open-cap` | 5 candidates already open |
| `evidence-injection` | log-derived text looks like an instruction, or is not plain job-name text |

A signature that was closed and then fails again after the closing time **reopens** as a new ticket (the caps still apply).

## Closing needs a measurement

`close` has no way to type a result in. It re-reads the evidence itself and compares it with the reading stored when
the ticket was made. It refuses `no-before-reading`, `evidence-not-refreshed` (the evidence file is not newer than the
ticket), `still-failing` (a newer failure, or still listed below threshold), `no-pass-since` (no success after the last
failure), or, for a target, a number that still misses. A free-text note changes nothing: "looked into it" is refused.
On success the ticket gets a `measure` proof ("before 3 after 0": incidents counted then, new failures since) and is marked done.

`--by-design "why"` closes a failure candidate without a fix, with a reason of at least 10 characters. At most 5 per 30
days; it is not available for targets (change the target instead).

## Bug sweep: `bug_sweep.py`

The bug check for an empty queue. You list your projects once, in `$HUBKIT_HOME/state/projects.json`:

    {"projects": [{"name": "app", "path": "/abs/path/to/repo", "smoke": ["python3", "-m", "unittest"], "timeout": 60}]}

`smoke` is an argument list, run directly: never through a shell, never read from the repo, killed (whole process
group) when `timeout` seconds pass. `bug_sweep.py run` then sweeps each listed project that has commits no passing
sweep has covered yet, and writes `state/bugsweep/NAME.txt`: `0` when the smoke test passed, `1` plus the last 4000
characters of its output (tokens removed) when it failed. `improve.py mine` turns a `1` into one candidate whose note
points at that file and whose record carries the output; a later passing sweep, then `improve.py close`, closes it.

| it refuses | reason printed |
|---|---|
| any open ticket that is not an improve candidate | `queue not empty (N)`, exit 4 |
| a project not in `projects.json`, or a name with odd characters | `unlisted-project`, `bad-name` |
| commits already covered by a passing sweep (a rewritten history is swept again for its new commits) | `SKIP NAME range-covered` |
| a second sweep at the same time (one lock; the queue, config and coverage are read after taking it) | `sweep-running`, exit 4 |
| a detached HEAD, a string command, a relative path, a bad timeout, an unreadable `projects.json` | `detached-head`, `bad-command`, `bad-path`, `bad-timeout`, `projects-unreadable` |

A failing sweep does not mark its commits covered, so it runs again until it passes. A project that never swept looks
at its last 20 commits. `bug_sweep.py run --dry-run` shows what would run and writes nothing; `projects` validates the file.
When every source is quiet, `mine` prints the idle line and records a `loop.idle_declared` event.

## What it never does

- Never copies log text into a title or note. Titles come from a template that holds only the signature id and a number.
- `improve.py` never runs a command: its sources are files. The only command that ever runs is the smoke command you
  wrote in `projects.json`, and only `bug_sweep.py` runs it, as an argument list with no shell.
- Never reads outside `$HUBKIT_HOME`, never touches the network, never spends anything.
- Never creates an operator-asked ticket. Its tickets are always `asked_by: hub`; `ask` only recognises tickets that
  already exist in the ledger as operator-asked.

## Limits

- A smoke command that detaches itself into a new session escapes the timeout kill (the group kill covers normal children).
- The sweep runs the command in the project folder as it is on disk, not in a clean checkout; keep it free of local edits.
- Coverage is this tool's own log. It does not read gate verdict files.

- A ticket closed directly with `ledger status done` (not through `improve close`) frees its slot but carries no
  before/after reading. `status` does not flag these.
- The `failure` source needs the register to be regenerated (run `failure_register.py` on a schedule) before a close.

## Files it writes (all in `$HUBKIT_HOME/state`)

`improve-candidates.jsonl` (proposed and closed events), `improve-readings.jsonl` (one line per signal per run, for the
flapping check), `improve.tick` (heartbeat: written on every completed `mine`, so `deadman` can watch it),
`events.jsonl` (`improve.candidate`, `improve.refused`, `improve.idle`, `loop.idle_declared`, `improve.closed_measured`,
`bugsweep.run`), `improve.lock`. The sweep adds `bugsweep/NAME.txt`, `bugsweep-covered.jsonl`, `bug-sweep.tick`, `bug-sweep.lock`.

Python 3.9, standard library only. macOS or Linux.
