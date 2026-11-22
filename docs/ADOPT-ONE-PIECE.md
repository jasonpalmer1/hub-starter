# Adopt one piece at a time

Every piece works alone. None needs another one, except `schedule-examples`, which
wants `deadman`. Pick the pain you feel most, adopt that, live with it a week, then
add the next.

Two ways to get a piece:

    ./install.sh --only deadman          # the installer, one piece (plus what it needs)
    # or by hand: copy the files listed in docs/PATTERN-MATRIX.md

Each piece has its own guide at `patterns/<name>/README.md`. The commands below were run
in a fresh home folder. `HK` means `~/.claude/hubkit`.

## A list that will not let "done" be a claim: `ledger`

    ./install.sh --only ledger
    python3 $HK/ledger/ledger add "try the ledger"
    python3 $HK/ledger/ledger status L-0001 done        # refused, no proof yet
    echo ok > ~/proof.txt
    python3 $HK/ledger/ledger proof L-0001 file ~/proof.txt
    python3 $HK/ledger/ledger status L-0001 done

Optional banner in every chat turn: merge the `UserPromptSubmit` entry from
`settings.snippet.json`.

## A job that tells you when it dies: `deadman`

    ./install.sh --only deadman
    python3 $HK/deadman/deadman.py beat nightly-backup --max-age 25h
    python3 $HK/deadman/deadman.py check --all
    python3 $HK/deadman/deadman.py list

Beat before you check: a `check --all` with no beat yet raises a stale alarm (exit 1) for a
brand-new job. Call `beat` as the last line of a job, only when it worked. Run `check --all` from a
different scheduler entry (`patterns/schedule-examples` has cron and macOS files).

## A health check that says what to fix: `doctor`

    ./install.sh --only doctor
    python3 $HK/doctor/doctor.py
    python3 $HK/doctor/doctor.py --paste     # safe to paste into a chat

Read-only. It never changes anything.

## A helper cannot stop without its report: `report-gate`

    ./install.sh --only report-gate

Then merge its `SubagentStop` entry from `~/.claude/hubkit/settings.snippet.json`.
Put the report path in every brief you give a helper. Try it without Claude:

    echo '{"session_id":"s1","agent_id":"w1","agent_type":"builder"}' | python3 $HK/report-gate/report-gate.py; echo $?

It prints a refusal and exit code 2: a typed helper with no brief on record is
refused once.

## A helper must say which model: `model-guard`

    ./install.sh --only model-guard

Merge its `PreToolUse` entry from `~/.claude/hubkit/settings.snippet.json` (the installer writes
it there, and `kit/settings.snippet.json` in this repo has the same entry). Test:

    echo '{"tool_name":"Agent","tool_input":{"prompt":"x"}}' | python3 $HK/model-guard/model-guard.py; echo $?

Exit 2 with a message: no model named.

## Do not start the same task twice: `dup-worker-check`

    ./install.sh --only dup-worker-check

Merge its `PreToolUse` entry from `~/.claude/hubkit/settings.snippet.json`. Run by hand:

    echo '{"description":"x","prompt":"work on L-0001"}' | python3 $HK/dup-worker-check/dup-worker-check.py; echo $?

Exit 0 means free. It reads Claude Code's own session files; with none, nothing is held.

## One owner per ticket, taken at start and freed at finish: `ticket-lock`

    ./install.sh --only model-guard,report-gate,ticket-lock

It has no hook of its own: `model-guard` takes the lock when a builder starts and `report-gate` frees it when
the builder is allowed to finish. Merge those two `PreToolUse` and `SubagentStop` entries as above. Test by hand:

    python3 $HK/ticket-lock/ticket-lock.py list

A second builder whose description names a held ticket is refused with "write a handoff or wait".
`python3 $HK/ticket-lock/ticket-lock.py release L-0001` frees a lock after a stopped worker.

## One owner per area, with a failure breaker: `lane-lock`

    ./install.sh --only lane-lock
    python3 $HK/lane-lock/lane-lock.py claim my-repo --owner me --pid $$ --reason "first try"
    python3 $HK/lane-lock/lane-lock.py status my-repo
    python3 $HK/lane-lock/lane-lock.py release my-repo --owner me

Report a gate result with `--gate` set to one of `bug-gate`, `playtest` or `other`:

    python3 $HK/lane-lock/lane-lock.py record-fail my-repo --ticket L-0001 --gate bug-gate --detail "login breaks"
    python3 $HK/lane-lock/lane-lock.py record-pass my-repo --ticket L-0001 --gate bug-gate

Two fails in a row trip the breaker (claims then exit 3) until `reset` (see TROUBLESHOOTING).

## Give an idle lane its next ticket: `work-loop`

    ./install.sh --only work-loop      # also installs ledger
    python3 $HK/work-loop/work_loop.py next --lane lane-a

Prints one ticket id (exit 3 = nothing to hand out). It skips tickets owned by another lane, being
worked, waiting on you, done, waiting on an outside event, or offered in the last 24 hours.
Optional lane list: `$HK/state/lanes.json`, format in `patterns/work-loop/README.md`.

## Count how often a build fails its first review: `gate-metrics`

    ./install.sh --only gate-metrics
    python3 $HK/gate-metrics/gate_metrics.py --target 30

Reads the `VERDICT.md` files the `bug-gate` agent writes (format in `patterns/gate-agents/bug-gate.md`) and, if
`model-guard` is installed, its spawn index. Prints the first-review FAIL share; exits 1 only when you pass
`--target` and the share is above it. It never changes anything, and a broken index never stops a spawn.

## Show the reviewer the past-incident checks a diff triggers: `diff-triggers`

    ./install.sh --only diff-triggers
    python3 $HK/diff-triggers/trigger_check.py <repo> <base> <tip>
    python3 $HK/diff-triggers/base_read.py <repo> <base> CLAUDE.md

The first prints one line per row of your own table that the diff matches (copy `triggers.example.txt` to
`$HK/triggers/triggers.tsv` and edit). The second prints a rules file as it was at the base commit, so a change
cannot soften its own review. Both are read only and never block anything.

## Only wake the operator for something new and critical: `wake-gate`

    ./install.sh --only wake-gate
    python3 $HK/wake-gate/wake_gate.py --kind disk-full --source nightly --severity crit --summary "disk 99 percent"

Prints `WAKE` (exit 0) the first time, `LOG` (exit 1) for the same key inside 24 hours. `warn` and `info`
never wake. If its memory file cannot be written it prints the finding on stderr and answers `LOG`.
`work-loop` sends its starvation finding through it when both are installed.

## Get a dead lane's tickets back into the queue: `sweep`

    ./install.sh --only sweep          # also installs ledger
    python3 $HK/sweep/sweep.py sweep               # stale tickets and dead owners; wake-gate decides who is woken
    python3 $HK/sweep/sweep.py orphans --reassign  # dead owner's tickets go back to open and unowned

Needs a live-owner list that your own script writes (`$HK/state/live-owners.txt`, format in `patterns/sweep/README.md`).
A missing, truncated or stale list means "unknown", and unknown never takes a ticket from anyone.
`sweep.py hold --lane NAME --minutes N` protects a lane you know is only quiet.

## See which failures keep coming back: `failure-register`

    ./install.sh --only failure-register
    python3 $HK/failure-register/record_failure.py --job nightly --step build --reason "timed out" --exit 124 || true
    python3 $HK/failure-register/failure_register.py

Each job appends its failures to `$HK/state/failures.jsonl`; the register writes `failure-register.md` listing any
job, step and reason that failed in 3 or more separate incidents (times, ids and paths are ignored when grouping).
One or two failures stay silent. It never opens a ticket, keys and paths are removed from the page, and every run
rewrites `failure-register.tick` so `deadman` can tell it is still running.

## Turn an empty queue into measured work, never filler: `improve`

    ./install.sh --only improve        # pulls in ledger
    python3 $HK/improve/improve.py mine --dry-run
    python3 $HK/improve/improve.py mine

When nothing is queued, `mine` reads `failure-register.md` (and an optional `targets.json` you write) and turns a
repeated failure or a missed target into at most 3 new tickets a day and 5 open at once, each with its evidence and a
done-test. With nothing measured failing it prints `IDLE: nothing measured is failing`. A ticket is closed only by
`improve.py close`, which re-reads the evidence itself; a note such as "looked into it" is refused. An operator-asked
ticket in the ledger always comes first. Every file it reads must live under `$HK`.

To add a bug check, write `$HK/state/projects.json` (name, absolute path, smoke command as a list, timeout), then
`python3 $HK/improve/bug_sweep.py run`. A failing smoke test becomes a candidate on the next `mine`; see
`patterns/improve/README.md`.

## Cap a runaway builder: `turn-cap-builder`

    ./install.sh --only turn-cap-builder

The caps are written inside the agent file, so no settings change. Ask for the
`builder` agent by name. Details and one unverified point: `patterns/turn-cap-builder/README.md`.

## Check disk before a build: `disk-guard`

    ./install.sh --only disk-guard
    python3 $HK/disk-guard/disk-guard.py; echo $?

0 OK, 1 WARN, 2 CRIT.

## Block secrets at commit time: `secret-scan-precommit`

    ./install.sh --only secret-scan-precommit
    git init demo && bash $HK/secret-scan-precommit/install-hook.sh demo

Per repo. It never overwrites a hook you already have.

## Plan first, review last: `gate-agents`

    ./install.sh --only gate-agents

Copies `planner`, `bug-gate`, `playtester`, `bottleneck`, `judge` and `check-checker` into `~/.claude/agents` (skipping any name you
already have). In Claude Code: `run the planner on <idea>`, later `run the bug-gate on this change`;
`run the check-checker on this new test` before you add a check, `run the judge` for a final ruling.

## Memory that survives a new chat: `memory`

    ./install.sh --only memory
    python3 ~/.claude/hubkit/memory/memory_init.py

Creates the three-tier store (router, archive, session logs), installs the `/log` and `/checkpoint` commands
(only if the names are free) and two hooks (PreCompact, SessionStart) that leave a note across a compaction.
Read `~/.claude/hubkit/memory/MEMORY-RULES.md` once. Type `/log` at a pause, `/checkpoint` before you walk away.

## The whole protocol: `kit`

`kit/commands/hub.md` is a generic "lead chat" protocol, with blank
`OPERATOR.template.md` and `CLAUDE.template.md` for your own rules. The installer
copies the command to `~/.claude/commands/`. Read it before using it; it is plain text.

## Tidy new items with plain rules: `groomer`

    ./install.sh --only groomer
    python3 $HK/groomer/groomer.py groom --title "login page is broken" --source operator-chat

Fills a finish line when the item has none, suggests a kind and a grade, and finds repeats among open
machine-made items. No model call. `groom-off` in `$HK/state` is the kill switch, `groom-shadow` counts only.

## Make workers ask in plain words: `card-guard`

    ./install.sh --only card-guard
    python3 $HK/card-guard/worker_card.py --ticket L-0001 --header "Go live today?" --why "it saves a week because the review is done" --option "Ship|Puts the change live" --option "Wait|Keeps the old page" --recommended 0

Refuses cards with ids, hashes, file names, no proof, no consequence, or Yes/No-only options. A worker gets 2 cards per run.

## Prove a finished item is really live: `verify-watch`

    ./install.sh --only verify-watch
    python3 $HK/verify-watch/verify_watch.py check 'VERIFY: url=https://example.com/pricing has="Free plan included"'

A closed one-line grammar plus a host allowlist (`$HK/verify-hosts.txt`). It decides which lines are allowed; it fetches nothing.

## Keep a card board honest: `board-keeper`

    ./install.sh --only board-keeper
    python3 $HK/board-keeper/board_keeper.py --dry-run --json

Moves cards on obvious signals only, forward only, with daily caps, a kill switch and `undo`. Never edits an item.

## Taking a piece out

Delete its folder under `~/.claude/hubkit` and remove its entry from your `settings.json`.
Nothing else depends on it. To remove everything the installer wrote, run
`./install.sh --uninstall` from the repo folder (`--dry-run` first to preview).

The `doctor` is installed with every choice, so `python3 ~/.claude/hubkit/doctor/doctor.py`
always works.
