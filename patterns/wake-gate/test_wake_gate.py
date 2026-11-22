"""Tests for wake_gate.py. Run through test.sh (scratch HOME). Python 3.9, stdlib only."""
import json
import os
import pwd
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.environ.get("WAKE_GATE") or os.path.join(HERE, "wake_gate.py")
NOW = 1_800_000_000
DAY = 24 * 3600
REAL_CLAUDE = os.path.join(pwd.getpwuid(os.getuid()).pw_dir, ".claude")


def mtime_snapshot():
    out = {}
    for root, dirs, files in os.walk(REAL_CLAUDE):
        if root.count(os.sep) - REAL_CLAUDE.count(os.sep) > 1:
            dirs[:] = []
        out[root] = os.stat(root).st_mtime_ns
    return out


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.hk = os.path.join(self.tmp.name, "hk")
        self.sd = os.path.join(self.hk, "state")
        os.makedirs(self.sd)
        self.seen = os.path.join(self.sd, "wake-seen.json")

    def tearDown(self):
        os.chmod(self.sd, 0o755)
        self.tmp.cleanup()

    def run_cli(self, *args, now=NOW):
        env = dict(os.environ, HUBKIT_HOME=self.hk, WAKE_GATE_NOW=str(now), PYTHONDONTWRITEBYTECODE="1")
        return subprocess.run([sys.executable, TOOL] + list(args), env=env, capture_output=True, text=True)

    def crit(self, kind="disk", source="nightly", now=NOW, *extra):
        return self.run_cli("--kind", kind, "--source", source, "--severity", "crit", "--summary", "s", *extra, now=now)

    def events(self):
        p = os.path.join(self.sd, "events.jsonl")
        if not os.path.exists(p):
            return []
        with open(p) as fh:
            return [json.loads(x) for x in fh if x.strip()]


class Rules(Base):
    def test_new_crit_wakes(self):
        r = self.crit()
        self.assertEqual((r.returncode, r.stdout.split()[0]), (0, "WAKE"))

    def test_second_wake_same_key_blocked(self):
        self.crit()
        r = self.crit(now=NOW + 3600)
        self.assertEqual((r.returncode, r.stdout.split()[0]), (1, "LOG"))
        r = self.crit(now=NOW + DAY - 1)
        self.assertEqual(r.returncode, 1)

    def test_wakes_again_after_24h(self):
        self.crit()
        self.assertEqual(self.crit(now=NOW + DAY).returncode, 0)

    def test_blocked_repeat_does_not_extend_window(self):
        self.crit()
        self.crit(now=NOW + DAY - 10)
        self.assertEqual(self.crit(now=NOW + DAY).returncode, 0)

    def test_different_key_wakes(self):
        self.crit()
        self.assertEqual(self.crit(source="other", now=NOW + 5).returncode, 0)
        self.assertEqual(self.crit(kind="mem", now=NOW + 5).returncode, 0)

    def test_info_and_warn_never_wake(self):
        for sev in ("info", "warn", "", "CRITICAL", "high"):
            r = self.run_cli("--kind", "k", "--source", "s", "--severity", sev)
            self.assertEqual(r.returncode, 1, sev)
            self.assertTrue(r.stdout.startswith("LOG"), sev)
        self.assertFalse(os.path.exists(self.seen))

    def test_severity_case_and_space_tolerated(self):
        r = self.run_cli("--kind", "k", "--source", "s", "--severity", " CRIT ")
        self.assertEqual(r.returncode, 0)

    def test_dead_lane_with_asks_wakes_even_when_warn(self):
        r = self.run_cli("--kind", "lane", "--source", "s", "--severity", "warn", "--dead-lane-asks")
        self.assertEqual(r.returncode, 0)
        r = self.run_cli("--kind", "dead-lane-holding-operator-asks", "--source", "lane-a", "--severity", "info", now=NOW + 1)
        self.assertEqual(r.returncode, 0)

    def test_dead_lane_also_once_per_24h(self):
        a = ("--kind", "lane", "--source", "s", "--severity", "warn", "--dead-lane-asks")
        self.run_cli(*a)
        self.assertEqual(self.run_cli(*a, now=NOW + 60).returncode, 1)
        self.assertEqual(self.run_cli(*a, now=NOW + DAY).returncode, 0)

    def test_missing_kind_is_usage_error_not_crash(self):
        r = self.run_cli("--severity", "crit")
        self.assertEqual(r.returncode, 2)
        self.assertNotIn("Traceback", r.stderr)

    def test_python_api_missing_fields(self):
        sys.path.insert(0, os.path.dirname(TOOL))
        import importlib
        wg = importlib.import_module("wake_gate")
        os.environ["HUBKIT_HOME"] = self.hk
        try:
            for bad in (None, {}, [], "x", {"severity": "crit"}):
                self.assertIn(wg.gate(bad, now=NOW)["verdict"], ("WAKE", "LOG"))
        finally:
            del os.environ["HUBKIT_HOME"]


class State(Base):
    def test_state_file_content_and_atomic_leftovers(self):
        self.crit()
        with open(self.seen) as fh:
            data = json.load(fh)
        self.assertEqual(data, {"disk|nightly": float(NOW)})
        self.assertEqual([f for f in os.listdir(self.sd) if f.startswith(".wake-seen.")], [])

    def test_old_entries_pruned(self):
        with open(self.seen, "w") as fh:
            json.dump({"old|x": NOW - 8 * DAY, "recent|x": NOW - 100}, fh)
        self.crit()
        with open(self.seen) as fh:
            self.assertEqual(sorted(json.load(fh)), ["disk|nightly", "recent|x"])

    def test_event_line_per_call(self):
        self.crit()
        self.crit(now=NOW + 1)
        self.run_cli("--kind", "k", "--severity", "info")
        evs = [e for e in self.events() if e["event"] == "wake.decision"]
        self.assertEqual([e["verdict"] for e in evs], ["WAKE", "LOG", "LOG"])
        self.assertEqual(evs[0]["key"], "disk|nightly")
        self.assertIn("24h", evs[1]["reason"])

    def test_corrupt_state_still_works_and_is_repaired(self):
        for junk in ("{not json", "[1,2]", "", '{"disk|nightly": "yesterday"}', "\x00\x01"):
            with open(self.seen, "w") as fh:
                fh.write(junk)
            r = self.crit()
            self.assertEqual(r.returncode, 0, junk)
            self.assertNotIn("Traceback", r.stderr)
            with open(self.seen) as fh:
                self.assertIn("disk|nightly", json.load(fh))
            os.unlink(self.seen)

    def test_corrupt_values_do_not_block_other_keys(self):
        with open(self.seen, "w") as fh:
            json.dump({"disk|nightly": NOW - 5, "bad|k": None, "worse|k": [1]}, fh)
        self.assertEqual(self.crit().returncode, 1)

    @unittest.skipIf(os.getuid() == 0, "root ignores permissions")
    def test_unwritable_state_logs_and_prints_the_crit(self):
        os.chmod(self.sd, 0o555)
        r = self.run_cli("--kind", "disk", "--source", "nightly", "--severity", "crit", "--summary", "disk 99 percent")
        self.assertEqual(r.returncode, 1)
        self.assertTrue(r.stdout.startswith("LOG"))
        self.assertNotIn("Traceback", r.stderr)
        self.assertIn("UNRECORDED", r.stderr)
        self.assertIn("disk 99 percent", r.stderr)

    @unittest.skipIf(os.getuid() == 0, "root ignores permissions")
    def test_unwritable_state_info_stays_quiet(self):
        os.chmod(self.sd, 0o555)
        r = self.run_cli("--kind", "k", "--severity", "info")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(r.stderr, "")

    def test_state_dir_is_a_file(self):
        os.rmdir(self.sd) if not os.listdir(self.sd) else None
        with open(self.sd, "w") as fh:
            fh.write("x")
        r = self.crit()
        self.assertEqual(r.returncode, 1)
        self.assertIn("UNRECORDED", r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        os.unlink(self.sd)
        os.makedirs(self.sd)


class Keys(Base):
    def test_newline_key_is_one_line_and_dedups(self):
        r = self.run_cli("--kind", "a\nb", "--source", "s\r\nx", "--severity", "crit", "--summary", "l1\nl2")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(len(r.stdout.strip().splitlines()), 1)
        r = self.run_cli("--kind", "a b", "--source", "s x", "--severity", "crit", now=NOW + 5)
        self.assertEqual(r.returncode, 1)
        with open(self.seen) as fh:
            self.assertEqual(list(json.load(fh)), ["a b|s x"])
        with open(os.path.join(self.sd, "events.jsonl")) as fh:
            lines = fh.read().splitlines()
        self.assertEqual(len(lines), 2)
        for ln in lines:
            json.loads(ln)

    def test_newline_cannot_forge_another_key(self):
        self.crit(kind="x", source="y")
        r = self.run_cli("--kind", "x", "--source", "y\nz", "--severity", "crit", now=NOW + 5)
        self.assertEqual(r.returncode, 0)

    def test_subject_separates_keys(self):
        a = ("--kind", "k", "--source", "s", "--severity", "crit")
        self.assertEqual(self.run_cli(*a, "--subject", "one").returncode, 0)
        self.assertEqual(self.run_cli(*a, "--subject", "two", now=NOW + 1).returncode, 0)
        self.assertEqual(self.run_cli(*a, "--subject", "one", now=NOW + 2).returncode, 1)

    def test_huge_key_truncated(self):
        r = self.run_cli("--kind", "k" * 5000, "--source", "s", "--severity", "crit")
        self.assertEqual(r.returncode, 0)
        self.assertLess(len(r.stdout), 400)


class Clock(Base):
    def test_clock_backwards_within_window_still_blocks(self):
        self.crit()
        self.assertEqual(self.crit(now=NOW - 3600).returncode, 1)

    def test_far_future_stamp_is_bogus_and_wakes(self):
        with open(self.seen, "w") as fh:
            json.dump({"disk|nightly": NOW + 400 * DAY}, fh)
        self.assertEqual(self.crit().returncode, 0)
        with open(self.seen) as fh:
            self.assertEqual(json.load(fh)["disk|nightly"], float(NOW))

    def test_bad_clock_env_falls_back(self):
        env = dict(os.environ, HUBKIT_HOME=self.hk, WAKE_GATE_NOW="garbage")
        r = subprocess.run([sys.executable, TOOL, "--kind", "k", "--severity", "crit"], env=env,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0)


class Isolation(Base):
    def test_real_claude_dir_untouched(self):
        before = mtime_snapshot()
        self.crit()
        self.crit(now=NOW + 1)
        self.assertEqual(mtime_snapshot(), before)


if __name__ == "__main__":
    unittest.main(verbosity=1)
