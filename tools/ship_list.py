#!/usr/bin/env python3
"""ship_list.py [--write|--check] : the human-readable table of every file that ships.

Columns (tab separated): path, lines, purpose, pattern. Default prints the table for
the git-tracked files. --write regenerates MANIFEST.txt, keeping purposes already written.
--check exits 1 if MANIFEST.txt paths differ from `git ls-files` (either direction).
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def tracked():
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    return sorted(set(out.split("\n")) - {""})


def old_manifest():
    ents = {}
    p = os.path.join(ROOT, "MANIFEST.txt")
    if os.path.isfile(p):
        for ln in open(p, encoding="utf-8").read().splitlines():
            if ln.strip() and not ln.startswith("#"):
                cols = ln.split("\t")
                ents[cols[0]] = cols + [""] * (4 - len(cols))
    return ents


def nlines(rel):
    try:
        with open(os.path.join(ROOT, rel), "rb") as fh:
            return fh.read().count(b"\n")
    except OSError:
        return 0


def table(files, old):
    rows = []
    for rel in sorted(set(files) | {"MANIFEST.txt"}):
        o = old.get(rel, [rel, "", "TODO: purpose", ""])
        rows.append("\t".join([rel, "-" if rel == "MANIFEST.txt" else str(nlines(rel)),
                               o[2] or "TODO: purpose", o[3] or "core"]))
    return rows


def main(argv):
    mode = argv[0] if argv else ""
    files, old = tracked(), old_manifest()
    if mode == "--check":
        want = set(files) | {"MANIFEST.txt"}
        have = set(old)
        bad = sorted(want ^ have)
        for b in bad:
            print("%s:0 manifest" % b)
        return 1 if bad else 0
    rows = table(files, old)
    if mode == "--write":
        with open(os.path.join(ROOT, "MANIFEST.txt"), "w", encoding="utf-8") as fh:
            fh.write("# path\tlines\tpurpose\tpattern  (every shipped file; prepush fails if this and git differ)\n")
            fh.write("\n".join(rows) + "\n")
    else:
        print("\n".join(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
