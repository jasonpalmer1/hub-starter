# dup-worker-check

Two workers quietly doing the same job.

## What it fixes

When a chat dies or you open a second one, it is easy to start a new worker on
a ticket that another worker is still busy with. Both finish, both write
files, one overwrites the other. This hook looks at the workers that are still
running in any chat on your machine, and refuses to start another on a ticket
one of them already holds.

## What you need

- python3, 3.9 or newer
- tickets named like `L-0001` in your briefs (the `ledger` pattern hands out
  ids in that shape; any other shape works via `DUP_TICKET_RE`)
- no other pattern is required

## Install

By hand:

    mkdir -p ~/.claude/hubkit/dup-worker-check
    cp dup-worker-check.py dup-worker-hook.py ~/.claude/hubkit/dup-worker-check/

Then add this to your `settings.json` yourself (this pattern never edits it):

    {"hooks": {"PreToolUse": [{"matcher": "Agent|Task",
      "hooks": [{"type": "command",
       "command": "python3 ~/.claude/hubkit/dup-worker-check/dup-worker-hook.py"}]}]}}

## What you will see

If you start a worker for `L-0001` while a live worker already has `L-0001`,
the call is refused with a line like:

    L-0001 is held by live worker a1b2c3 (session sess7, "plan L-0001", active 2.0 min ago)

Either wait for that worker, or message it. If you really want two (a second
opinion, a verifier), start a line of the new brief with `DUP-OK:` and a reason (for example
`DUP-OK: independent check of the first worker`) and try again. Writing the word
somewhere in the middle of a sentence does not count, on purpose. Neither does a
`DUP-OK:` line inside a fenced code block (a pasted brief or an earlier refusal): a block
opens with three or more backticks or tildes and closes only on the same character, at
least as long, with nothing else on the line, so a shorter or different fence inside it is
just text.

## How it decides

- A ticket counts as "primary" for a brief only if it is in the description or
  the first 300 characters of the prompt. A long brief that mentions other
  tickets deep in the text is not claiming them.
- A worker is "live" if its transcript changed in the last 15 minutes
  (`DUP_LIVE_MIN`) and its last message is not the assistant finishing its turn.
- It reads transcripts under `~/.claude/projects/` (`DUP_PROJECTS_ROOT`).
  Nothing is written.
- It never blocks by accident: a missing script, a crash, bad input or a check
  that takes over 3 seconds all let the call through.

You can also run the check by hand:

    echo '{"description":"x","prompt":"work on L-0001"}' | python3 dup-worker-check.py
    echo $?        # 0 = free, 3 = held (the holders are printed)

## Test it

    bash test.sh            # 28 tests in a scratch HOME (about 5 seconds)
    bash test.sh --mutate   # also breaks each guard and checks a test fails

## Lift just this piece

Copy both `.py` files. Nothing else in this repo is needed.
