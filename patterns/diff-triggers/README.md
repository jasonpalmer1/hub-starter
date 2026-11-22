# diff-triggers

Two small read-only tools for the review step: one lists the past-incident checks a diff should trigger, the
other reads a rules file as it was BEFORE the change. Observe only: neither one blocks, refuses or writes a verdict.

## What it fixes

1. Reviewers rediscover the same mistakes. A table of rows ("if the diff touches X without Y, check Z") turns each
   past incident into one line the reviewer sees only when it applies.
2. A branch can edit the rules it is reviewed by. `base_read.py` prints a rules file from the base commit, so a
   change to `CLAUDE.md` or `REVIEW.md` cannot soften its own review.

## Install

    ./install.sh --only diff-triggers
    mkdir -p ~/.claude/hubkit/triggers
    cp ~/.claude/hubkit/diff-triggers/triggers.example.txt ~/.claude/hubkit/triggers/triggers.tsv   # then edit

## Use

    python3 trigger_check.py <repo> <base> <tip> [--rows FILE] [--log FILE | --no-log]
    python3 base_read.py <repo> <base> <path>

`trigger_check.py` prints one `TRIGGER-OBSERVE:` line per matching row, a `RULES-EDIT:` line when the diff edits a
rules file (`CLAUDE.md`, `REVIEW.md`, `scripts/pregate.sh`), and a count line. Exit 0 for any diff it can read; 2 for a bad
call, bad ref or a bad table. The rows come from your install root, never from the diff.

Row columns (TAB): `id`, `trigger`, `check`, `source`, `replaces`, `mode` (`observe` or `off`). Trigger terms are
joined with ` & `: `changed:<glob>`, `unchanged:<glob>`, `added-path:<regex>`, `added-line:<glob>`,
`added-line-re:<pathre>~~<linere>`, `none`. A row with an empty `replaces` is refused: a new check must name the
check it removes. Use `-` only for a note-only row with trigger `none`.

`base_read.py` exits 0 and prints the file as it is at the base; 3 when the file does not exist at base (it never
falls back to the tip copy); 2 for a bad base, an unsafe path (`..`, absolute, newline) or a symlink.

## Wire it into the reviewer

In `bug-gate.md` (see `patterns/gate-agents`) the reviewer reads the rules via `base_read.py` and the
`trigger_check.py` lines. Each flagged row is one item to check, nothing more.

## Optional: record how each change was verified

If your land script writes a merge message, append one line built only from records it already holds, for example
`Verified: pregate=<12 hex> gate=PASS|FAIL-fixed|none`. It must never block a land and never create a verdict file.
Later you can join reverted changes against `gate=none`. This kit ships no land script, so this is a convention only.

## Test it

    bash test.sh            # 20 tests on throwaway git repos in a scratch HOME
    bash test.sh --mutate   # also breaks each rule and checks a test fails
