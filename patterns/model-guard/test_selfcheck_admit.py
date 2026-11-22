#!/usr/bin/env python3
"""Tests for selfcheck_admit.py (gate admission by the builder's own record). Scratch git repo, stdlib only."""
import datetime, importlib.util, json, os, pathlib, subprocess, sys, tempfile

HERE = pathlib.Path(__file__).resolve().parent
T = pathlib.Path(tempfile.mkdtemp(prefix="sca-kit-")).resolve()
G = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", "-C", str(T)]
def git(*a): return subprocess.run(G + list(a), capture_output=True, text=True, check=True).stdout.strip()
git("init", "-q", "-b", "main")
def commit(n):
    (T / "f").write_text(n); git("add", "f"); git("commit", "-q", "-m", n); return git("rev-parse", "HEAD")
common = pathlib.Path(git("rev-parse", "--path-format=absolute", "--git-common-dir")); (common / "selfcheck").mkdir()
def rec(sha, exit=0): (common / "selfcheck" / sha).write_text(json.dumps({"sha": sha, "exit": exit, "clean": True}))
GREEN, RED, MISSING, UNREAD = commit("g"), commit("r"), commit("m"), commit("u")
rec(GREEN); rec(RED, 1); rec(UNREAD); os.chmod(common / "selfcheck" / UNREAD, 0)
OLD = T / "marker-old"; OLD.write_text("gate-admit warn-only since 2026-01-01T00:00:00+00:00\n")
spec = importlib.util.spec_from_file_location("sca", HERE / "selfcheck_admit.py"); sca = importlib.util.module_from_spec(spec); spec.loader.exec_module(sca)
BG = {"subagent_type": "bug-gate"}
def chk(sha, marker=OLD, tin=BG, cue="tip"): return sca.check(tin, "gate %s in %s %s" % (T, cue, sha), path=str(marker))
fails = 0
def t(name, cond):
    global fails
    print("PASS" if cond else "FAIL", name); fails += (not cond)

t("red record blocks", (chk(RED) or ("",))[0] == "block")
t("missing record blocks", (chk(MISSING) or ("",))[0] == "block")
t("green record allows", chk(GREEN) is None)
t("unreadable record fails open", (chk(UNREAD) or ("",))[0] != "block")
t("no cue word: not checked", sca.check(BG, "red is %s in %s" % (RED, T), path=str(OLD))[0] == "warn")
t("UUID-shaped string is not a sha", sca.shas_in("seen at " + "-".join(["ddc5cb1c", "309b", "4281", "b81c", "c88052fe698a"])) == [])
t("warn window allows a red record", (chk(RED, marker=T / "nope") or ("",))[0] == "warn")
t("waiver line allows", sca.check(BG, "SELFCHECK-WAIVED: doc only\n" + "tip " + RED)[0] == "waived")
t("other agent types are ignored", chk(RED, tin={"subagent_type": "builder"}) is None)
import time
for i, body in enumerate(["", "lorem ipsum", "since someday", "since 2026-13-45T99:99:99", "2026-01-01", "since 2099-01-01"]):
    mk = T / ("bad%d" % i); mk.write_text(body); os.utime(mk, (time.time() - 30 * 86400,) * 2)
    t("malformed marker %r warns, never blocks" % body, (chk(RED, marker=mk) or ("",))[0] == "warn")
t("valid old marker still blocks", (chk(RED, marker=OLD) or ("",))[0] == "block")
def _boom(prompt): raise RuntimeError("x")
_real = sca.evaluate; sca.evaluate = _boom
_c = sca.check(BG, "tip abcdef1234", path=str(OLD)); sca.evaluate = _real
t("crash fails open as a visible warn", _c is not None and _c[0] == "warn")
sca._DEADLINE[0] = time.monotonic() - 1
try: sca._remaining(4); t("spent deadline raises", False)
except sca._Deadline: t("spent deadline raises", True)
sca._DEADLINE[0] = None
os.chmod(common / "selfcheck" / UNREAD, 0o644)
print("FAILS:", fails)
sys.exit(1 if fails else 0)
