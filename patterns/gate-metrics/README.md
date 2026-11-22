# gate-metrics

Counts how often a build fails its first review, from files your reviewers already write. Read only: it
never changes a verdict, a ticket or a file.

## What it fixes

Without a number, "the reviews keep failing" is a feeling. With one you can see whether a change made
first-time quality better or worse, and whether a builder is being dispatched over and over for the same slice.

## Install

    ./install.sh --only gate-metrics
    python3 ~/.claude/hubkit/gate-metrics/gate_metrics.py

## What it reads

1. Verdict files. The `bug-gate` agent (see `patterns/gate-agents/bug-gate.md`) writes a `VERDICT.md` next to its
   report, with header lines `VERDICT:`, `TICKET:`, `SLICE:` and `ROUND:`. Any file named `VERDICT.md` or
   `*-VERDICT.md` under `~/.claude/hubkit/bug-gate/` (up to 4 folders deep, symlinks ignored) is read.
   `PENDING`, an unknown verdict, or a ticket that is not `L-` plus 3 to 5 digits is not counted.
2. The spawn index, `~/.claude/hubkit/state/gate-spawns.jsonl`, one line per builder or bug-gate spawn. The
   `model-guard` hook writes it when both are installed; without it the spawn counts stay 0.

A chain is one ticket plus one slice. Its first gate is the lowest `ROUND` (the oldest file breaks a tie). The
headline is the share of chains whose first gate FAILED.

## Use

    python3 gate_metrics.py                 # plain text, 3 lines
    python3 gate_metrics.py --json          # the same numbers for a script
    python3 gate_metrics.py --target 30     # exit 1 (and say OVER TARGET) when the share is above 30 percent
    python3 gate_metrics.py --root DIR --index FILE   # read other places

    gates 3 (0 not counted) in 3 chains
    first-gate FAIL share: 1/3 = 33.3%
    spawns: bug-gate 3, builder 4 (0 corrupt index lines)

An unreadable root or index (missing, a folder, no permission) prints one line to stderr and counts as empty; the exit code is unchanged. A spawn line whose `subagent_type` is not a string is counted as corrupt.

Env: `HUBKIT_HOME` (install root), `GATE_SPAWNS_LOG` (index file). Exit 0 unless `--target` is given and missed.

## Never breaks the spawn

The index is written by `model-guard`, not by this tool, and only as one append after the call is already
allowed. If the index is unwritable, a symlink, a folder or corrupt, the spawn still goes through and this
tool skips the bad lines and counts them.

## Test it

    bash test.sh            # 15 tests in a scratch HOME
    bash test.sh --mutate   # also breaks each rule and checks a test fails
