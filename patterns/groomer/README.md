# groomer

Plain rules that tidy a new to-do item the moment it is filed.

## What it fixes

Items arrive with no finish line, and the same machine alarm gets filed ten times. The groomer
writes a one-sentence "done means" when the item has none, suggests a kind and a grade, and finds
repeats among open machine-made items. It makes no model call, uses no network, and never raises:
if anything goes wrong the item is filed exactly as it was.

## What it will not do

- It never changes an item a person filed: those sources are protected and are never folded.
- It never closes, deletes or re-owns anything. Kind and grade are suggestions only.
- It never logs a title. The counts log holds event names, the source and numbers.
- It matches only open or active items, and items that differ in an id-like word (anything with a digit) are different things.

## What you need

- python3, 3.9 or newer; nothing else. The verify-watch pattern is optional (adds a suggested VERIFY line).

## Install

    ./install.sh --only groomer

## Modes

Set by files in `~/.claude/hubkit/state`:

| file | mode | effect |
|---|---|---|
| `groom-off` | off | nothing happens (kill switch) |
| `groom-shadow` | shadow | only counts are written to `groom-events.jsonl` |
| none | live | finish line filled, kind and grade suggested, repeats reported |

A missing state folder counts as shadow, never live.

## Use it

    python3 ~/.claude/hubkit/groomer/groomer.py mode
    python3 ~/.claude/hubkit/groomer/groomer.py groom --title "login page is broken" --source operator-chat
    python3 ~/.claude/hubkit/groomer/groomer.py similar --title "disk full on build host" --items open-items.json

From code, call `groom(title, source, done_means, items)` once when you create an item and apply the
fields it returns (`done_means`, `kind`, `grade`, `fold_to`, `verify`). Wire it into your own "add"
command; the ledger pattern does not call it for you.

## Test

    bash patterns/groomer/test.sh            # 22 tests
    bash patterns/groomer/test.sh --mutate   # breaks each guard, tests must notice

## Differences from the private hub

| Private hub does | Kit does | Why |
|---|---|---|
| Never folds when the item is grade 9 or 10, has its own ticket, is a gate note, or has an origin | Same: `--grade`, `--own-ticket`, `--gate-note`, `--origin` (a grade that is not a number counts as high) | Restored after the first gate. |
| Protects anything asked by the operator (`asked_by`) and anything whose title or notes cite an operator card, tap or dated quote | Protects by source (`PROTECTED_SOURCES`) and by `asked_by` (`PROTECTED_ASKERS`, default `operator`; add your own name), for both the new item and the fold target | The title and notes wording rules are tied to the private tracker, so they are not ported. If your tracker has such markers, set the source or asked_by at filing time. |
| Calls a small model to rewrite a finish line, runs a detached scan, replays, writes a ledger note | None of these | No model call and no ledger in the kit. |
