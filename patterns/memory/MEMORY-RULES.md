# Memory conventions: three tiers, one rule per tier

The point: a new chat knows what the last one knew, without you retyping it, and without loading a
mountain of old text every time.

## The three tiers

| tier | file | loaded | what goes in | cap |
|---|---|---|---|---|
| 1 router | `MEMORY.md` in the memory folder | every session, automatically | standing rules and "who I am": one line per theme, each with a link to a detail file | 40 non-blank lines |
| 2 session memory | `## Session memory` section of the project's `CLAUDE.md` | every session that starts in that project | what changed and what is next for THAT project: decisions and why, traps, state | about 120 words per entry, 12,000 characters per file |
| 3 archive and logs | `ARCHIVE.md`, `conversations_index.md`, `conversations/*.md` | only when you or Claude look | everything older, plus one log per session | none (but keep the index honest) |

Detail files (one fact per file, linked from the router) sit next to `MEMORY.md`. Each starts with:

    ---
    name: short title
    description: one line, specific enough to decide relevance from
    type: user | rule | project | pointer
    ---

`feedback` files say what to do or avoid AND why (the why lets Claude judge edge cases). `project` files hold
state that changes; convert relative dates ("Thursday") to absolute dates when you save them.

## Rules

1. Search before you write. Update the existing entry; never add a duplicate.
2. A router line is a pointer, not the memory. If it needs more than one line, put the detail in its own file.
3. Over a cap means archive, in the same edit: move the oldest or least used entry to `ARCHIVE.md` with its link.
   Never delete to get under a cap.
4. Do not save what the code or git history already shows. Save the reason, the surprise, the correction.
5. Never save secrets, tokens or passwords. Memory is plain text on disk.
6. A memory is a claim about the past. Before acting on a file path, a name or a number from memory, check
   it still exists.
7. Public repo: put session memory in `CLAUDE.local.md` (git-ignored), not in `CLAUDE.md`.

## Session logs (`/log`, `/checkpoint`)

- One file per session: `conversations/YYYY-MM-DD-<slug>.md`, under 30 lines: what was asked, what was
  done or decided, files touched, open threads, then **Resume state** and **Pickup prompts**.
- The last line is a footer `<!-- logged-session: <session id> -->`. The session id is the name of the newest
  `.jsonl` file (without the extension) in `~/.claude/projects/<folder slug>/`. A log appended by a later session
  keeps the older footer and gets a new footer line of its own.
- Read the last 2 or 3 logs BEFORE writing (is this a continuation?), and check the result AFTER writing: your file
  exists with today's date, the index line points at it, and every log file has an index line.
- `/checkpoint` = `/log` plus saving state to disk and a process sweep. Use it before you walk away or when a chat
  has grown long. Auto-compact handles context by itself; you never need to type /clear.

## The pre-compact stub

`pre_compact_stub.py` (PreCompact hook) writes a tiny file per session saying whether that session was logged.
`compact_pickup.py` (SessionStart hook) shows it to the next session. Both exit 0 whatever happens.
