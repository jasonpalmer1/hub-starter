#!/usr/bin/env python3
"""base_read.py <repo> <base> <path>

Prints <path> as it is AT the base commit (git show base:path). Never falls back to the worktree or tip copy,
so a branch cannot change the rules it is reviewed by.
Exit 0 = printed; 2 = bad call, base not a commit, unsafe path, symlink or non-file; 3 = path absent at base
(an explicit MISS on stderr).
"""
import subprocess
import sys


def err(msg):
    print("base_read: " + msg, file=sys.stderr)


def main(argv):
    if len(argv) != 4:
        err("usage: base_read.py <repo> <base> <path>")
        return 2
    repo, base, p = argv[1:]
    parts = p.split("/")
    if not p or p.startswith("/") or ".." in parts or "\n" in p or "\0" in p:
        err("unsafe path: %r" % p)
        return 2
    r = subprocess.run(["git", "-C", repo, "rev-parse", "--verify", "-q", base + "^{commit}"], capture_output=True)
    sha = r.stdout.decode().strip()
    if r.returncode != 0 or not sha:
        err("base is not a commit: %s" % base)
        return 2
    t = subprocess.run(["git", "-C", repo, "ls-tree", "-z", sha, "--", p], capture_output=True)
    ent = t.stdout.split(b"\0")[0].decode("utf-8", "replace")
    if not ent:
        err("MISS: %s does not exist at base %s (no fallback to the tip copy)" % (p, sha[:12]))
        return 3
    mode = ent.split(" ", 1)[0]
    if mode not in ("100644", "100755"):
        err("refused: %s is mode %s at base %s (symlink or not a regular file)" % (p, mode, sha[:12]))
        return 2
    c = subprocess.run(["git", "-C", repo, "cat-file", "blob", "%s:%s" % (sha, p)], capture_output=True)
    if c.returncode != 0:
        err("cat-file failed for %s" % p)
        return 2
    sys.stdout.buffer.write(c.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
