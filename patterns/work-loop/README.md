# work-loop

An idle lane should be handed its next ticket without anyone typing it, and two lanes must never get the same one.

## What it fixes

A lane (one long-lived worker chat) finishes and sits idle while open tickets wait. Hand-picking
the next ticket for each one is slow and error-prone: you give a lane something another lane owns,
something already being worked, or something you were supposed to decide yourself.
`work_loop.py next` makes that choice by fixed rules and records the offer.

## Install

    ./install.sh --only work-loop      # also installs ledger, which it reads

## Use

    python3 $HK/work-loop/work_loop.py next --lane lane-a
    # prints L-0007, exit 0     (a ticket; the offer is recorded)
    # exit 3                    (nothing to hand out; message on stderr)
    # exit 2                    (bad usage, or lanes.json unreadable / lane unknown)

`--peek` chooses without recording. `--json` prints id, title and the number of skipped ledger lines.

## The rules

Never returned: a ticket owned by another lane; `active` (claimed); `needs-operator`; `done` or
`dropped`; with a non-empty `trigger` (it waits for an outside event); offered to any lane in the
last 24 hours (that memory is what makes two callers get two different tickets).

Order: owned by this lane, then unowned. Inside each group, tickets the operator asked for first.
Ties go to the lower ticket number. Names compare trimmed and lower-case (`Lane-A ` is `lane-a`).
A ticket id written inside another ticket's title means nothing: only the id field counts.

A bad line in the ledger is skipped and reported on stderr; it never stops the pick. The ledger is
read-only here; the only thing written is `$HUBKIT_HOME/state/loop-feed.json`.

## Lane registry (optional): `$HUBKIT_HOME/state/lanes.json`

    {"lanes": {"lane-a": {"aliases": ["a"], "projects": ["site"]},
               "lane-b": {}}}

`aliases`: other owner names that mean this lane (a name minus `-lane` always works).
`projects`: if set, the lane is only given tickets in those projects, or with no project.
With a registry present, an unknown `--lane` is refused (exit 2). With none, any name works.

## Keeping lanes fed: `tick`

Schedule `python3 work_loop.py tick` every 10 minutes (cron or launchd). For each lane in `lanes.json`
it feeds the next ticket if, and only if, all of these hold:

- the lane is not held (`hold --lane L --minutes N`; a hold always expires; `--clear` removes it; an
  unreadable hold file counts as a hold),
- its last sign of life (`touch --lane L`, call it from a prompt/stop hook) is at least 10 minutes old.
  A lane that was never touched, or whose touch is dated in the future (clock skew), is not fed and the
  tick says so,
- it was not fed in the last 30 minutes,
- a ticket is still free when the message is about to go out (the ledger is read again just before
  sending; a ticket claimed in between is dropped, nothing is recorded).

Delivery adapters, set per lane in `lanes.json`:

    work_loop.py lane add lane-a                                  # file (default): state/inbox/lane-a.txt = "L-0007<TAB>title"
    work_loop.py lane add lane-b --cmd /usr/local/bin/notify --cmd lane-b-chat
                                                                  # cmd: that program, argument list, NO shell;
                                                                  # env WORK_LOOP_LANE / WORK_LOOP_TICKET / WORK_LOOP_TITLE

Lane names are `[a-z0-9-]`, 1 to 32 characters. `--cmd` words may only use letters, digits and `_./:=+@%,-`
(use `--cmd=-x` for a word starting with a dash). A failed delivery records nothing, so the next tick retries.

`lane remove NAME` also deletes that lane's touch, hold and inbox files. `lane list` shows lanes and adapters.

Other outcomes: **starvation** = lane idle 30+ min, open tickets exist, nothing fed. It is reported once per
lane per 24 h as an event `loop.starved` and a file in `state/findings/`. The finding also goes through the `wake-gate` pattern (key `starved|<lane>`,
severity crit): it says WAKE the first time and LOG for the same key inside 24 h; the file records the verdict.
Without `wake-gate` installed beside it the file is still written and nothing wakes. **Idle** = no open ticket at all: event `loop.idle_declared`, once per 24 h.
Each completed tick refreshes a `work-loop-tick` heartbeat (limit 30 min) in the `deadman` pattern's files.
`tick --dry-run` prints what it would do and writes nothing (no files, locks or events). Two overlapping
ticks: the second exits at once. Events (`loop.next`, `loop.feed`, `loop.feed_failed`, `loop.starved`,
`loop.idle_declared`) go to `state/events.jsonl`, one JSON line each.

## Stop arm (optional, OFF by default)

`stop_arm.py` is a Stop hook for chats that should pick up the next ticket instead of stopping. It is not in
`kit/settings.snippet.json`; the separate `kit/settings.snippet.loop.json` holds the entry, and you merge it yourself.
The chat names its lane in `HUBKIT_LANE`; the lane must be in `lanes.json`. On a stop it prints
`{"decision":"block","reason":"Next ticket: L-0007 ..."}`, using the same rules as `next`. It lets the chat stop when:
the payload is not JSON or is from a subagent; `state/loop-pause` exists; the lane is unset or unregistered;
the lane holds an active claim; it already continued the lane 3 times without a handed-out ticket being closed
as done (counted in `state/stop-arm.json`, not from `stop_hook_active`); no ticket is eligible (needs-operator ones
never are); or anything fails, including an unreadable ledger. Events: `loop.stop_continue`, `loop.stop_capped`.

## Test

    bash patterns/work-loop/test.sh            # tests, in a scratch HOME
    bash patterns/work-loop/test.sh --mutate   # break each rule in a copy; the tests must notice
