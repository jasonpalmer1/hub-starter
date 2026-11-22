# Lessons

Why the pieces are built the way they are. Short, general, and each one has a
guard in the code.

1. **A claim is not proof.** "Done" from a helper (or from you, tired) means
   nothing until something checkable is attached: a file, a commit, a test
   output. The ledger refuses `done` without it, and a claim about a number does
   not count.

2. **A rule that lives only in a document is not a rule.** If it matters, a script
   enforces it: a hook refuses, a check alarms. Then test the guard by breaking it
   on purpose (every `test.sh --mutate` does this).

3. **A check that cannot see must not say "fine".** An unreadable disk is CRIT, a
   corrupt record is an alarm, a git that errors blocks the commit. Silence on
   failure is a costly bug.

4. **But a safety hook must never stop real work by accident.** Hooks that fail on
   their own bug let the call through (model-guard, turn-cap), except the ones
   whose whole job is to fail closed (the secret scanner, and the report gate on its own bugs). Each pattern says which
   it is.

5. **A watcher cannot live inside the thing it watches.** If the check runs in the
   job, a dead job cannot report itself. Heartbeats are written by the job, checked
   by a different scheduler entry.

6. **Beat on success only.** A heartbeat written before the work, or on any exit, says
   "alive" about a job that is failing.

7. **One owner per area at a time.** Two helpers in the same folder overwrite each
   other. A lock with a real owner, a dead-owner cleanup, and an escape hatch that
   writes down who used it and why.

8. **Two failures in a row means a person looks.** A breaker stops a loop of "fix,
   fail, fix, fail". Only a human reset reopens it; a later pass does not.

9. **Plan first, review last, by someone who did not build it.** The reviewer reads
   the exact change, runs the user's real verb on real data, and is allowed to say no.
   A gate that cannot block is only a suggestion.

10. **Cap the runaway.** Every helper gets a tool-call budget, a nudge near the end,
    and a hard stop that still lets it write its handoff. A fresh helper continues
    from the handoff; nobody resumes a worn-out one.

11. **Never overwrite, never edit what is yours.** An installer that replaces your
    files or rewrites your settings costs you trust the first time it surprises you.
    This one copies new files, keeps yours, and prints the settings text for you to
    merge.

12. **Reports are paths, not promises.** Name the exact file in the brief. Accept only
    a file created during the run with real text in it. "Any recent file" lets an
    unrelated helper's output pass.

13. **Share only what a stranger needs.** Scan everything before it leaves the
    machine: secrets, personal paths, names, addresses. Prefer an allowlist of what
    ships (`MANIFEST.txt`) to a list of what must not.

14. **Surface the open question yourself.** Keep waiting-on-you items in a file that
    outlives the chat and raise them yourself, as a short question with a simple answer.
