# ticket-lock

Two workers on one ticket, both finishing, one overwriting the other.

## What it fixes

`dup-worker-check` looks at workers that are still running. This pattern is stricter and simpler:
when a worker is started for a ticket, a small lock file is written. A second worker for the same
ticket is refused until the first one finishes, a handoff is written, or the lock lapses (an hour, or 2 minutes if the worker never starts).

It has no hook of its own. It is a library that two other patterns call:

- `model-guard` takes the lock as the last check before it allows a worker to start.
- `report-gate` frees the lock when the worker is allowed to finish (its report exists).

Install all three for the full loop. With only `model-guard` the locks are taken and expire after
an hour. With neither, nothing happens; the library on its own does nothing.

## What you need

- python3, 3.9 or newer
- `model-guard` and `report-gate` installed beside it (see the install line below)
- tickets named like `L-0001` in the worker's description (any other shape works via `TICKET_LOCK_ID_RE`)

## Install

    ./install.sh --only model-guard,report-gate,ticket-lock

By hand, copy the folder next to the other patterns:

    mkdir -p ~/.claude/hubkit/ticket-lock
    cp ticket_lock.py ticket-lock.py ~/.claude/hubkit/ticket-lock/

The hooks find it at `~/.claude/hubkit/ticket-lock/` (or `../ticket-lock/` beside the hook).
If the folder is missing, or anything inside it fails, the hooks behave as if it did not exist.

## What you will see

A second builder for a held ticket is refused:

    L-0001 already owned by Build L-0001 widget; write a handoff or wait

To take over on purpose, put a line at the start of the worker's prompt:

    HANDOFF-FROM: Build L-0001 widget

To run a second opinion on the same ticket, put `DUP-OK` anywhere in its prompt.
Only `builder`, `general-purpose` and `claude` workers take or hit locks; reviewers
(`bug-gate`, `playtester`, `planner`) never do. Only ids in the worker's description lock;
ids quoted in the prompt body do not.

## Freeing a stuck lock

If a worker was stopped or crashed (so it never finished normally):

    python3 ~/.claude/hubkit/ticket-lock/ticket-lock.py release L-0001
    python3 ~/.claude/hubkit/ticket-lock/ticket-lock.py list

Or describe the re-start as a "fix round" or "re-dispatch" from the same chat: once the lock is
more than 10 minutes old that spawn is allowed. The lock also expires on its own after an hour. A claim whose worker never started (the spawn was blocked by
another hook) lapses after 2 minutes, and the same session's retry of the same brief takes it over after 15 seconds.

## Alarms

Written to `$HUBKIT_HOME/state/ticket-alarms.log`:

- `TICKET-HANDOFF` when a handoff takes a held ticket.
- `TICKET-MISMATCH` when a finishing worker ran `ledger status <id> done|closed` for a ticket it was
  not briefed on (only an executed command counts, not text in a quoted argument).

## Test it

    bash test.sh            # 49 tests in a scratch HOME
    bash test.sh --mutate   # also breaks each guard and checks a test fails

## Lift just this piece

Copy `ticket_lock.py`. Call `acquire(tool_input, session_id, tool_use_id)` before starting a worker
(it returns a refusal message or `None`) and `release_and_audit(prompt, transcript_path, agent_id)`
when it stops. Both swallow their own errors.
