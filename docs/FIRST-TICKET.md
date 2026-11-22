# Your first ticket

One small task, start to finish, so you see every guard do its job. About 10 minutes.
Do the install in the README first.

The builder agent is not in the core set. Add it:

    cd hub-starter             # the installer is run from the repo folder
    ./install.sh --only turn-cap-builder

Shortcut used below:

    HK=~/.claude/hubkit

## 1. Write the ticket down

    python3 $HK/ledger/ledger add "write the hello file"

It prints `L-0001`. The ticket now exists in a list that outlives the chat.

## 2. Plan before you build (in Claude Code)

Tell Claude: `run the planner on L-0001: write a file hello.txt that says hello`.
The planner writes a plan under `~/.claude/hubkit/plans/` and changes nothing else.
Read it. You (or the main Claude Code conversation you are directing the work from; see `docs/GLOSSARY.md`) approve it before any building starts.

## 3. Mark it started

    python3 $HK/ledger/ledger status L-0001 active

## 4. Build, with a report

Tell Claude: `use the builder agent on L-0001; write your report to
~/.claude/hubkit/reports/L-0001.md`. Naming the report path is the key line.
With `report-gate` switched on, the builder cannot finish until that file exists
and has real text in it. With `turn-cap-builder`, a runaway builder is stopped
and told to write a handoff.

## 5. Try to close it without proof

    python3 $HK/ledger/ledger status L-0001 done

It is refused. That is the point.

## 6. Attach the proof, then close it

    python3 $HK/ledger/ledger proof L-0001 file ~/.claude/hubkit/reports/L-0001.md
    python3 $HK/ledger/ledger status L-0001 done
    python3 $HK/ledger/ledger list

Proof can be a file, a commit, a link or a test output. A claim about a number does not count.

## 7. Review the exact change

Tell Claude: `run the bug-gate on this change`. It reads the diff, runs the tests,
and answers PASS or FAIL with a way to reproduce each problem. A FAIL blocks the ship.
For anything people will see, add `run the playtester` as well.

## 8. Check the setup is still healthy

    python3 $HK/doctor/doctor.py

## 9. Let the loop hand out the next one (optional)

    ./install.sh --only work-loop      # from the repo folder
    python3 $HK/ledger/ledger add "write the goodbye file"
    python3 $HK/work-loop/work_loop.py next --lane lane-a

It prints the id of the new ticket and records that it was offered, so a second lane asking right
now gets a different one (or exit code 3 when none is left). Run `next` again and see that the same ticket
is not offered twice. To have it handed out on a schedule, with no one asking, follow `docs/ALWAYS-RUNNING.md`.

## What just happened

A written plan, a worker that had to show its work, a ticket that could not be
closed on a claim, and a reviewer that could say no. Each is its own piece
(`docs/ADOPT-ONE-PIECE.md`); you can keep the ones that earn their place.
