# Start here (for a complete beginner)

This kit gives Claude Code a working system: a ticket list that will not accept "done" without proof, helpers
(agents) with clear jobs, reviewers that check the work, and a memory that survives a new chat. You do not
need to know how to code. You need to be able to copy and paste into a terminal.

On Windows 11? Use `docs/WINDOWS.md` instead of the steps below; it runs everything inside WSL2.

## 1. Install the two things you need

1. **Claude Code.** Follow the install steps at https://code.claude.com/docs (one command), then type `claude`
   in a terminal once and sign in.
2. **Python 3.9 or newer.** On a Mac, type `python3 --version` in the Terminal app. If it prints a number, you
   are done. If not, install it from https://www.python.org/downloads/.

## 2. Get the kit and install it

Get the kit folder from whoever shared it (a zip file or a git link). If it is a zip, unzip it. If it is a
git link, run `git clone <that link>`. Then in the Terminal app (copy each line, press Enter), going into the
folder named `hub-starter`:

    cd hub-starter
    ./install.sh --dry-run

The dry run only prints what would happen. It writes nothing. If it looks fine:

    ./install.sh --all --wire-settings

What this does, in plain words:

- **Saves a copy of your current `~/.claude` folder first**, to a folder named `.claude.backup-<date>` in your
  home folder. If anything ever looks wrong, your old files are there.
- Puts the kit in `~/.claude/hubkit`, the helpers in `~/.claude/agents`, and the `/hub`, `/log` and
  `/checkpoint` commands in `~/.claude/commands`. It never replaces a file you already have.
- Creates your memory folder (a short router file plus folders for logs).
- Adds the kit's hooks to `~/.claude/settings.json`. It only adds; every setting and hook you had is kept.

Check that it works:

    python3 ~/.claude/hubkit/doctor/doctor.py

It lists what is healthy and prints the exact fix for anything that is not. To remove everything the installer
wrote: `./install.sh --uninstall`.

## 3. Fill in two short files

Open `~/.claude/hubkit/templates/OPERATOR.template.md` in any text editor, replace every `<...>` with your own
answers (who you are, how you want replies, what must never happen without you), and save it as
`~/.claude/OPERATOR.md`. Then copy `CLAUDE.template.md` from the same folder to the top folder of a project as
`CLAUDE.md` and fill that in too. Ten minutes, once.

## 4. Your first ticket

Open Claude Code in your project folder and type `/hub`. The chat becomes your "hub": it files what you ask for
as tickets, hands the work to helpers, and checks the proof. Then ask for something small, for example:
"add a hello file to this project". Watch it:

    python3 ~/.claude/hubkit/ledger/ledger list

shows your tickets. A ticket only closes when the helper points at proof (a file, a test result, a commit). Try
closing one with no proof and see it refuse. The slower walk-through, step by step, is `docs/FIRST-TICKET.md`.

## 5. Daily use

| when | do this |
|---|---|
| start of a day | open Claude Code, type `/hub`, it reads the ticket list and tells you what is open |
| you have an idea | tell the hub; it asks the `planner` helper for a written plan first |
| work is built | it asks `bug-gate` to review the exact change (and `playtester` for anything people see) |
| a pause, or before you walk away | type `/checkpoint` (it saves where you stopped to your memory folder) |
| a new chat | just start; the memory router loads by itself and the hub reads the ticket list |
| you want a fact remembered | say "remember this"; it files one short note and a one-line pointer |
| something seems off | `python3 ~/.claude/hubkit/doctor/doctor.py` |

Never type `/clear` to "save context"; the memory and the compaction note handle that.

## 6. Keep going while you are away (optional, later)

Read `docs/ALWAYS-RUNNING.md`. It sets up a small scheduled loop that hands the next ticket to an idle chat.
Skip it until the daily routine feels natural.

## If something breaks

`docs/TROUBLESHOOTING.md` lists the common symptoms and the fix. The first move is always the doctor command
above. Words you do not know (hub, lane, brief, breaker): `docs/GLOSSARY.md`.
