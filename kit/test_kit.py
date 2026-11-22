#!/usr/bin/env python3
"""Checks for the kit files: templates keep their blanks, the settings snippet is valid and
points at hooks the patterns really ship, hub.md carries its core rules."""
import json
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PATTERNS = os.path.join(os.path.dirname(HERE), "patterns")


def read(*p):
    with open(os.path.join(HERE, *p)) as f:
        return f.read()


def readabs(p):
    with open(p) as f:
        return f.read()


class Kit(unittest.TestCase):
    def test_snippet_is_json_and_hooks_exist(self):
        snip = json.loads(read("settings.snippet.json"))
        n = 0
        for event, entries in snip["hooks"].items():
            for entry in entries:
                for h in entry["hooks"]:
                    m = re.search(r"hubkit/([\w-]+)/([\w.-]+)", h["command"])
                    self.assertTrue(m, h["command"])
                    self.assertTrue(os.path.exists(os.path.join(PATTERNS, m.group(1), m.group(2))), h["command"])
                    n += 1
        self.assertEqual(n, 6)
        self.assertEqual(sorted(snip["hooks"]), ["PreCompact", "PreToolUse", "SessionStart", "SubagentStop", "UserPromptSubmit"])

    def test_stop_arm_snippet_is_separate_and_points_at_shipped_file(self):
        main = json.loads(read("settings.snippet.json"))
        loop = json.loads(read("settings.snippet.loop.json"))
        self.assertNotIn("Stop", main["hooks"])          # off by default: never in the snippet people merge
        self.assertEqual(sorted(loop["hooks"]), ["Stop"])
        cmd = loop["hooks"]["Stop"][0]["hooks"][0]["command"]
        m = re.search(r"hubkit/([\w-]+)/([\w.-]+)", cmd)
        self.assertTrue(m and os.path.exists(os.path.join(PATTERNS, m.group(1), m.group(2))), cmd)
        self.assertIn("OFF BY DEFAULT", loop["_comment"])
        self.assertIn("Stop", read("HOOKS-VERIFIED.md"))

    def test_snippet_has_model_guard_and_dup_worker_entries(self):
        snip = json.loads(read("settings.snippet.json"))
        pre = snip["hooks"]["PreToolUse"]
        cmds = {h["command"].split("/")[-1]: e.get("matcher") for e in pre for h in e["hooks"]}
        self.assertIn("model-guard.py", cmds)
        self.assertIn("dup-worker-hook.py", cmds)
        self.assertIn("Agent", cmds["model-guard.py"])
        self.assertIn("Agent", cmds["dup-worker-hook.py"])

    def test_hub_md_status_line_is_an_optional_convention(self):
        t = read("commands", "hub.md").lower()
        self.assertIn("optional", t)
        self.assertNotIn("never a menu", t)

    def test_snippet_events_match_pattern_json(self):
        snip = json.loads(read("settings.snippet.json"))
        meta = json.loads(readabs(os.path.join(PATTERNS, "report-gate", "pattern.json")))
        self.assertEqual(meta["hooks"][0]["event"], "SubagentStop")
        self.assertIn("SubagentStop", snip["hooks"])

    def test_templates_are_blank(self):
        for n in ("OPERATOR.template.md", "CLAUDE.template.md"):
            t = read("templates", n)
            self.assertGreaterEqual(t.count("<"), 5, n)
            self.assertLess(len(t.splitlines()), 100, n)

    def test_hub_md_core_rules(self):
        t = read("commands", "hub.md")
        for s in ("WORKING", "IDLE", "ledger add", "needs-operator", "proof", "model", "exact report",
                  "planner", "bug-gate", "playtester", "one owner", "handoff"):
            self.assertIn(s.lower(), t.lower(), s)
        self.assertLess(len(t.splitlines()), 120)
        self.assertTrue(t.startswith("---\n"))

    def test_no_personal_leftovers(self):
        for root, _, files in os.walk(HERE):
            for f in files:
                if f.endswith((".md", ".json")):
                    t = readabs(os.path.join(root, f))
                    for bad in ("/Us" + "ers/", ".claude" + "/hub/", "nt" + "fy", "[" + "["):
                        self.assertNotIn(bad, t, f + " " + bad)

    def test_hook_entries_have_type_command(self):
        snip = json.loads(read("settings.snippet.json"))
        for event, entries in snip["hooks"].items():
            for entry in entries:
                for h in entry["hooks"]:
                    self.assertEqual(h["type"], "command", event)

    def test_gate_agents_keep_their_blocking_rules(self):
        for name, must in (("bug-gate.md", "A FAIL blocks"), ("playtester.md", "A FAIL blocks")):
            self.assertIn(must, readabs(os.path.join(PATTERNS, "gate-agents", name)), name)

    def _command_docs(self):
        return [os.path.join(HERE, "commands", "hub.md"),
                os.path.join(PATTERNS, "gate-agents", "bug-gate.md"),
                os.path.join(PATTERNS, "gate-agents", "playtester.md")]

    def test_no_bare_tool_names_in_command_docs(self):
        """ledger / lane-lock.py are not on PATH: every runnable span must use the full installed path."""
        for path in self._command_docs():
            for span in re.findall(r"`([^`\n]+)`", readabs(path)):
                self.assertFalse(re.match(r"(ledger |lane-lock\.py|doctor\.py|deadman\.py)", span),
                                 "%s runs %r without its path" % (os.path.basename(path), span))

    def test_every_command_in_hub_and_agent_docs_runs_against_the_real_cli(self):
        """Parse each full-path command out of hub.md and the gate agents, fill the placeholders, run it in a
        scratch HOME against the shipped scripts, and require that the CLI accepts its arguments."""
        import shlex
        import subprocess
        import tempfile
        pat = re.compile(r"`(python3 ~/\.claude/hubkit/([\w-]+)/([\w.-]+)( [^`]*)?)`")
        seen = []
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, HOME=tmp, HUBKIT_HOME=os.path.join(tmp, "hk"), PYTHONDONTWRITEBYTECODE="1",
                       LEDGER_PATH=os.path.join(tmp, "ledger.jsonl"))
            ledger = os.path.join(PATTERNS, "ledger", "ledger")
            subprocess.run([sys.executable, ledger, "add", "scratch item"], env=env, capture_output=True, check=True)
            for path in self._command_docs():
                for _, pname, script, rest in pat.findall(readabs(path)):
                    target = os.path.join(PATTERNS, pname, script)
                    self.assertTrue(os.path.isfile(target), "%s names a missing script %s/%s" % (path, pname, script))
                    args = re.sub(r"<id>", "L-0001", rest or "")
                    args = re.sub(r"<pid>", str(os.getpid()), args)
                    args = re.sub(r"<[^>]+>", "x", args)
                    argv = [sys.executable, target] + shlex.split(args)
                    r = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=30)
                    err = r.stderr + r.stdout
                    for bad in ("usage:", "arguments are required", "invalid choice", "unrecognized arguments",
                                "Traceback"):
                        self.assertNotIn(bad, err, "%s: %s -> %s" % (os.path.basename(path), rest, err[:200]))
                    seen.append((pname, shlex.split(args)[:1]))
        names = {p for p, _ in seen}
        self.assertTrue({"ledger", "lane-lock"} <= names, seen)
        subs = {a[0] for p, a in seen if p == "ledger" and a}
        self.assertTrue({"banner", "surface", "add", "status", "claim", "proof"} <= subs, subs)


if __name__ == "__main__":
    unittest.main()
