# turn-cap-builder

A builder worker that cannot run forever.

## What it fixes

A subagent given a big job can burn hundreds of tool calls, run out of
context, and die with nothing written down. This pattern gives you a
`builder` agent with a budget:

- around call 80 its own instructions tell it to wrap up,
- at call 90 a hook nudges it ("write handoff.md now"),
- at call 115 every tool is refused except writing its handoff and report.

The builder writes `handoff.md` (done / next / files / commands / open
questions). A fresh builder continues from that file. Builders are never
resumed.

## What you need

- python3, 3.9 or newer
- macOS or Linux
- nothing else; no other pattern is required (it pairs well with `report-gate`)

## Install

By hand:

    mkdir -p ~/.claude/hubkit/turn-cap-builder ~/.claude/agents
    cp turn-cap.py ~/.claude/hubkit/turn-cap-builder/
    cp builder.md ~/.claude/agents/builder.md     # skip if you already have one

The two hooks are written inside `builder.md` (in its frontmatter), so you do
not touch `settings.json`. They only fire for this agent. If you already have a
`builder` agent, copy the `hooks:` block from this file into yours instead.

Put the file in your user folder, `~/.claude/agents`, as above. Hooks written in
an agent's frontmatter are skipped, with no error, when the agent lives in a
project folder (`<project>/.claude/agents`) that Claude Code has not yet been
told to trust. A skipped hook means the cap silently does not apply.

## Use it

Ask for it by name in a brief to the Agent tool: subagent type `builder`. In
the brief, name the report path and what the builder owns.

## What it does exactly

- Counts every tool call per worker, in a small file under
  `~/.claude/hubkit/state/turns/` (safe when calls run in parallel).
- Past the cap, only Write or Edit of a file inside `~/.claude/hubkit/reports/`
  is allowed (symlinks and `..` tricks do not get out of that folder).
- `bug-gate`, `playtester` and `planner` agents are never capped; the main chat
  has no worker id and is never counted.
- Prints nothing on the normal path, so it costs no tokens per turn.
- If the hook itself breaks, it lets the call through. It must never stop real work.

Change the numbers with `TURN_CAP_NUDGE` and `TURN_CAP_BLOCK`.

Not verified here: whether Claude Code shows the 90-call nudge to a subagent.
The 115-call block does not depend on it. To check the nudge, run a builder
through ~100 trivial calls and look in its transcript for "TURN CAP NUDGE".

## Test it

    bash test.sh            # 32 tests in a scratch HOME (about 30 seconds)
    bash test.sh --mutate   # also breaks each guard and checks a test fails

## Lift just this piece

Copy `turn-cap.py` and `builder.md`. Nothing else in this repo is needed.
