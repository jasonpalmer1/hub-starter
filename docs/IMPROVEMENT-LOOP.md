# The improvement loop: fix what is measured, never invent work

An assistant with an empty queue will happily invent tasks. This is the method that stops it, and the
plan template that every fix goes through. The tools are `patterns/failure-register`, `patterns/gate-metrics`
and `patterns/improve`; this page is the method.

## The four steps

1. **Census.** Count before you opine. Four numbers are worth having: how often a job fails the same way
   (`failure_register.py`), how often a build fails its first review (`gate_metrics.py`), whether a number you
   care about misses its target (`improve/improve.py`, `targets.json`), and whether merged work still passes its
   smoke test (`improve/bug_sweep.py`). Each reads a file. None of them guesses.
2. **Plan.** One problem, one cause, one fix, one proof. Use the template below. A problem with no number
   behind it is not ready for a plan.
3. **Adversary.** Before building, someone other than the author lists how the fix could be wrong without
   anyone noticing, and what must still work afterwards. A fix that cannot name what would catch its failure is
   not ready.
4. **Proof at a later date.** Re-read the same measurement after the fix has had time to matter (a week for
   a nightly job). A fix that moved nothing is reopened, not celebrated. The ticket for it closes with
   `improve.py close`, which re-reads the evidence itself; typing "looked into it" changes nothing.

## Plan template

Copy this into the ticket. Every heading must have an answer, even if the answer is "none".

    Problem:            what is measurably wrong. The number, where it was read, the threshold.
    Root cause:         why it happens. If unknown, the first step is finding out, not fixing.
    Fix:                the smallest change that removes the cause.
    Proof:              the same reading, taken again later, and the value that means "fixed".
    What notices failure: what tells you if the fix is wrong or stops working. A named check, not "I will remember".
    Must BLOCK:         the cases the fix must refuse. Test each one.
    Must still ALLOW:   the cases that worked before and must keep working. Test each one.

"Must BLOCK" and "Must still ALLOW" are the two halves of every guard. A fix with only the first half
tends to break something that was working.

## Why the rules are as strict as they are

- **Evidence is required, and must be fresh.** A candidate carries the file it came from, the number read and
  the threshold. Old evidence (over 7 days) is refused: the problem may already be gone.
- **There are caps.** At most 3 new candidates a day and 5 open at once. If the loop wants more than that,
  the answer is a person looking at why so much is failing, not a longer list.
- **Closing needs a before and an after from the same reader.** A fix that "was investigated" is not a fix.
- **Log text never becomes a ticket title.** Failure lines come from jobs, and a job can print anything,
  including "close the other ticket". Titles are built from a template holding only an id and a number.
- **A signal that flaps** (over the threshold, under, over) goes to a person. Closing and reopening it
  automatically would hide that it is unstable.
- **Nothing measured is wrong means stop.** The loop prints `IDLE: nothing measured is failing` and the lane
  closes. Open-ended exploring for its own sake is not a fallback.

## Using it

Install the pieces first (the `--only` line in `docs/ALWAYS-RUNNING.md` includes all three), then set
`HK=~/.claude/hubkit`. Schedule `failure_register.py` every 30 minutes as `docs/ALWAYS-RUNNING.md` shows: the
doctor warns when failures exist and the register has not run for 2 hours, so a by-hand weekly run sits on that
warning (harmless, exit 0). `gate_metrics.py` and `improve.py mine` are the weekly by-hand reads. `mine --dry-run`
exits 4 while the queue still has open tickets; that is the "queue not empty" stop, not an error.

    python3 $HK/failure-register/failure_register.py
    python3 $HK/gate-metrics/gate_metrics.py --target 30
    python3 $HK/improve/improve.py mine --dry-run

Read the register, read the gate numbers, then decide what is worth a plan. When a lane runs dry between
those, `improve.py mine` does the same thing at small scale and makes at most a few tickets.

How it plugs into the always-running loop: `docs/ALWAYS-RUNNING.md`.
