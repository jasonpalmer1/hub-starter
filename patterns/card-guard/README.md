# card-guard

A guard that stops a decision card from reaching you in engineering language.

## What it fixes

A worker needs a decision and posts "ALARM k=3fa9c1 slice 4 of groom-check.py on main". You cannot
answer that. The guard refuses a card until it reads like something a person can decide in one tap.

A good card is: a short question, a `Proof:` line (how the facts were checked), a `Why:` line
(what happens if the recommended answer is picked), and 2 or 3 options, each with a label and a
plain sentence saying what it does. The recommended option comes first.

## What it refuses

- ticket ids, commit hashes, card ids, file names, `[script-name]` tags, raw JSON, ISO timestamps, a cut-off `...` sentence
- the words "slice" and "main"; machine alarm text
- a header over 100 characters; fewer than 2 options; an option with no description; options that are only Yes/No
- (worker cards) no `Proof:` line, or a `Why:` line that is empty or names no consequence
- a worker's third card in one run (cap 2, cards older than 12 hours stop counting)
- a question that was already answered (`state/answers.jsonl`), so nobody is asked twice
- non-ASCII or control characters, over-long fields, a ticket that does not look like `L-0001`

## What you need

- python3, 3.9 or newer; macOS or Linux. Nothing else.

## Install

    ./install.sh --only card-guard

## Use it

Check a card you wrote:

    python3 ~/.claude/hubkit/card-guard/card_guard.py --header "Go live today?" \
      --context "Proof: tested\nWhy: it saves a week" \
      --option "Ship|Puts the change live" --option "Wait|Keeps the old page" --require-why --require-proof

A worker files its own card (appended to `~/.claude/hubkit/state/cards.jsonl` for your page or script to show):

    python3 ~/.claude/hubkit/card-guard/worker_card.py --ticket L-0001 --header "Go live today?" \
      --why "it saves a week because the review is done" --proof "test run passed" \
      --option "Ship|Puts the change live" --option "Wait|Keeps the old page" --recommended 0

It prints `Card: <id>`; the worker pastes that line in its report. A retry with the same ticket and
header posts nothing new and costs no cap.

## Test

    bash patterns/card-guard/test.sh            # 23 tests
    bash patterns/card-guard/test.sh --mutate   # breaks each guard, tests must notice

## Differences from the private hub

| Private hub does | Kit does | Why |
|---|---|---|
| Posts the card to the operator's decision page through a bridge | Appends it to `state/cards.jsonl` | The bridge is private. Show that file however you like. |
| Cap ledger line broken: the run crashes (so it fails closed) | Refuses the card with a clear message (`cap ledger ... corrupt`), never resets the count; also refuses a NaN or missing time | Same safety, plain error. Fix or delete the named file to continue. |

Concurrency note: the 2-card cap is kept by a file lock around the ledger read and write; the suite does not race parallel runs.
