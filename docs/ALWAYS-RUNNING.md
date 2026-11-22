# Keep the work going while you are away

A scheduled loop can hand the next ticket to an idle chat and stop, with one line, when nothing measured is
wrong. Honest scope: it needs a machine that stays awake (cron and launchd do not run on a sleeping laptop), and
a chat that reads its inbox file or the optional Stop arm. On its own it writes a file; it does not type into a chat.

Two loops do this. Everything is a small script you schedule; nothing runs unless you start it.

- **Feed loop**: when a chat (a "lane" here: one long-lived chat that works one area) has been idle for 10
  minutes and a ticket is free, the ticket is handed to it. Two lanes never get the same ticket.
- **Improve loop**: when no ticket is left anywhere, look only at things that are *measured* to be wrong
  (a job that keeps failing, a smoke test that fails). If nothing is, say `IDLE: nothing measured is failing`
  and stop. Method: `docs/IMPROVEMENT-LOOP.md`.

## Set it up (about 10 minutes)

    cd hub-starter
    ./install.sh --only work-loop,sweep,wake-gate,failure-register,gate-metrics,improve,deadman
    HK=~/.claude/hubkit
    python3 $HK/ledger/ledger add "write the hello file"
    python3 $HK/work-loop/work_loop.py lane add lane-a
    python3 $HK/work-loop/work_loop.py touch --lane lane-a

`touch` means "lane-a is active right now". Call it from a hook of your own (for example on
`UserPromptSubmit`) so an idle lane is one whose last touch is old. The installer never edits your settings.

Then schedule these (cron shown; the launchd examples are in `patterns/schedule-examples`):

    */10 * * * * python3 $HOME/.claude/hubkit/work-loop/work_loop.py tick
    */30 * * * * python3 $HOME/.claude/hubkit/sweep/sweep.py sweep
    */30 * * * * python3 $HOME/.claude/hubkit/failure-register/failure_register.py
    */15 * * * * python3 $HOME/.claude/hubkit/deadman/deadman.py check --all

These four lines are also in `patterns/schedule-examples/crontab.loop.example.txt`. On Windows 11 (WSL2) the loop
needs one more piece to keep Ubuntu running: see "Keep it running" in `docs/WINDOWS.md`.

Jobs that fail on their own record it with `failure-register/record_failure.py` (see its README). The
improve loop is not scheduled blind: run `improve/improve.py mine` when a lane runs dry (the lane's own
instructions can say so), or add it as a fifth line if you prefer.

## What you see

A lane that has been idle 10 minutes gets its ticket in a file:

    cat $HK/state/inbox/lane-a.txt        # L-0001 <TAB> write the hello file

Close it with proof, and the next tick feeds the next one:

    python3 $HK/ledger/ledger proof L-0001 file ~/.claude/hubkit/reports/L-0001.md
    python3 $HK/ledger/ledger status L-0001 done

Instead of a file you can deliver by a command of your own (`work_loop.py lane add NAME --cmd PROGRAM`);
see `patterns/work-loop/README.md`.

## Healthy and idle, or stuck?

    python3 $HK/doctor/doctor.py

| doctor line | meaning |
|---|---|
| `PASS loop-tick` | the tick ran in the last 30 minutes. Healthy. |
| `FAIL loop-tick` | the tick stopped. Lanes are not being fed. Run it by hand, then check the scheduler. |
| `PASS loop-feed` / `SKIP loop-feed` | when a ticket was last fed, or that none has been. Idle is fine if the queue is empty. |
| `FAIL stop-arm` | you wired the optional Stop hook, but its file is missing. |
| `WARN failure-register` | failures are recorded but the register has not run for 2 hours. |

A lane that is idle while open tickets wait raises one "starved" finding a day. `wake-gate` decides whether
that wakes you: only a new serious finding does, and only once per 24 hours per cause.

## Stopping and pausing

- Hold one lane: `work_loop.py hold --lane lane-a --minutes 120`. A hold always ends by itself.
- Stop the optional Stop arm everywhere: `touch ~/.claude/hubkit/state/loop-pause`.
- Remove the schedule lines. Nothing else keeps running.

## Limits you should know

- Delivery is a file or a command you supply. Typing into a running chat is not something this kit does.
- The optional Stop arm (`kit/settings.snippet.loop.json`) is off by default and has not been run against a live
  Claude Code session by this kit's tests; see `kit/HOOKS-VERIFIED.md`.
- `sweep` takes a ticket away from a lane only when you supply a fresh list of live lanes. No list means "unknown",
  and unknown is never treated as dead (`patterns/sweep/README.md`).
