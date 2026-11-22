#!/usr/bin/env python3
"""Structure tests for the gate agent files. Run: python3 test_gate_agents.py"""
import json
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
AGENTS = ("planner", "bug-gate", "playtester", "bottleneck", "judge", "check-checker")


def read(name):
    with open(os.path.join(HERE, name)) as f:
        return f.read()


def frontmatter(text):
    m = re.match(r"---\n(.*?)\n---\n", text, re.S)
    assert m, "no frontmatter"
    out = {}
    for line in m.group(1).splitlines():
        k, _, v = line.partition(":")
        out[k.strip()] = v.strip()
    return out


class GateAgents(unittest.TestCase):
    def test_frontmatter(self):
        for name in AGENTS:
            fm = frontmatter(read(name + ".md"))
            self.assertEqual(fm["name"], name)
            self.assertTrue(fm["description"])
            self.assertIn(fm["model"], ("sonnet", "opus", "haiku"))

    def test_gates_cannot_edit_code(self):
        for name in AGENTS:
            tools = [t.strip() for t in frontmatter(read(name + ".md"))["tools"].split(",")]
            self.assertNotIn("Edit", tools, name)
            self.assertIn("Write", tools, name)

    def test_no_personal_or_machine_paths(self):
        for name in AGENTS:
            t = read(name + ".md")
            self.assertNotIn("/Users/", t)
            self.assertNotIn(".claude" + "/hub/", t)
            self.assertNotIn("[[", t)

    def test_acceptance_first_and_fail_blocks(self):
        for name in ("bug-gate", "playtester"):
            t = read(name + ".md")
            self.assertIn("Acceptance check FIRST", t)
            self.assertIn("FAIL", t)

    def test_planner_sections_and_status(self):
        t = read("planner.md")
        for s in ("CHANNEL:", "Attack list", "UNCONFIRMED", "STATUS: DRAFT", "Bug-gate checklist", "Playtest script"):
            self.assertIn(s, t)

    def test_bug_gate_mutation_and_attacker(self):
        t = read("bug-gate.md")
        self.assertIn("TEMPORARY COPY", t)
        self.assertIn("held-out attacker", t)
        self.assertIn("Never fix product code", t)

    def test_never_kill_by_name(self):
        for name in ("bug-gate", "playtester"):
            self.assertIn("Never `pkill`", read(name + ".md"))

    def test_pattern_json_lists_real_files(self):
        meta = json.loads(read("pattern.json"))
        for f in meta["files"]:
            self.assertTrue(os.path.exists(os.path.join(HERE, f)), f)
        self.assertEqual(sorted(meta["agents"]), sorted(a + ".md" for a in AGENTS))


if __name__ == "__main__":
    unittest.main()
