# Pattern matrix

Pattern, files, tests, what it needs. Test commands run from the repo folder; each runs in
a scratch home folder and touches nothing of yours. "Core" means `./install.sh` installs it
without flags.

| pattern | core | files (in `patterns/<name>/`) | test (count) | hook you merge | needs |
|---|---|---|---|---|---|
| ledger | yes | `ledger`, `ledger_lib.py`, `ledger-banner-hook.py` | `bash patterns/ledger/test.sh` (66) | UserPromptSubmit (optional banner) | nothing |
| report-gate | yes | `report-gate.py` | `bash patterns/report-gate/test.sh` (36) | SubagentStop | nothing |
| deadman | yes | `deadman.py` | `bash patterns/deadman/test.sh` (50) | none (call from jobs) | nothing |
| doctor | yes | `doctor.py` | `bash patterns/doctor/test.sh` (67) | none | nothing (reads deadman and ledger files if present) |
| secret-scan-precommit | yes | `secret-scan.py`, `install-hook.sh` | `bash patterns/secret-scan-precommit/test.sh` (50) | none (git pre-commit, per repo) | git |
| gate-agents | yes | `planner.md`, `bug-gate.md`, `playtester.md`, `bottleneck.md`, `judge.md`, `check-checker.md` | `bash patterns/gate-agents/test.sh` (8) | none | nothing (pairs well with report-gate) |
| memory | yes | `memory_init.py`, `pre_compact_stub.py`, `compact_pickup.py`, `log.md`, `checkpoint.md`, `MEMORY-RULES.md`, `MEMORY.template.md`, `ARCHIVE.template.md`, `conversations_index.template.md` | `bash patterns/memory/test.sh` (15) | PreCompact, SessionStart | nothing (pairs well with ledger) |
| model-guard | no | `model-guard.py`, `gate_round_cap.py`, `selfcheck_admit.py` | `bash patterns/model-guard/test.sh` (57) | PreToolUse (Agent, Task, Workflow) | nothing |
| gate-metrics | no | `gate_metrics.py` | `bash patterns/gate-metrics/test.sh` (15) | none (reads what bug-gate and model-guard write) | nothing (pairs well with model-guard and gate-agents) |
| diff-triggers | no | `trigger_check.py`, `base_read.py`, `triggers.example.txt` | `bash patterns/diff-triggers/test.sh` (20) | none | python3, git |
| dup-worker-check | no | `dup-worker-check.py`, `dup-worker-hook.py` | `bash patterns/dup-worker-check/test.sh` (35) | PreToolUse (Agent, Task) | nothing |
| ticket-lock | no | `ticket_lock.py`, `ticket-lock.py` | `bash patterns/ticket-lock/test.sh` (49) | none (library called by model-guard and report-gate) | model-guard and report-gate (pairs well with ledger) |
| lane-lock | no | `lane-lock.py` | `bash patterns/lane-lock/test.sh` (74) | none | nothing (pairs well with gate-agents) |
| work-loop | no | `work_loop.py`, `stop_arm.py` | `bash patterns/work-loop/test.sh` (63) | none (the Stop arm is optional: `kit/settings.snippet.loop.json`) | ledger (pairs well with lane-lock) |
| wake-gate | no | `wake_gate.py` | `bash patterns/wake-gate/test.sh` (27) | none (call from jobs; work-loop uses it) | nothing |
| sweep | no | `sweep.py` | `bash patterns/sweep/test.sh` (28) | none (call `sweep` and `orphans --reassign` from a schedule) | ledger (pairs well with wake-gate and work-loop) |
| failure-register | no | `failure_register.py`, `record_failure.py` | `bash patterns/failure-register/test.sh` (51) | none (call from cron; jobs call the recorder) | nothing |
| improve | no | `improve.py`, `bug_sweep.py` | `bash patterns/improve/test.sh` (69) | none (call `improve.py mine` and `bug_sweep.py run` from a schedule or when a lane runs dry) | ledger (pairs well with failure-register) |
| turn-cap-builder | no | `turn-cap.py`, `builder.md` | `bash patterns/turn-cap-builder/test.sh` (27) | none (hooks inside `builder.md`) | nothing |
| disk-guard | no | `disk-guard.py` | `bash patterns/disk-guard/test.sh` (15) | none | nothing |
| groomer | no | `groomer.py` | `bash patterns/groomer/test.sh` (15) | none (call `groom` when an item is filed) | nothing (pairs well with ledger and verify-watch) |
| card-guard | no | `card_guard.py`, `worker_card.py` | `bash patterns/card-guard/test.sh` (16) | none (workers call `worker_card.py`) | nothing (pairs well with report-gate) |
| verify-watch | no | `verify_watch.py` | `bash patterns/verify-watch/test.sh` (11) | none | nothing (pairs well with groomer) |
| board-keeper | no | `board_keeper.py` | `bash patterns/board-keeper/test.sh` (21) | none (call from a schedule) | nothing (pairs well with ledger and verify-watch) |
| schedule-examples | no | `crontab.example.txt`, `launchd-job.plist.example`, `launchd-deadman-check.plist.example`, `crontab.loop.example.txt`, `windows-keepalive.task.xml`, `notify-windows.example.sh` | `bash patterns/schedule-examples/test.sh` (19) | none | deadman |

Every pattern folder also has `README.md` (guide), `pattern.json` (what the installer reads)
and `test.sh` (add `--mutate` to break each guard and prove a test notices).

## Outside `patterns/`

| piece | files | test |
|---|---|---|
| kit | `kit/commands/hub.md`, `kit/templates/*.template.md`, `kit/settings.snippet.json`, `kit/HOOKS-VERIFIED.md` | `bash kit/test.sh` (6) |
| installer | `install.sh`, `tools/install.py` | `bash tools/test-install.sh` (30) |
| leak scan | `tools/scan.py`, `tools/prepush-check.sh` | `bash tools/test-scan.sh` |

## Which hook events are used

| event | when it fires | used by |
|---|---|---|
| UserPromptSubmit | you send a message | ledger banner |
| PreToolUse | before a tool call | model-guard (with ticket-lock), dup-worker-check |
| SubagentStop | a helper tries to finish | report-gate (frees ticket-lock) |
| Stop | a chat tries to stop | work-loop Stop arm (optional, off by default) |

What was checked about each, and what was not: `kit/HOOKS-VERIFIED.md`.

## Run everything

    for t in patterns/*/test.sh kit/test.sh tools/test-install.sh tools/test-scan.sh; do bash "$t" || echo "FAILED: $t"; done
