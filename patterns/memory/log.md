---
description: Summarize the current session into the persistent conversation log
---

Write a concise summary of the current session to the conversation log.

Memory folder: run `python3 ~/.claude/hubkit/memory/memory_init.py --print-dir` and use that path.
(If the folder does not exist yet, run `python3 ~/.claude/hubkit/memory/memory_init.py` once.)

Steps:
0. **Read the neighbors before writing.** List `conversations/` newest first and read the last 2 or 3 entries.
   If this session continues one of them, say so in the log and name that file; do not fork the story silently.
   Read `conversations_index.md` and audit it against the disk in BOTH directions: every line points at a real
   file, and every file has a line. Fix what you can now. For a log you have not read, index it as
   "unsummarized"; never invent a hook to make the index look clean. Check the caps (40 lines in the index and in
   `MEMORY.md`) before adding, and ask what this entry supersedes: archive that now.
1. Pick a slug from the session's main topic. The file is `conversations/YYYY-MM-DD-<slug>.md` with today's date.
2. Write it, **under 30 lines**: what was asked, what was done or decided, files touched, open threads. Facts that
   should live longer go into a real memory file (and link to it), not only here.
3. End with **Resume state** (open threads, exact next steps, anything mid-flight: running agents, undeployed
   changes, unpushed commits) and **Pickup prompts**: one fenced block per open thread, second person, pasteable
   into a fresh session with no edits. Each names the folder and branch to own, the exact next action, what NOT
   to touch, and what closes the ticket.
4. **Footer, last line:** `<!-- logged-session: <session id> -->`. The id is the name of the newest `.jsonl`
   file (no extension) in your own `~/.claude/projects/<folder slug>/`. Check that it matches a real file there.
   Appending to an existing log: leave older footers alone and add your own as the new last line.
5. Append one line to `conversations_index.md`:
   `- [YYYY-MM-DD Title](conversations/YYYY-MM-DD-<slug>.md) - one-line hook`
6. If a durable fact, preference or correction surfaced, save it to its own memory file (see
   `MEMORY-RULES.md` in the memory pattern folder) and add one router line to `MEMORY.md`.
7. **Verify before you claim it.** `ls -la` the conversations folder: your file exists, dated today, written
   during this session (a neighbor from another session is not yours). Confirm the index line landed and its
   link resolves. Confirm any memory file from step 6 is on disk. Fix and re-check if anything fails.
8. Only then end your reply with one line naming the file: `Logged - <filename> on disk, index line resolves.`

Never ask the user to /clear; auto-compact handles context. Keep the log terse: "what happened when", not a
transcript.
