# hub-starter

Small, separate tools that make an AI coding assistant (Claude Code) trustworthy
when you hand it real work. Take one piece or take them all.

**Headline: a scheduled loop can keep the queue moving while you are away.** It writes the next ticket to an
idle lane's inbox file, two lanes never get the same one, and when nothing is left it looks only at measured
problems and otherwise says `IDLE: nothing measured is failing`. It needs a machine that stays awake and a chat
that reads its inbox (or the optional Stop arm). Set it up in `docs/ALWAYS-RUNNING.md`.

## The problems it fixes

- "Done" with nothing to show for it. The `ledger` will not close an item without proof.
- A helper that stops without writing its report. `report-gate` refuses the stop.
- A scheduled job that quietly died. `deadman` alarms when its heartbeat goes stale.
- "Is my setup even working?" `doctor` checks and prints the exact fix.
- A secret about to be committed. `secret-scan-precommit` blocks the commit.
- Two helpers on the same task, or a helper that runs forever. `dup-worker-check`,
  `lane-lock`, `turn-cap-builder`. Plus gate agents that plan first and review last.

Full list: `docs/PATTERN-MATRIX.md`.

## 5-minute install

You need python3 (3.9 or newer), macOS or Linux, and Claude Code.
On Windows 11, use WSL2 and follow `docs/WINDOWS.md` instead.

    cd hub-starter
    ./install.sh --dry-run     # shows what would be copied, writes nothing
    ./install.sh               # copies the core pieces into ~/.claude/hubkit
    python3 ~/.claude/hubkit/doctor/doctor.py

A fresh install shows one FAIL from the doctor, about hooks not being wired in
your settings. That is expected: the installer never edits your `settings.json`.
It prints the entries to merge and saves them at
`~/.claude/hubkit/settings.snippet.json`. Merge them yourself, run the doctor
again, and the FAIL turns into PASS.

What the installer writes, and nowhere else:

- `~/.claude/hubkit/` (everything lives here, including `settings.snippet.json`, `templates/`,
  `HOOKS-VERIFIED.md` and a small `state/` folder with an install record and a local log).
- `~/.claude/agents/*.md`, only for the patterns that ship an agent (`gate-agents`,
  `turn-cap-builder`), and only if that file name is free.
- `~/.claude/commands/hub.md`, only for the default or `--all` install (or `--only ... --with-hub-command`),
  and only if that file name is free.
- Never your `settings.json`. The `doctor` is always installed, so the first troubleshooting step works for any choice.

To remove exactly what was installed, run `./install.sh --uninstall` (add `--dry-run` to preview).
It deletes only files it created and that you have not edited, then tidies empty folders.
Remove the hook entries you merged into `settings.json` yourself.

Other install choices: `./install.sh --only ledger,deadman`, `./install.sh --all`,
`./install.sh --prefix hk-` (renames agent and command files you copy into `~/.claude`).

New to the words (lane, hub, lead chat, operator, brief, breaker)? See `docs/GLOSSARY.md`.

Then do `docs/FIRST-TICKET.md`: one real task from start to finish.

## What it never does

- Never overwrites a file you already have. A different file is kept and reported.
- Never edits `settings.json`. You merge hook entries yourself.
- Never sends anything anywhere. No network calls, no accounts, no telemetry.
- Never deletes your data. `./install.sh --uninstall` removes only what the installer wrote; your ledger and reports are kept.
- Never stores secrets. The secret scanner shows file and line, not the value.

## Where to read next

| you want | read |
|---|---|
| one task end to end | `docs/FIRST-TICKET.md` |
| work that keeps going while you are away | `docs/ALWAYS-RUNNING.md` |
| the measure-then-fix method and plan template | `docs/IMPROVEMENT-LOOP.md` |
| one piece only | `docs/ADOPT-ONE-PIECE.md` |
| which file belongs to which piece | `docs/PATTERN-MATRIX.md` |
| why it is built this way | `docs/LESSONS.md` |
| something is not working | `docs/TROUBLESHOOTING.md` |
| what a word means | `docs/GLOSSARY.md` |

## Check the repo before sharing it

    bash tools/prepush-check.sh --generic-only
    bash tools/test-scan.sh --mutate

Every shipped file is listed in `MANIFEST.txt`. The scan covers the working tree and every file and
commit message in the whole git history, so a leak that was committed and later deleted still fails.
History blobs a person reviewed and accepted are listed by exact hash in `tools/history-allow.txt`.
By default the history scan judges only commits after the reviewed baseline pinned in `tools/history-base.txt`
(none is pinned yet, so it judges all history); `bash tools/prepush-check.sh --generic-only --full` scans ALL history and must
be green before the repo is made public.

Commits must be authored and committed as `hub-starter`. Run `bash tools/install-hooks.sh` once in every clone
(including scratch clones): it sets that identity and installs a commit and merge-commit guard that refuses any
other. `tools/identity-allow.txt` is empty and frozen (`tools/test_scan.py` pins it).

Licensed under the MIT License, see `LICENSE`.
