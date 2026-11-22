# <PROJECT NAME>

Copy to the root of a project as `CLAUDE.md` and fill the `<...>` parts. Keep it short: it is read at the
start of every session and every worker. Delete this line when done.

## What this is

<One paragraph: what the project does and who uses it.>

## Commands

- Build: `<command>`
- Test: `<command>` (must pass before anything ships)
- Run locally: `<command>`
- Deploy: `<command or "not set up yet">`

## Layout

<A few lines: where the code, tests and config live.>

## Rules for workers

- Read your brief first; it names the scope, the report path and the files you own. Do only that.
- Write your report to the path in the brief before you finish, with proof (command output, commit
  hashes), not claims.
- Do not edit files outside your scope. Do not loosen a failing test to make it pass.
- Never print or commit secrets. They live in `<where your secrets live>`.

## Known traps

<Things that have bitten before, one line each. Delete a line when it stops being true.>

## Session memory

<Tier 2 memory for this project: what changed, decisions and why, what comes next. One entry per topic, about
120 words each, newest first, absolute dates. Keep this file under 12,000 characters: when it grows past that,
move the oldest entries to `ARCHIVE.md` in your memory folder. Public repo? Put this section in `CLAUDE.local.md`
(git-ignored) instead.>
