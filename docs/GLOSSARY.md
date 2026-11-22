# Glossary

Plain meanings for the words the guides use.

- **hub / lead chat**: the one Claude Code conversation you talk to. It files your asks,
  hands work to helpers, and checks their results. It can be closed and reopened; the
  ledger keeps the state. `kit/commands/hub.md` is a ready-made protocol for it.
- **operator**: you, the person directing the work. `OPERATOR.template.md` is a blank page
  where you write how you want the assistant to work with you.
- **helper / worker / typed helper**: a short-lived Claude Code subagent started for one job
  (planner, builder, bug-gate, playtester). It writes a report and stops.
- **brief**: the message you give a helper: the ticket id, the exact scope, the files it
  owns, and the exact path where its report must be written.
- **ticket**: one line in the `ledger`, with an id like `L-0001`, that is open until
  something checkable proves it done.
- **lane**: an area of work that only one owner should touch at a time, usually one repo or
  one folder. `lane-lock` hands out one claim per lane.
- **gate**: a check that can say no before work ships: a plan, a bug check, a playtest.
- **breaker**: in `lane-lock`, a stop that trips after two failed gates in a row (set
  `LANE_BREAKER_THRESHOLD` to change it). A person must `reset` it after reading the change.
- **heartbeat**: a small file a scheduled job writes when it succeeds. `deadman` raises an
  alarm when one goes stale.
- **hook**: a command Claude Code runs automatically at an event (before a tool call, when a
  helper stops). Hooks are switched on by entries in your `settings.json`.
- **proof**: a file, a commit, a link, or real command output attached to a ticket.
