# ledger

A to-do list that will not let anyone say "done" without proof.

## What it fixes

AI workers (and people) report "finished" when the work is half done. The ledger
keeps one list of items. An item can only be closed after you attach proof
(a file, a commit, a link, a test output). Without proof, `status done` is
refused.

## What you need

- python3, 3.9 or newer
- macOS or Linux (Windows: use WSL)
- nothing else; no other pattern is required

## Install

The repo installer copies it for you. By hand:

    mkdir -p ~/.claude/hubkit/ledger
    cp ledger ledger_lib.py ledger-banner-hook.py ~/.claude/hubkit/ledger/
    chmod +x ~/.claude/hubkit/ledger/ledger

Then add that folder to your PATH, or run it by full path:

    ~/.claude/hubkit/ledger/ledger --help

Your list is stored at `~/.claude/hubkit/state/ledger.jsonl`. Set `LEDGER_PATH`
to use a different file (handy for trying it out).

## Use it

    ledger add "write the hello file"       # prints L-0001
    ledger status L-0001 active
    ledger status L-0001 done               # refused: no proof yet
    ledger proof L-0001 file ./hello.txt
    ledger status L-0001 done               # works
    ledger list

`ledger --help` has the same walkthrough plus every command.

## Show the list in every chat turn (optional)

`ledger-banner-hook.py` is a Claude Code `UserPromptSubmit` hook. It adds a few
lines to each prompt: the time, how many items are open, how many need you, and
the items that need attention. Add this to your `settings.json` yourself (this
pattern never edits it):

    {"hooks": {"UserPromptSubmit": [{"hooks": [
      {"type": "command",
       "command": "python3 ~/.claude/hubkit/ledger/ledger-banner-hook.py"}]}]}}

It never blocks a prompt: if the ledger is missing or broken it prints just the
clock and exits cleanly.

To add your own lines (say, a reminder when no workers have run for a while),
put a `banner_extras.py` with `prompt_lines(session_id, cwd)` next to the hook.
If it is missing or fails, the hook prints what it would have printed anyway.

## Statuses

| status | meaning |
|---|---|
| open | nobody started it |
| active | being worked now |
| needs-operator | waiting on you |
| done | finished, with proof |
| dropped | abandoned on purpose |

## How it is built

- One JSON line per change, appended, never edited. An item's state is every
  line for its id folded in order.
- Ids are never reused, even if the file is rewound (a sidecar high-water mark).
- Concurrent writers are safe (file lock around id allocation, atomic appends).
- A malformed line stops the commands rather than quietly under-counting.
- Proof types `metric-claim` and `metric-claim-verdict` are kept on the record
  but do not satisfy the done gate: a claim about a number is not evidence.

## Test it

    bash test.sh            # 66 tests in a scratch HOME
    bash test.sh --mutate   # also breaks each guard and checks a test fails

## Lift just this piece

Copy the three files. Nothing else in this repo is needed.
