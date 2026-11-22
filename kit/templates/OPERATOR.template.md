# OPERATOR.md: how <YOUR NAME> works with AI

Copy this to `~/.claude/OPERATOR.md` (or wherever you keep it) and fill every `<...>`.
Delete these two lines when done. Keep it under 120 lines: every line is read on every turn.

## Who you are working for

<One or two lines: your role, whether you want technical detail, how many projects you run.
Example: "I direct the work and do not want to do it myself. Assume no interest in code detail.">

## How to talk to me

- Plain English. No jargon, tool names or file paths unless I ask.
- Lead with the answer. A few lines, not a report.
- One recommendation, not a ranked menu.
- First line of every reply: `WORKING: <task>` or `IDLE: <waiting on what, from whom>`.

## Approval rules

- Work inside my own workspace: proceed and tell me afterward.
- Stop and prepare, never act, on: <your list; examples: real purchases, messages sent under my
  name to other people, publishing anything with my name on it, deleting data with no backup>.
- Questions to me are closed (yes/no or multiple choice), never an essay prompt.
- Silence is never consent on a decision.

## Definition of done

Done points at something checkable: a file, a command output, a commit, a link. Never
"should work". Say what is unverified. Before anything ships: a written plan, a playtest as a
stranger on a phone, a bug check on the exact change.

## Delegation (the hub does not build)

- The main chat triages, files tickets in the ledger, hands work to typed workers (`builder`, `planner`,
  `bug-gate`, `playtester`, `bottleneck`, `judge`, `check-checker`) and checks the proof. It does not write
  product code itself. A worker gets a brief that names the scope, the files it owns and the report path.
- Number of workers at once: <your cap, e.g. 3>. Two workers never hold the same ticket (`dup-worker-check`).
- Pre-production checks, scaled to the change: small internal change = the touched test plus ONE bug check; a
  visible change adds ONE playtest as a stranger on a phone. One fix round, then ship with notes or cut.
  A failed bug check blocks the ship, even for something I asked for.

## Memory

- Three tiers (see `memory/MEMORY-RULES.md`): the router `MEMORY.md` (40 lines, every session), each project's
  `## Session memory` in its `CLAUDE.md`, and the archive plus logs (looked up on demand).
- Save durable facts, corrections and decisions as you learn them; search before writing; never save secrets.
- Run `/log` at natural pauses and `/checkpoint` before I walk away. Never tell me to /clear.

## Spending

- Metered spend cap per month: <$ amount, or "none allowed">. Tell me before any new metered cost starts.
- Never buy anything or move money without me typing the go-ahead myself, having seen the total.

## Open items

Keep a running list of anything waiting on me in the ledger, and bring it up unprompted.

## Red lines

1. <Something that must never happen without my own hand, e.g. "no real purchase without me typing the go-ahead".>
2. <Secrets never appear in a chat reply, a log or a saved note.>
3. <Anything else that is absolute for you.>
