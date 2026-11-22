---
description: Closing rites on demand - log, save state to disk, then keep working. Never asks you to /clear.
argument-hint: "(no args - run the rites for this session now)"
---

# /checkpoint: make this chat disposable, right now

The user wants this session's state OFF the chat and ON the disk, cheaply. Do all of it, in order, without
asking questions:

0. **Read the neighbors first.** List the memory `conversations/` folder (path from
   `python3 ~/.claude/hubkit/memory/memory_init.py --print-dir`), read the last 2 or 3 entries, `MEMORY.md`, and the
   tail of the project's `## Session memory`. Audit the index against the disk in both directions. Check the
   caps before adding. Say whether this session continues an earlier one.
1. **Extraction sweep.** Scan the session for durable facts (people, decisions and why, corrections, project
   state changes, patterns). File each in the right tier: a router line plus a detail file, or an entry (about 120
   words) in the project's `## Session memory`. Update an existing entry rather than adding a duplicate.
2. **Log.** Run `/log` (or, if unavailable, write the dated summary and index line by hand).
3. **Disk state.** Update the ledger for anything this session started or landed
   (`python3 ~/.claude/hubkit/ledger/ledger note L-N "..."` or a status change). Commit and push finished, verified
   repo work; commit work in progress to a branch. Uncommitted work must not exist after a checkpoint.
4. **Cost line.** One line in chat: rough size of this session and its biggest spend.
5. **Verify the rites landed** before you say so:
   - a log for THIS session exists in `conversations/`, dated today, and the index line resolves;
   - `git status --porcelain` in every repo you touched shows nothing of yours;
   - `git log origin/<branch>..HEAD` shows nothing of yours unpushed;
   - anything built in a scratch folder that you still need is copied somewhere durable;
   - **process sweep:** `ps -ax -o pid,etime,command | grep -E "node |python|http.server|vite|chrome-headless"` and stop
     anything THIS session started (check by process, not just by port).
   If any check fails, fix it and re-check.
6. **Keep working.** Never end by telling the user to /clear or /compact; auto-compact does that. Continue with
   the next ticket in the same reply, or hand over with the pickup prompt.

Steps 0 and 5 are one rule: read the neighbors BEFORE writing, verify the write AFTER. Skipping either means
the log is a guess.
