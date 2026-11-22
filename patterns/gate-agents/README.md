# gate-agents

Six reviewer agents that stand between an idea and production.

## What it fixes

A worker that builds something and then reviews its own work finds nothing.
These agents each do one job, in a fresh context, and write a verdict to a file.

| agent | when | output |
|---|---|---|
| planner | before any new scope | a plan file marked DRAFT; you or the hub mark it APPROVED |
| bug-gate | on the exact diff before it ships | PASS or FAIL with reproductions |
| playtester | after a build, before production | a stranger's-eye report with screenshots |
| bottleneck | the same stage keeps stalling | a verdict, a measured fix and a ready build brief |
| judge | after a for and an against review, or a tripped breaker | one ruling file with one verdict word |
| check-checker | before any new check, test, hook or alarm is added | KEEP, CUT or MERGE per check |

The builder agent that does the work lives in the `turn-cap-builder` pattern.

## What you need

- Claude Code with subagents
- playtester only: a headless browser you can script (Playwright, for example)
- nothing else; no other pattern is required (`lane-lock` is used if present)

## Install

By hand. Agent files must not overwrite yours:

    mkdir -p ~/.claude/agents ~/.claude/hubkit/plans
    for f in planner bug-gate playtester; do
      [ -e ~/.claude/agents/$f.md ] || cp $f.md ~/.claude/agents/$f.md
    done

If a name is taken, copy under another name (for example `hk-planner.md`) and
change the `name:` line inside it to match.

## Use it

Ask for the agent by name in a chat: "run the planner on <idea>", then after
the build "run the bug-gate on this branch". Pair them with `report-gate` so a
gate cannot finish without writing its report.

## Rules baked in

- Acceptance first: the gate runs the user's own verb against real data before reading code.
- A FAIL blocks the ship.
- The bug-gate mutates one guard in a temporary copy to prove the tests notice.
- Gates never fix code; the hub routes fixes.

## Test

    bash patterns/gate-agents/test.sh

Checks each file's frontmatter, that tools are read-only plus Write (no Edit),
and that no personal path or leftover project rule slipped in.
