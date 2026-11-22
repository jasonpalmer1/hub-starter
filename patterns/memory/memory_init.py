#!/usr/bin/env python3
"""memory_init: create the three-tier memory store for one project folder. Never overwrites a file.

  python3 memory_init.py                     store for $HOME (the usual choice for one chat folder)
  python3 memory_init.py --project DIR       store for the folder you start Claude Code in
  python3 memory_init.py --print-dir         print the memory folder and exit (the /log and /checkpoint commands call this)
  python3 memory_init.py --dry-run           show what would be created

The store lives where Claude Code looks for its own memory: ~/.claude/projects/<folder slug>/memory/
(the slug is the project path with every character that is not a letter or digit turned into "-").
Env: MEMORY_DIR overrides the folder (absolute path). HOME must be set.
"""
import argparse
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def slug(path):
    return re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(path))


def memory_dir(project=None):
    env = os.environ.get("MEMORY_DIR")
    if env:
        if not os.path.isabs(env):
            raise SystemExit("memory_init: MEMORY_DIR must be an absolute path")
        return env
    home = os.environ.get("HOME", "")
    if not home or not os.path.isabs(home):
        raise SystemExit("memory_init: HOME is not set to an absolute path")
    return os.path.join(home, ".claude", "projects", slug(project or home), "memory")


def main(argv):
    ap = argparse.ArgumentParser(prog="memory_init.py")
    ap.add_argument("--project", help="project folder the store belongs to (default: HOME)")
    ap.add_argument("--print-dir", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    d = memory_dir(a.project)
    if a.print_dir:
        print(d)
        return 0
    made, kept = [], []
    plan = [("MEMORY.template.md", "MEMORY.md"), ("ARCHIVE.template.md", "ARCHIVE.md"),
            ("conversations_index.template.md", "conversations_index.md")]
    for src, dst in plan:
        target = os.path.join(d, dst)
        if os.path.lexists(target):
            kept.append(dst)
            continue
        made.append(dst)
        if not a.dry_run:
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(HERE, src), "rb") as fh:
                data = fh.read()
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            with os.fdopen(fd, "wb") as out:
                out.write(data)
    conv = os.path.join(d, "conversations")
    if not os.path.isdir(conv):
        made.append("conversations/")
        if not a.dry_run:
            os.makedirs(conv, exist_ok=True)
    print("memory_init%s -> %s" % (" (DRY RUN)" if a.dry_run else "", d))
    print("created: %s" % (", ".join(made) or "nothing"))
    print("kept (already there): %s" % (", ".join(kept) or "nothing"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
