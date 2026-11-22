# Hook behaviour this kit relies on

Checked 2026-10-01 against the Claude Code hooks docs (code.claude.com/docs/en/hooks). Re-check
it when Claude Code changes a lot; a hook that relies on a field that moved fails quietly.

| event | the kit uses it for | payload fields used | how a hook blocks |
|---|---|---|---|
| UserPromptSubmit | ledger banner | `prompt` | exit 2 blocks the prompt (the banner hook never blocks) |
| SubagentStop | report-gate | `agent_id`, `agent_type`, `session_id`, `transcript_path`, `last_assistant_message` | exit 2 keeps the worker running; stderr text goes back to it |
| PreToolUse | model-guard, dup-worker-check, turn-cap | `tool_name`, `tool_input` | exit 2 refuses the call and shows stderr to Claude |
| Stop | work-loop Stop arm (optional, off by default) | `stop_hook_active`, `last_assistant_message`, `agent_id` | print `{"decision":"block","reason":"..."}` and exit 0: Claude keeps going and reads the reason; no output and exit 0 lets it stop |
| PostToolUse | turn-cap (counting only) | `tool_name`, `tool_input` | exit 2 cannot undo the call; the kit never blocks here |

Confirmed in the docs:

- The settings shape is `{"hooks": {"<Event>": [{"matcher": "...", "hooks": [{"type": "command", "command": "..."}]}]}}`.
  A matcher is an exact name, a `|` list (`Agent|Task`), or a regex; leave it out to match all.
- A subagent file can carry its own `hooks:` in its frontmatter. They run only while that
  subagent runs. This is how the builder agent wires its turn cap without touching settings.
- The kit's hooks exit 0 to allow and 2 to block, with the reason on stderr. The docs also
  documents a JSON reply (`decision: block` for SubagentStop and UserPromptSubmit,
  `permissionDecision: deny` for PreToolUse); either style works, the kit uses exit codes.
- Stop: the input carries `stop_hook_active` (true when Claude is already continuing because of a Stop hook).
  The kit does NOT rely on that flag to stop a loop, because a payload can say false every time; the Stop arm
  counts its own continuations in `state/stop-arm.json` and caps them at 3 per lane until a ticket it handed
  out is closed. Re-check this row against the docs before turning the arm on.

Not confirmed (the docs page did not list these as plainly): the exact name of the tool-result
field sent on PostToolUse, and that `transcript_path` for a subagent points at the main transcript
(report-gate therefore finds the worker's own transcript under `~/.claude/projects/*/<session_id>/subagents/`).
Neither blocks the kit: turn-cap reads only `tool_name` and `tool_input`, and report-gate fails open
when it cannot find a transcript for an untyped call. `hubkit doctor` checks the wiring on your machine.

Also not confirmed: that a Stop payload from a subagent carries `agent_id` (the Stop arm treats a payload with `agent_id` as a subagent and lets it stop; a payload without it is treated as the main chat). The arm is off unless you wire it.
