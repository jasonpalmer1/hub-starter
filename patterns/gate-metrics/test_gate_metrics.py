#!/usr/bin/env python3
"""Tests for gate_metrics.py: first-gate FAIL share from verdict files, spawn counts from the index."""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.environ.get("GATE_METRICS", os.path.join(HERE, "gate_metrics.py"))
BUG_GATE_MD = os.path.join(HERE, "..", "gate-agents", "bug-gate.md")


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "bug-gate")
        self.idx = os.path.join(self.tmp, "gs.jsonl")
        os.makedirs(self.root)
        self.env = dict(os.environ, HOME=self.tmp, PYTHONDONTWRITEBYTECODE="1", GATE_SPAWNS_LOG=self.idx)
        self.env.pop("HUBKIT_HOME", None)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def vf(self, rel, verdict, ticket="L-0001", slice_="S1", rnd=None, mtime=None):
        p = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        head = "VERDICT: %s\nTICKET: %s\nSLICE: %s\n" % (verdict, ticket, slice_)
        if rnd is not None:
            head += "ROUND: %d\n" % rnd
        with open(p, "w") as f:
            f.write(head + "\nblockers...\n")
        if mtime:
            os.utime(p, (mtime, mtime))
        return p

    def run_tool(self, *args):
        r = subprocess.run([sys.executable, TOOL, "--json", *args], capture_output=True, text=True, env=self.env)
        return r, (json.loads(r.stdout) if r.stdout.strip().startswith("{") else None)

    def test_three_synthetic_verdicts_give_the_right_first_gate_share(self):
        self.vf("a/VERDICT.md", "FAIL", "L-0001", "S1")
        self.vf("b/VERDICT.md", "PASS", "L-0002", "S1")
        self.vf("c/VERDICT.md", "PASS WITH NOTES", "L-0003", "S1")
        r, d = self.run_tool("--root", self.root)
        self.assertEqual(r.returncode, 0)
        self.assertEqual((d["first_gate_fails"], d["first_gate_total"], d["first_gate_fail_pct"]), (1, 3, 33.3))

    def test_a_fail_then_pass_counts_as_one_first_gate_fail(self):
        self.vf("a/r1-VERDICT.md", "FAIL", rnd=1, mtime=2000)
        self.vf("a/r2-VERDICT.md", "PASS", rnd=2, mtime=1000)  # older file but later round
        _, d = self.run_tool("--root", self.root)
        self.assertEqual((d["chains"], d["gates"], d["first_gate_fails"], d["first_gate_total"]), (1, 2, 1, 1))
        self.assertEqual(d["first_gate_fail_pct"], 100.0)

    def test_round_missing_ties_break_on_oldest_file(self):
        self.vf("a/x-VERDICT.md", "PASS", mtime=1000)
        self.vf("b/VERDICT.md", "FAIL", mtime=2000)
        _, d = self.run_tool("--root", self.root)
        self.assertEqual((d["first_gate_fails"], d["first_gate_total"]), (0, 1))

    def test_pending_unknown_and_bad_ticket_are_not_counted(self):
        self.vf("a/VERDICT.md", "PENDING")
        self.vf("b/VERDICT.md", "MAYBE", "L-0002")
        self.vf("c/VERDICT.md", "FAIL", "not-a-ticket")
        self.vf("d/VERDICT.md", "PASS", "L-0004")
        _, d = self.run_tool("--root", self.root)
        self.assertEqual((d["gates"], d["not_counted"], d["first_gate_total"]), (1, 3, 1))

    def test_same_ticket_different_slices_are_different_chains(self):
        self.vf("a/VERDICT.md", "FAIL", "L-0001", "S1")
        self.vf("b/VERDICT.md", "PASS", "L-0001", "S2")
        _, d = self.run_tool("--root", self.root)
        self.assertEqual((d["chains"], d["first_gate_fails"], d["first_gate_total"]), (2, 1, 2))

    def test_symlinked_verdict_and_other_names_are_ignored(self):
        outside = os.path.join(self.tmp, "outside.md")
        with open(outside, "w") as f:
            f.write("VERDICT: FAIL\nTICKET: L-0009\n")
        os.symlink(outside, os.path.join(self.root, "VERDICT.md"))
        with open(os.path.join(self.root, "notes.md"), "w") as f:
            f.write("VERDICT: FAIL\nTICKET: L-0009\n")
        _, d = self.run_tool("--root", self.root)
        self.assertEqual((d["gates"], d["not_counted"]), (0, 0))

    def test_files_deeper_than_four_folders_are_ignored(self):
        self.vf("a/b/c/d/e/VERDICT.md", "FAIL")
        _, d = self.run_tool("--root", self.root)
        self.assertEqual(d["gates"], 0)

    def test_empty_binary_and_huge_files_do_not_crash(self):
        open(os.path.join(self.root, "VERDICT.md"), "w").close()
        with open(os.path.join(self.root, "x-VERDICT.md"), "wb") as f:
            f.write(b"\xff\xfe\x00" * 100)
        with open(os.path.join(self.root, "y-VERDICT.md"), "w") as f:
            f.write("VERDICT: PASS\nTICKET: L-0001\n" + "z" * 3000000)
        r, d = self.run_tool("--root", self.root)
        self.assertEqual((r.returncode, d["gates"]), (0, 1))

    def test_missing_root_is_zero_not_a_crash(self):
        r, d = self.run_tool("--root", os.path.join(self.tmp, "nope"))
        self.assertEqual((r.returncode, d["gates"], d["first_gate_fail_pct"]), (0, 0, None))

    def test_target_exit_code(self):
        self.vf("a/VERDICT.md", "FAIL", "L-0001")
        self.vf("b/VERDICT.md", "PASS", "L-0002")
        r, d = self.run_tool("--root", self.root, "--target", "40")
        self.assertEqual((r.returncode, d["over_target"]), (1, True))
        r, d = self.run_tool("--root", self.root, "--target", "50")
        self.assertEqual((r.returncode, d["over_target"]), (0, False))
        r, d = self.run_tool("--root", os.path.join(self.tmp, "nope"), "--target", "0")
        self.assertEqual((r.returncode, d["over_target"]), (0, False))

    def test_spawn_counts_skip_corrupt_lines(self):
        with open(self.idx, "wb") as f:
            f.write(b'{"subagent_type":"builder"}\n{"subagent_type":"bug-gate"}\n{"subagent_type":"builder"}\n'
                    b'{{garbage\n\xff\xfe\n[1,2]\n{"subagent_type":"fork"}\n')
        _, d = self.run_tool("--root", self.root)
        self.assertEqual(d["spawns"], {"bug-gate": 1, "builder": 2, "corrupt": 3})

    def test_non_string_subagent_type_is_corrupt_not_a_crash(self):
        with open(self.idx, "w") as f:
            f.write('{"subagent_type":"builder"}\n{"subagent_type":{}}\n{"subagent_type":[]}\n'
                    '{"subagent_type":["builder"]}\n{"subagent_type":5}\n{"subagent_type":true}\n'
                    '{"subagent_type":""}\n{"subagent_type":"bug-gate"}\n{"x":1}\n')
        r, d = self.run_tool("--root", self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(d["spawns"], {"bug-gate": 1, "builder": 1, "corrupt": 5})
        self.assertEqual(r.stderr, "")

    def test_unreadable_root_or_index_says_so_on_stderr(self):
        os.makedirs(os.path.join(self.tmp, "adir"))
        r, d = self.run_tool("--root", os.path.join(self.tmp, "nope"))
        self.assertIn("could not read root", r.stderr)
        self.assertIn("could not read index", r.stderr)  # the index does not exist yet
        r, d = self.run_tool("--root", os.path.join(self.tmp, "adir", "..", "bug-gate"), "--index", os.path.join(self.tmp, "adir"))
        self.assertIn("could not read index", r.stderr)
        self.assertNotIn("could not read root", r.stderr)
        self.assertEqual((r.returncode, d["spawns"]["corrupt"]), (0, 0))
        r, d = self.run_tool("--root", os.path.join(self.tmp, "nope"), "--target", "0")
        self.assertEqual((r.returncode, d["over_target"]), (0, False))
        self.assertTrue(r.stderr.strip())

    def test_empty_index_and_readable_root_stay_silent(self):
        open(self.idx, "w").close()
        r, d = self.run_tool("--root", self.root)
        self.assertEqual((r.returncode, r.stderr, d["gates"]), (0, "", 0))

    def test_missing_index_is_zero_spawns(self):
        _, d = self.run_tool("--root", self.root)
        self.assertEqual(d["spawns"], {"bug-gate": 0, "builder": 0, "corrupt": 0})

    def test_it_reads_only_and_default_paths_follow_the_scratch_home(self):
        env = {k: v for k, v in self.env.items() if k != "GATE_SPAWNS_LOG"}
        base = os.path.join(self.tmp, ".claude", "hubkit")
        os.makedirs(os.path.join(base, "bug-gate", "p"))
        with open(os.path.join(base, "bug-gate", "p", "VERDICT.md"), "w") as f:
            f.write("VERDICT: FAIL\nTICKET: L-0005\nSLICE: S2\n")
        before = sorted(os.listdir(self.tmp))
        r = subprocess.run([sys.executable, TOOL, "--json"], capture_output=True, text=True, env=env)
        self.assertEqual(json.loads(r.stdout)["first_gate_fails"], 1)
        self.assertEqual(sorted(os.listdir(self.tmp)), before)

    def test_text_output_is_short_and_plain(self):
        self.vf("a/VERDICT.md", "FAIL")
        r = subprocess.run([sys.executable, TOOL, "--root", self.root, "--target", "10"], capture_output=True,
                           text=True, env=self.env)
        self.assertEqual(r.returncode, 1)
        out = r.stdout.splitlines()
        self.assertEqual(out[1], "first-gate FAIL share: 1/1 = 100.0%")
        self.assertEqual(out[-1], "OVER TARGET 10.0%")
        self.assertTrue(all(len(x) <= 60 for x in out))

    def test_the_convention_in_bug_gate_md_is_what_the_parser_reads(self):
        with open(BUG_GATE_MD) as f:
            text = f.read()
        block = re.search(r"\n((?:    [A-Z]+: .*\n){4})", text)
        self.assertIsNotNone(block, "bug-gate.md lost its VERDICT.md example")
        lines = [x.strip() for x in block.group(1).splitlines()]
        self.assertEqual([x.split(":")[0] for x in lines], ["VERDICT", "TICKET", "SLICE", "ROUND"])
        p = os.path.join(self.root, "VERDICT.md")
        with open(p, "w") as f:
            f.write("\n".join(x.replace("PENDING|PASS|PASS WITH NOTES|FAIL", "FAIL") for x in lines) + "\n")
        _, d = self.run_tool("--root", self.root)
        self.assertEqual((d["gates"], d["first_gate_fails"]), (1, 1))


if __name__ == "__main__":
    unittest.main(verbosity=1)
