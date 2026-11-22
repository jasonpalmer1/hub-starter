# model-guard

Helpers that quietly run on the wrong model.

## What it fixes

When your chat starts a subagent without saying which model it should use, the
helper takes whatever model the main chat is on that day. Switch the main chat
to a bigger model and every helper gets slower and costs more, with no sign
anything changed. This hook refuses any Agent or Task call that does not name
its model, so the choice is always deliberate.

Any model is fine, including a bigger one than the main chat. Only "unset" and
"inherit" are refused.

## What you need

- python3, 3.9 or newer
- nothing else; no other pattern is required

## Install

By hand:

    mkdir -p ~/.claude/hubkit/model-guard
    cp model-guard.py ~/.claude/hubkit/model-guard/

Then add this to your `settings.json` yourself (this pattern never edits it):

    {"hooks": {"PreToolUse": [{"matcher": "Agent|Task|Workflow",
      "hooks": [{"type": "command",
       "command": "python3 ~/.claude/hubkit/model-guard/model-guard.py"}]}]}}

## What you will see

When Claude forgets the model, the call is refused and Claude is told why. It
re-issues the call with a model and carries on. You do not have to do anything.

## Options

- `MODEL_GUARD_ALLOWED=haiku,sonnet` also refuses any model outside the list
  (case does not matter). Unset means any named model is allowed.
- Workflow calls pass through untouched (the old `agent(...)` / `model:` count
  warning was cut: a text match could not tell which call missed its model).
- Decisions are logged to `~/.claude/hubkit/state/model-guard.log`
  (or `MODEL_GUARD_LOG`).
- Allowed `builder` and `bug-gate` spawns are also appended, one JSON line each, to
  `~/.claude/hubkit/state/gate-spawns.jsonl` (or `GATE_SPAWNS_LOG`) for the `gate-metrics` pattern: time, session,
  type, model, ticket ids (from the description, else the first 400 characters of the prompt), slice, description.
  This is observability only: an unwritable, symlinked or corrupt index never blocks or changes a spawn.
- Report path (optional): with the `report-gate` pattern installed beside this one (or under
  `~/.claude/hubkit/report-gate`), a `builder`, `bug-gate` or `playtester` brief must carry a usable report path or the
  line `Report: none`, the same rule report-gate applies when the worker stops. A `Report:` line with `..` or a control
  character is refused too. A `HANDOFF-FROM:` line exempts the brief. Without report-gate nothing changes.
  `MODEL_GUARD_REPORT_CHECK=0` switches it off.
- Failure modes: a spawn (Agent or Task) fails closed on anything the hook cannot read or classify (an unreadable or
  non-object payload, a non-string `tool_name`, a non-object `tool_input`, or a crash mid-check): it is refused with a
  message to re-send it as a normal Agent call. Any other tool (Workflow and the rest) fails open if the hook itself
  breaks. A block always exits 2, even when stderr is broken. An unwritable log never changes a decision.

## Test it

    bash test.sh            # 57 tests in a scratch HOME
    bash test.sh --mutate   # also breaks each guard and checks a test fails

## Lift just this piece

Copy `model-guard.py` and wire the hook. Nothing else in this repo is needed.
