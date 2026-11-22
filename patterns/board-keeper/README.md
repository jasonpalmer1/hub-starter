# board-keeper

Keeps a card board in step with the to-do list, with plain code and no model calls.

## What it fixes

A board of cards (one column per stage: ideas, grooming, todo, building, blocked, verifying, review, done)
drifts from the real list. Tickets close and their card sits in "building" for a week. This script
compares each card's column with the status of the items it links, and fixes only what is obvious.

## The rules

- **Fix:** a card in an open column whose linked items are all closed (at least one done) moves to `done`.
- **Move, forward only:** a claimed and owned item moves its card to `todo`; a live worker moves it to `building`;
  a waiting item to `blocked`; an item with a VERIFY line to `verifying`. Only the legal edges are taken. A jump
  backward or over a column is flagged `illegal-move-blocked`, never made.
- **Flag, never change:** a `done` card with open items, a `building` card whose owners are all gone, a linked item the list does not know.
- **Caps:** 25 fixes and 100 moves of any kind per day, counted from the log. An unreadable log counts as the cap reached.
- **Log first, or nothing moves:** the attempt line (`board-keeper.log`) and the move line (`moves.jsonl`) are written BEFORE each move. If either cannot be written the run stops, nothing moves, and it exits non-zero.
- **One run at a time:** a file lock (`board-keeper.lock`); a second run exits at once.
- **Kill switch:** create `state/board-keeper/board-keeper-off` and the next run reads and writes nothing; it is checked again before every single move.
- **No clobbering a person:** the board is re-read right before each write; only that one card changes, and only if it is still in the column the run saw. Otherwise the card is flagged `card-changed-since-read`. `undo` likewise refuses a card that was moved since.
- **A suspect items file** (missing, not valid JSON, empty) makes the run flag-only.
- **People win:** if anyone but board-keeper moved a card, the forward mover leaves it alone for 24 hours; if a person ran `undo`, the to-done close is held too (flag `undone-by-hand`). A proof-based close is not held by a plain move.
- It never closes, edits or deletes an item.

## What you need

- python3, 3.9 or newer; nothing else

## Install

    ./install.sh --only board-keeper

## Files

    ~/.claude/hubkit/state/board.json    {"cards": [{"id": "c1", "stage": "todo", "linked": ["L-0001"]}]}
    ~/.claude/hubkit/state/items.json    {"L-0001": {"status": "active", "owner": "w1", "wait": false, "verify": false}}
    ~/.claude/hubkit/state/live-owners.txt   one live worker name per line (missing, empty or older than 24 hours means unknown: no building moves, no dead-owner flags)

Write `items.json` from your own to-do list (for example from the ledger) before each run.

## Use it

    python3 ~/.claude/hubkit/board-keeper/board_keeper.py --dry-run --json
    python3 ~/.claude/hubkit/board-keeper/board_keeper.py
    python3 ~/.claude/hubkit/board-keeper/board_keeper.py undo c1

Run it from a schedule (every 10 minutes is plenty). Every change is one line in `state/board-keeper/moves.jsonl`
(who, why, from, to, time).

## Test

    bash patterns/board-keeper/test.sh            # 37 tests
    bash patterns/board-keeper/test.sh --mutate   # breaks each guard, tests must notice

## Differences from the private hub

The kit version is deliberately smaller. Everything below is a known gap against the private original, not an accident.

| Private hub does | Kit does | Why |
|---|---|---|
| Reads a ledger of events, needs 100+ events or treats it as truncated | Reads `items.json`; the minimum row count is `BK_MIN_ITEMS`, default 1 | A stranger has no 100-event history. Set it to your normal size. |
| Moves cards through a pipeline command with an expected-from check | Edits `board.json` itself: re-reads it right before each write and compares the column | No pipeline command in the kit. A person writing in the same instant between our re-read and rename could still be lost (tiny window). |
| Holds a person-asked card in review for an accept, reopens closed items | Not ported | Needs the private ledger and an accept flow. |
| Refuses to act on an unstable ledger | Not ported | There is no ledger. |
| Checks the lock and the kill switch, logs a skip line | Same, plus exits non-zero when the log cannot be written | Restored after the first gate. |

Restored after the first gate: log-before-move (fail closed), run lock, kill switch before every move, live-owners 24 hour and empty-file rules, stale-snapshot protection, fixes held only after an undo.
