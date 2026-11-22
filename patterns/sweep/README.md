# sweep

Three small commands for a ledger that several lanes share.

    python3 sweep.py sweep               # findings: stale tickets, dead owners, done-without-proof; each goes through wake-gate
    python3 sweep.py orphans             # list tickets whose owner is provably dead
    python3 sweep.py orphans --reassign  # put those tickets back in the queue (open, unowned)
    python3 sweep.py hold --lane lane-a --minutes 120   # keep lane-a's tickets even if it looks dead

## The one rule: unknown is not dead

You supply `$HUBKIT_HOME/state/live-owners.txt` from whatever knows which lanes are alive (a process list, the
`touch` files of work-loop, a chat roster). Write it atomically, every few minutes:

    # live-owners 1800000000
    lane-a
    lane-b
    # end

A ticket is taken from its owner only when this file is present, whole (header and `# end`), under 30 minutes old,
names at least one owner, and does not list the ticket's owner. A missing, corrupt, truncated, stale, empty or
symlinked file, or one with any malformed name line, means liveness is UNKNOWN: nothing is reassigned and `sweep` reports `liveness-unknown`.

Never taken: tickets owned by `operator` or `unowned`; owners that are alive; held lanes; needs-operator, done and
dropped tickets; tickets touched after the list was written or in the last 10 minutes; anything while the ledger has
an unreadable line. Names match whole, so `lane-1` dead does not touch `lane-10`.
A name line is one owner: letters, digits and `. _ @ -` only (up to 64), case-insensitive. A comma-joined line, a
trailing note like `(pid 123)`, JSON, or anything with a space makes the WHOLE list UNKNOWN, so nothing moves.
A hold file that is unreadable, `NaN` or not a number counts as a hold.

Known gap: the list is read once and may be up to 30 minutes old. A lane that restarted after the list was written
and resumes its old ticket without touching the ledger can lose it in that window. A ledger touch after the list was
written protects the ticket; shorten `LIVE_MAX_AGE_S` or refresh the list often if that matters to you.
`orphans --json` prints one JSON line on stdout (notes go to stderr); a ledger line with a non-text `id` is skipped
with a warning.

## Wakes

Findings go through `wake-gate`. Only a dead lane holding an operator-asked ticket can wake (once per lane per 24 h).
Stale tickets, unknown liveness, done-without-proof and dead owners of ordinary tickets are logged, never woken.
With wake-gate not installed, findings print as `NO-GATE` and nothing wakes anyone.

## Safety

`orphans --reassign` takes a non-blocking lock (a second sweep exits at once) and re-reads the ledger under the
ledger lock before each write, so concurrent sweeps return a ticket once. Events: `sweep.finding`, `orphans.reassigned`
in `state/events.jsonl`. Tests: `bash patterns/sweep/test.sh` (add `--mutate` to break each rule and see the tests fail).
