#!/usr/bin/env python3
"""bug_sweep: the bug check for an empty queue. Runs an operator-declared smoke command against work that merged
and has not been swept yet, and records the result where `improve.py mine` reads it.

  bug_sweep.py run [--project NAME] [--dry-run]   sweep every listed project (or one) that has an uncovered range
  bug_sweep.py projects                           validate projects.json, one line per entry
  bug_sweep.py status                             last result per project

Config is $HUBKIT_HOME/state/projects.json, written by the operator and nothing else:
  {"projects": [{"name": "app", "path": "/abs/path/to/repo", "smoke": ["python3", "-m", "unittest"], "timeout": 60}]}
The smoke command is an argument list. It is run directly, never through a shell, and never taken from the repo.

Rules (each refusal prints its reason):
  queue-not-empty     any open ticket that is not an improve candidate stops the sweep (work comes first)
  unlisted-project    only names in projects.json are swept
  range-covered       commits already covered by a passing sweep are not swept again
  sweep-running       one sweep at a time (non-blocking lock); every decision is read inside the lock
A failing smoke test writes state/bugsweep/NAME.txt (line 1 = 1, then the output). `improve.py mine` turns that into a
candidate; the candidate is closed by a later passing sweep, not by a note.

Python 3.9, standard library only. POSIX only (fcntl, process groups).
"""
import argparse
import contextlib
import fcntl
import json
import math
import os
import re
import signal
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import improve  # noqa: E402  (same folder: the gate, the clock, the ledger readers)

NAME_RE = improve.NAME_RE
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DEFAULT_TIMEOUT = 60
MAX_TIMEOUT = 600
KILL_GRACE_S = 2
FIRST_SWEEP_COMMITS = 20       # a never-swept project: look at the last 20 commits, not its whole history
MAX_RANGE = 500
OUT_KEEP = 4000
TOKENISH = re.compile(r"[A-Za-z0-9_\-]{32,}|\b(?:gh[pousr]_|sk-|xox[a-z]-|AKIA)[A-Za-z0-9_\-]{8,}")

Refuse = improve.Refuse


def now_s():
    return improve.now_s()


# ------------------------------------------------------------------ state (every write stays under $HUBKIT_HOME)
def sweep_dir(create=False):
    d = os.path.join(improve.state_dir(), "bugsweep")
    if create:
        os.makedirs(d, exist_ok=True)
    if not improve.inside_home(os.path.realpath(d)):
        raise Refuse("evidence-outside-home")
    return d


def coverage():
    return improve.read_jsonl("bugsweep-covered.jsonl")


def covered_shas(project):
    seen = []
    for e in coverage():
        if e.get("project") == project and e.get("result") == "pass" and SHA_RE.match(str(e.get("to", ""))):
            seen.append(e["to"])
    return seen[-50:]


@contextlib.contextmanager
def sweep_lock():
    """Non-blocking: a second sweep refuses instead of queueing behind a long smoke command."""
    os.makedirs(improve.state_dir(), exist_ok=True)
    fd = os.open(improve.path_in_state("bug-sweep.lock"), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise Refuse("sweep-running")
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


# ------------------------------------------------------------------ projects.json
def load_projects(now):
    """-> [(name, entry-or-None, problem-or-None)]. Raises Refuse when the file itself is unusable."""
    _, _, _, text = improve.evidence_file(os.path.join("state", "projects.json"), now, reading=False)
    try:
        items = json.loads(text)["projects"]
        if not isinstance(items, list):
            raise ValueError
    except (ValueError, KeyError, TypeError):
        raise Refuse("projects-unreadable")
    out = []
    for it in items:
        name = it.get("name") if isinstance(it, dict) else None
        if not isinstance(name, str) or not NAME_RE.match(name):
            out.append((str(name)[:30] if isinstance(name, str) else "?", None, "bad-name"))
            continue
        problem = check_entry(it)
        out.append((name, None if problem else it, problem))
    return out


def check_entry(it):
    path, smoke = it.get("path"), it.get("smoke")
    if not isinstance(path, str) or not os.path.isabs(path) or "\0" in path or not os.path.isdir(path):
        return "bad-path"
    if not isinstance(smoke, list) or not smoke or not all(isinstance(a, str) and a and "\0" not in a for a in smoke):
        return "bad-command"       # a string would need a shell; only an argument list is accepted
    t = it.get("timeout", DEFAULT_TIMEOUT)
    if isinstance(t, bool) or not isinstance(t, (int, float)) or not math.isfinite(t) or t <= 0:
        return "bad-timeout"
    if improve.injected(it["name"]):
        return "bad-name"
    return None


# ------------------------------------------------------------------ git (argument lists, repo config never executed)
def git(path, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    p = subprocess.run(["git", "-C", path] + list(args), capture_output=True, text=True, env=env, timeout=30)
    return p.returncode, p.stdout.strip()


def result_is_fresh(project):
    """Is the result file mine reads present and under the evidence age limit?"""
    try:
        age = now_s() - os.path.getmtime(os.path.join(sweep_dir(), project + ".txt"))
    except (OSError, Refuse):
        return False
    return age <= improve.EVIDENCE_MAX_AGE_S


def uncovered(path, project):
    """-> (head sha, number of uncovered commits). Raises Refuse. A commit reachable from a covered sha is covered, so a
    rebased history is swept again for exactly the commits that are new to it."""
    rc, head_ref = git(path, "symbolic-ref", "-q", "HEAD")
    if rc != 0:
        raise Refuse("detached-head")
    rc, head = git(path, "rev-parse", "--verify", "HEAD^{commit}")
    if rc != 0 or not SHA_RE.match(head):
        raise Refuse("no-commits")
    known = []
    for sha in covered_shas(project):
        if git(path, "cat-file", "-e", sha + "^{commit}")[0] == 0:
            known.append(sha)
    args = ["rev-list", "--count", "--max-count=%d" % (MAX_RANGE if known else FIRST_SWEEP_COMMITS), "HEAD"]
    if known:
        args += ["--not"] + known
    rc, n = git(path, *args)
    if rc != 0 or not n.isdigit():
        raise Refuse("no-commits")
    if int(n) == 0:
        if result_is_fresh(project):
            raise Refuse("range-covered")
        return head, 0     # covered, but the evidence mine reads has gone stale: sweep again to refresh it
    return head, int(n)


# ------------------------------------------------------------------ the smoke command
def run_smoke(argv, cwd, timeout):
    """Run argv with no shell, in its own process group. -> (exit code or None on timeout, output text).
    Output goes to a temp file (a stray grandchild can never hold a pipe open). The whole group is killed on timeout,
    and also after a normal exit so a leftover background child does not outlive the sweep."""
    try:
        timeout = float(timeout)
    except (TypeError, ValueError):
        timeout = MAX_TIMEOUT
    if not math.isfinite(timeout) or timeout <= 0:
        timeout = MAX_TIMEOUT
    timeout = min(timeout, MAX_TIMEOUT)
    fd, tmp = tempfile.mkstemp(prefix="smoke-", dir=sweep_dir(create=True))
    code, timed_out = None, False
    try:
        with os.fdopen(fd, "wb") as out:
            try:
                proc = subprocess.Popen(argv, cwd=cwd, shell=False, stdin=subprocess.DEVNULL, stdout=out,
                                        stderr=subprocess.STDOUT, start_new_session=True)
            except OSError as ex:
                return 127, "could not start: %s" % ex.__class__.__name__
            try:
                code = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
            finally:
                kill_group(proc)
        with open(tmp, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - OUT_KEEP))
            text = fh.read().decode("utf-8", errors="replace")
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
    if timed_out:
        text = "TIMEOUT after %ds, process group killed\n%s" % (timeout, text)
    return (None if timed_out else code), text


def kill_group(proc):
    pgid = proc.pid
    for sig, wait in ((signal.SIGTERM, KILL_GRACE_S), (signal.SIGKILL, 1)):
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError):
            break
        deadline = time.time() + wait
        while time.time() < deadline:
            try:
                os.killpg(pgid, 0)
            except (ProcessLookupError, PermissionError):
                break
            time.sleep(0.05)
    with contextlib.suppress(Exception):
        proc.wait(timeout=1)


def clean_output(text):
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", text.replace("\r", "\n"))
    return TOKENISH.sub("[redacted]", text)


def write_result(name, head, n, code, text):
    """Atomic write of the file `improve.py mine` reads. Line 1 is 1 (failed) or 0 (passed)."""
    d = sweep_dir(create=True)
    final = os.path.join(d, name + ".txt")
    failed = code != 0
    body = "%d\nproject: %s\nhead: %s\ncommits: %d\nexit: %s\n" % (1 if failed else 0, name, head[:12], n,
                                                                   "timeout" if code is None else code)
    if failed:
        body += "--- output ---\n" + clean_output(text)
    fd, tmp = tempfile.mkstemp(prefix="." + name + "-", dir=d)
    with os.fdopen(fd, "w") as fh:
        fh.write(body)
    os.replace(tmp, final)
    return final


# ------------------------------------------------------------------ commands
def cmd_run(args):
    now = now_s()
    if args.project is not None and not NAME_RE.match(args.project):
        print("REFUSE bad-name")
        return 5
    ctx = contextlib.nullcontext() if args.dry_run else sweep_lock()
    try:
        with ctx:
            # Everything the decision rests on is read INSIDE the lock: the queue, the config, the coverage log.
            try:
                records = improve.ledger_records()
            except Refuse as ex:
                print("bug-sweep: %s" % ex.reason)
                return 2
            cands = improve.candidates(records)
            blockers = improve.queue_blockers(records, cands)
            if blockers:
                print("queue not empty (%d)" % len(blockers))
                return 4
            try:
                projects = load_projects(now)
            except Refuse as ex:
                print("REFUSE %s" % ex.reason)
                return 2
            names = dict((n, (e, p)) for n, e, p in projects)
            if args.project is not None and args.project not in names:
                print("REFUSE unlisted-project")
                return 5
            todo = [args.project] if args.project else [n for n, _, _ in projects]
            swept = failed = 0
            for name in todo:
                entry, problem = names[name]
                if problem:
                    print("REFUSE %s %s" % (name, problem))
                    continue
                try:
                    head, n = uncovered(entry["path"], name)
                except Refuse as ex:
                    print("SKIP %s %s" % (name, ex.reason))
                    continue
                if args.dry_run:
                    print("WOULD RUN %s (%d commits)" % (name, n))
                    continue
                code, text = run_smoke(entry["smoke"], entry["path"], entry.get("timeout", DEFAULT_TIMEOUT))
                write_result(name, head, n, code, text)
                swept += 1
                ok = code == 0
                failed += 0 if ok else 1
                improve.append_jsonl("bugsweep-covered.jsonl", {"t": now, "project": name, "to": head, "n": n,
                                                                "result": "pass" if ok else "fail"})
                improve.emit("bugsweep.run", now, project=name, result="pass" if ok else "fail", commits=n)
                print("SWEEP %s %d commits: %s" % (name, n, "PASS" if ok else "FAIL" if code is not None else "TIMEOUT"))
            if not args.dry_run:
                with open(improve.path_in_state("bug-sweep.tick"), "w") as fh:
                    fh.write(improve.iso(now) + "\n")
                if not swept:
                    print("nothing to sweep")
            return 0
    except Refuse as ex:
        print("REFUSE %s" % ex.reason)
        return 4 if ex.reason == "sweep-running" else 5


def cmd_projects(args):
    try:
        projects = load_projects(now_s())
    except Refuse as ex:
        print("REFUSE %s" % ex.reason)
        return 2
    for name, entry, problem in projects:
        print("%s %s" % (name, "OK" if entry else "REFUSE " + problem))
    return 0


def cmd_status(args):
    last = {}
    for e in coverage():
        last[e.get("project")] = e
    for name in sorted(k for k in last if isinstance(k, str)):
        e = last[name]
        print("%s %s %s" % (name, e.get("result"), str(e.get("to", ""))[:8]))
    if not last:
        print("no sweeps yet")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="bug_sweep")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run")
    p.add_argument("--project", default=None)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_run)
    sub.add_parser("projects").set_defaults(fn=cmd_projects)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except (OSError, subprocess.SubprocessError) as ex:
        print("bug-sweep: %s" % ex.__class__.__name__)
        return 2


if __name__ == "__main__":
    sys.exit(main())
