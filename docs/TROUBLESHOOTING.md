# Troubleshooting

First move for anything (the installer always installs the doctor, even with `--only`): `python3 ~/.claude/hubkit/doctor/doctor.py`. It prints the
exact fix under each FAIL. To ask someone else for help, run it with `--paste` and
read it once before sending.

## The doctor says hooks are not wired / settings not found

Expected after install: the installer never edits `settings.json`. Merge the entries
from `~/.claude/hubkit/settings.snippet.json` into the `hooks` section of
`~/.claude/settings.json` yourself, then rerun the doctor. If your settings live
elsewhere:

    python3 ~/.claude/hubkit/doctor/doctor.py --settings /path/to/settings.json

## The installer says "kept yours (differs, not touched)"

You already had a different file at that spot. It was left alone on purpose. Compare
the two, and if you want the new one, move yours aside and run `./install.sh` again.
Running it a second time otherwise changes nothing.

    ./install.sh --dry-run

## "python3 not found" or an old Python

Everything needs python3, 3.9 or newer. Check with `python3 --version`.
On Windows use WSL2 and follow `docs/WINDOWS.md`; the ledger and lock files use Unix file locking.

## The ledger will not close an item

    python3 ~/.claude/hubkit/ledger/ledger status L-0001 done

"No proof" is the answer you want. Attach one with `ledger proof L-0001 file PATH`
(or a commit, link or test output), then close it. If it says a line is malformed,
the ledger file has a broken line and the commands stop instead of miscounting.
Fix or remove that one line.

## A helper was refused when it tried to stop (report-gate)

It did not write the report named in its brief. Say the exact file in the brief:
`write your report to ~/.claude/hubkit/reports/NAME.md`. A helper with no named path
is refused once, then let through, and the brief is saved to
`~/.claude/hubkit/state/report-gate-badbriefs.log`: use that file to fix whatever
writes your briefs. A worker that needs no report gets a line `report: none`.

## A new Agent call was refused: "no model" (model-guard)

Working as designed. Name the model on the call and it goes through.

## A new Agent call was refused: "held by live worker" (dup-worker-check)

Another live helper already has that ticket. Wait for it or message it. If you truly
want two, start a line of your brief with `DUP-OK:` and a reason.

## A lane is refused (lane-lock)

Exit 2 means someone alive holds it; wait, or if their process is gone the next claim
clears it. Exit 3 means two failures in a row tripped the breaker: read the change,
then reset it with your name and a reason:

    python3 ~/.claude/hubkit/lane-lock/lane-lock.py status my-repo
    python3 ~/.claude/hubkit/lane-lock/lane-lock.py force-release my-repo --by you --reason "owner crashed"
    python3 ~/.claude/hubkit/lane-lock/lane-lock.py reset my-repo --by you --reason "read the diff, fixed"

## The deadman alarms on a brand-new job

You scheduled the check before the job ever beat. Beat once by hand with its limit,
then schedule the check:

    python3 ~/.claude/hubkit/deadman/deadman.py beat nightly-backup --max-age 25h

To stop watching a job you retired: `deadman.py retire nightly-backup`.

## My commit was blocked by the secret scanner

It shows file, line and rule, never the value. Take the value out, read it from an
environment variable, `git add` again. Do not use `--no-verify` on a flagged line.
If the text is made up on purpose (a test), list that path in `SECRET_SCAN_ALLOW_PATHS`.

## A builder never seems to stop at its cap

The caps live in the agent file's own frontmatter. Hooks there are skipped, with no
error, if the agent lives in a project folder you have not trusted. Keep it in
`~/.claude/agents/`; the doctor warns when a copy is also in a project.

## The disk guard prints CRIT

Either the disk is nearly full (clear regenerable files: build output, caches) or it
could not read the disk at all, which counts as CRIT on purpose.

    python3 ~/.claude/hubkit/disk-guard/disk-guard.py --path ~

## Start over

Everything lives in `~/.claude/hubkit`, plus the agent files and `hub.md` command the
installer printed under "outside hubkit" (`~/.claude/agents/`, `~/.claude/commands/`). Run
`./install.sh --uninstall` from the repo folder to remove exactly those, then reinstall.
Files you edited are kept, and your own files and `settings.json` are never touched.
