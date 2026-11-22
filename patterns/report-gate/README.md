# report-gate

Workers that stop without leaving a report you can check.

## What it fixes

When you hand work to a Claude Code subagent (a "worker"), its last message
says "done" whether or not anything real was produced. This hook runs when a
worker tries to stop. It refuses until the worker has written the report file
named in its brief, and the refusal text goes straight back to the worker so it
can fix the problem itself.

## What you need

- python3, 3.9 or newer
- macOS or Linux (Windows: use WSL)
- nothing else; no other pattern is required

## Install

By hand:

    mkdir -p ~/.claude/hubkit/report-gate
    cp report-gate.py ~/.claude/hubkit/report-gate/

Then add this to your `settings.json` yourself (this pattern never edits it):

    {"hooks": {"SubagentStop": [{"hooks": [
      {"type": "command",
       "command": "python3 ~/.claude/hubkit/report-gate/report-gate.py"}]}]}}

## How a brief should look

Put the report path in the brief, as an exact file under a `reports` folder:

    Read ~/.claude/hubkit/reports/plan.md for background.
    Build the thing, then write your report to
    ~/.claude/hubkit/reports/hello-build.md

or, for a worker that needs no report, a line of its own:

    report: none

## What it checks

1. The brief names a path (written `~/...`, `$HOME/...`, `$HUBKIT_HOME/...`
   or absolute, ending `.md`, with a `reports` folder in it).
2. That exact file was created after the worker started (an old file that was
   merely touched does not count) and has real text in it.
3. If the brief names several paths, the one right after words like "write",
   "save", "report to" is the deliverable; background reading is ignored.

Never "any recent file": an unrelated worker's fresh file cannot pass this one.

Every worker also has a fallback report,
`~/.claude/hubkit/reports/workers/<agent_id>.md`. The refusal text names it, so a
worker whose brief is broken still has a safe place to put its proof.

## When the brief names no path

The worker is refused once and let through the second time. A worker cannot
rewrite its own brief, so refusing forever would only make it loop. The brief is
saved to `~/.claude/hubkit/state/report-gate-badbriefs.log`: that file is a to-do
list for fixing whatever dispatches your workers.

## Failure rules

- Empty or unreadable input: fails open (lets the stop through).
- Any other bug inside the hook: fails closed (refuses, and logs why), so a
  bug cannot silently switch the gate off.
- Blocks are logged to `~/.claude/hubkit/state/report-gate.log`; set
  `REPORT_GATE_LOG_ALL=1` to log allowed stops too.

## Test it

    bash test.sh            # 30 tests in a scratch HOME
    bash test.sh --mutate   # also breaks each guard and checks a test fails

## Lift just this piece

Copy `report-gate.py` and wire the hook. Nothing else in this repo is needed.
