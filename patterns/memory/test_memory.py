#!/usr/bin/env python3
"""Tests for the memory pattern. Run through test.sh (scratch HOME). Hooks are fed JSON on stdin only."""
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SID = "-".join(["0b5f3c1e", "1111", "4222", "8333", "444455556666"])
CANARY = "CANARY" + "-9f8e7d6c"   # stands in for any free text that must never reach a stub


def run(script, stdin="", env=None, args=()):
    e = dict(os.environ)
    e.update(env or {})
    return subprocess.run([sys.executable, os.path.join(HERE, script), *args], input=stdin, capture_output=True,
                          text=True, env=e, timeout=30)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        self.env = {"HOME": self.home, "HUBKIT_HOME": os.path.join(self.home, ".claude", "hubkit"), "PYTHONDONTWRITEBYTECODE": "1"}
        os.environ.pop("MEMORY_DIR", None)
        self.mem = run("memory_init.py", env=self.env, args=["--print-dir"]).stdout.strip()

    def init(self):
        return run("memory_init.py", env=self.env)


class Init(Base):
    def test_creates_the_store_once_and_never_overwrites(self):
        r = self.init()
        self.assertEqual(r.returncode, 0, r.stderr)
        for f in ("MEMORY.md", "ARCHIVE.md", "conversations_index.md"):
            self.assertTrue(os.path.isfile(os.path.join(self.mem, f)), f)
        self.assertTrue(os.path.isdir(os.path.join(self.mem, "conversations")))
        with open(os.path.join(self.mem, "MEMORY.md"), "a") as fh:
            fh.write("\n- my own rule\n")
        r2 = self.init()
        self.assertIn("created: nothing", r2.stdout)
        self.assertIn("my own rule", open(os.path.join(self.mem, "MEMORY.md")).read())

    def test_dry_run_writes_nothing(self):
        r = run("memory_init.py", env=self.env, args=["--dry-run"])
        self.assertEqual(r.returncode, 0)
        self.assertFalse(os.path.exists(self.mem))

    def test_dir_is_under_the_project_slug(self):
        self.assertIn(os.path.join(".claude", "projects"), self.mem)
        self.assertTrue(self.mem.endswith("memory"))
        self.assertNotRegex(os.path.basename(os.path.dirname(self.mem)), r"[^A-Za-z0-9-]")

    def test_relative_memory_dir_is_refused(self):
        r = run("memory_init.py", env=dict(self.env, MEMORY_DIR="rel/path"), args=["--print-dir"])
        self.assertNotEqual(r.returncode, 0)

    def test_router_template_is_within_its_cap(self):
        self.init()
        lines = [x for x in open(os.path.join(self.mem, "MEMORY.md")).read().splitlines() if x.strip()]
        self.assertLessEqual(len(lines), 40)


class Stub(Base):
    def stub_path(self, sid=SID):
        return os.path.join(self.env["HUBKIT_HOME"], "compact-stubs", sid + ".md")

    def test_writes_a_stub_and_exits_zero(self):
        self.init()
        tp = os.path.join(self.tmp, "t.jsonl")
        open(tp, "w").write('{"text":"working on L-0007 and L-0004, also L-0007"}\n')
        r = run("pre_compact_stub.py", json.dumps({"session_id": SID, "transcript_path": tp}), self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        t = open(self.stub_path()).read()
        self.assertIn("- tickets: L-0004, L-0007", t)
        self.assertIn("NO (run /log", t)

    def test_logged_footer_flips_the_flag(self):
        self.init()
        conv = os.path.join(self.mem, "conversations")
        open(os.path.join(conv, "2026-01-02-demo.md"), "w").write("x\n<!-- logged-session: %s -->\n" % SID)
        run("pre_compact_stub.py", json.dumps({"session_id": SID}), self.env)
        t = open(self.stub_path()).read()
        self.assertIn("- logged: yes", t)
        self.assertIn("2026-01-02-demo.md", t)

    def test_hostile_inputs_never_block_and_never_leak(self):
        bad = ["", "not json", "[]", "null", '{"session_id": "../../etc/passwd"}',
               json.dumps({"session_id": SID + "\n- injected: yes"}),
               json.dumps({"session_id": SID, "transcript_path": "/definitely/not/here"}),
               json.dumps({"session_id": 12345, "cwd": "/home/x/" + CANARY}),
               json.dumps({"session_id": SID, "last_assistant_message": CANARY}),
               "{" * 5000]
        for b in bad:
            r = run("pre_compact_stub.py", b, self.env)
            self.assertEqual(r.returncode, 0, b[:40])
        stubs = os.path.join(self.env["HUBKIT_HOME"], "compact-stubs")
        for f in os.listdir(stubs):
            body = open(os.path.join(stubs, f)).read()
            self.assertNotIn(CANARY, body)
            self.assertNotIn("injected", body)
            self.assertNotIn("/etc/passwd", body)
        self.assertEqual(sorted(f for f in os.listdir(stubs) if f.endswith(".md")),
                         sorted([SID + ".md", "unknown.md"]))

    def test_unwritable_hub_still_exits_zero(self):
        blocker = os.path.join(self.tmp, "file")
        open(blocker, "w").write("x")
        r = run("pre_compact_stub.py", json.dumps({"session_id": SID}), dict(self.env, HUBKIT_HOME=os.path.join(blocker, "sub")))
        self.assertEqual(r.returncode, 0)


class Pickup(Base):
    def test_prints_the_stub_as_session_context(self):
        run("pre_compact_stub.py", json.dumps({"session_id": SID}), self.env)
        r = run("compact_pickup.py", json.dumps({"session_id": SID, "hook_event_name": "SessionStart"}), self.env)
        self.assertEqual(r.returncode, 0)
        out = json.loads(r.stdout)
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertIn(SID, out["hookSpecificOutput"]["additionalContext"])

    def test_old_stub_of_another_session_is_not_shown(self):
        d = os.path.join(self.env["HUBKIT_HOME"], "compact-stubs")
        os.makedirs(d)
        old = os.path.join(d, "-".join(["11111111", "aaaa", "bbbb", "cccc", "222222222222"]) + ".md")
        with open(old, "w") as fh:
            fh.write("stale")
        os.utime(old, (1_000_000_000, 1_000_000_000))
        r = run("compact_pickup.py", json.dumps({"session_id": SID}), self.env)
        self.assertEqual((r.returncode, r.stdout), (0, ""))

    def test_silent_when_there_is_no_stub_or_bad_input(self):
        for b in ("", "garbage", "[]"):
            r = run("compact_pickup.py", b, self.env)
            self.assertEqual((r.returncode, r.stdout), (0, ""))


class Files(unittest.TestCase):
    def read(self, n):
        with open(os.path.join(HERE, n)) as fh:
            return fh.read()

    def test_pattern_json_lists_real_files(self):
        meta = json.loads(self.read("pattern.json"))
        for f in meta["files"]:
            self.assertTrue(os.path.exists(os.path.join(HERE, f)), f)
        for c in meta["commands"]:
            self.assertIn(c, meta["files"])
        self.assertEqual({h["event"] for h in meta["hooks"]}, {"PreCompact", "SessionStart"})

    def test_commands_have_frontmatter_and_both_halves_of_the_rule(self):
        for n in ("log.md", "checkpoint.md"):
            t = self.read(n)
            self.assertTrue(t.startswith("---\ndescription:"), n)
            self.assertIn("neighbors", t.lower(), n)
            self.assertIn("verify", t.lower(), n)
        self.assertIn("logged-session", self.read("log.md"))

    def test_no_machine_paths_or_wiki_links(self):
        for n in os.listdir(HERE):
            if n.endswith((".md", ".py", ".json")) and n != "test_memory.py":
                t = self.read(n)
                self.assertNotIn("/Users/", t, n)
                self.assertNotIn("[[", t, n)


if __name__ == "__main__":
    unittest.main()
