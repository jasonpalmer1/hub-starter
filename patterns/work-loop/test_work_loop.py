"""Tests for work_loop.py. Run through test.sh (scratch HOME). Python 3.9, stdlib only."""
import json
import os
import pwd
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.environ.get("WORK_LOOP") or os.path.join(HERE, "work_loop.py")
NOW = 1_800_000_000


def ev(tid, **kw):
    e = {"event": "created", "id": tid, "title": "t " + tid, "ts": "2026-01-01T00:00:00Z", "asked_by": "hub"}
    e.update(kw)
    return e


def status(tid, st):
    return {"event": "status", "id": tid, "status": st, "ts": "2026-01-02T00:00:00Z"}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.hk = os.path.join(self.tmp.name, "hk")
        os.makedirs(os.path.join(self.hk, "state"))
        self.ledger = os.path.join(self.hk, "state", "ledger.jsonl")
        self.env = dict(os.environ, HUBKIT_HOME=self.hk, WORK_LOOP_NOW=str(NOW), PYTHONDONTWRITEBYTECODE="1")
        self.env.pop("LEDGER_PATH", None)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, events, raw=()):
        with open(self.ledger, "w") as f:
            for e in events:
                f.write(json.dumps(e) + "\n")
            for r in raw:
                f.write(r + "\n")

    def run_next(self, lane, *extra, env=None):
        return subprocess.run([sys.executable, TOOL, "next", "--lane", lane] + list(extra),
                              capture_output=True, text=True, env=env or self.env)

    def next_id(self, lane, *extra):
        r = self.run_next(lane, *extra)
        return r.stdout.strip() if r.returncode == 0 else "rc%d" % r.returncode


class BlockTests(Base):
    def test_owned_by_other_lane_is_never_returned(self):
        self.write([ev("L-0001", owner="lane-b")])
        self.assertEqual(self.next_id("lane-a"), "rc3")

    def test_active_is_never_returned(self):
        self.write([ev("L-0001"), status("L-0001", "active")])
        self.assertEqual(self.next_id("lane-a"), "rc3")

    def test_needs_operator_done_dropped_never_returned(self):
        self.write([ev("L-0001"), status("L-0001", "needs-operator"), ev("L-0002"), status("L-0002", "done"),
                    ev("L-0003"), status("L-0003", "dropped")])
        self.assertEqual(self.next_id("lane-a"), "rc3")

    def test_trigger_bearing_never_returned(self):
        self.write([ev("L-0001", trigger="when the vendor replies")])
        self.assertEqual(self.next_id("lane-a"), "rc3")

    def test_offered_within_24h_blocked_then_allowed_after(self):
        self.write([ev("L-0001")])
        self.assertEqual(self.next_id("lane-a"), "L-0001")
        self.assertEqual(self.next_id("lane-a"), "rc3")
        self.assertEqual(self.next_id("lane-b"), "rc3")      # not offered twice to anyone
        later = dict(self.env, WORK_LOOP_NOW=str(NOW + 24 * 3600 + 1))
        self.assertEqual(self.run_next("lane-a", env=later).stdout.strip(), "L-0001")

    def test_just_inside_24h_still_blocked(self):
        self.write([ev("L-0001")])
        self.next_id("lane-a")
        edge = dict(self.env, WORK_LOOP_NOW=str(NOW + 24 * 3600 - 1))
        self.assertEqual(self.run_next("lane-a", env=edge).returncode, 3)

    def test_peek_does_not_record(self):
        self.write([ev("L-0001")])
        self.assertEqual(self.next_id("lane-a", "--peek"), "L-0001")
        self.assertEqual(self.next_id("lane-a"), "L-0001")


class AllowTests(Base):
    def test_unowned_open_is_returned(self):
        self.write([ev("L-0001")])
        self.assertEqual(self.next_id("lane-a"), "L-0001")

    def test_owned_by_this_lane_beats_unowned(self):
        self.write([ev("L-0001"), ev("L-0002", owner="lane-a")])
        self.assertEqual(self.next_id("lane-a"), "L-0002")

    def test_dash_lane_suffix_matches_owner(self):
        self.write([ev("L-0001"), ev("L-0002", owner="a")])
        self.assertEqual(self.next_id("a-lane"), "L-0002")

    def test_operator_asked_first_within_group(self):
        self.write([ev("L-0001"), ev("L-0002", asked_by="operator"), ev("L-0003", asked_by="operator")])
        self.assertEqual(self.next_id("lane-a"), "L-0002")

    def test_ties_broken_by_number_not_text(self):
        self.write([ev("L-10"), ev("L-2"), ev("L-9")])
        self.assertEqual([self.next_id("lane-a") for _ in range(3)], ["L-2", "L-9", "L-10"])

    def test_exit_3_when_none_and_when_no_ledger(self):
        self.assertEqual(self.next_id("lane-a"), "rc3")
        self.write([])
        self.assertEqual(self.next_id("lane-a"), "rc3")

    def test_json_output(self):
        self.write([ev("L-0001")])
        out = json.loads(self.run_next("lane-a", "--json").stdout)
        self.assertEqual((out["ticket"], out["lane"], out["skipped_lines"]), ("L-0001", "lane-a", 0))


class AttackTests(Base):
    def test_concurrent_callers_get_different_tickets(self):
        self.write([ev("L-%04d" % i) for i in range(1, 9)])
        env = dict(self.env, WORK_LOOP_HOLD_S="0.2")
        procs = [subprocess.Popen([sys.executable, TOOL, "next", "--lane", "lane-%d" % i],
                                  stdout=subprocess.PIPE, text=True, env=env) for i in range(8)]
        got = [p.communicate()[0].strip() for p in procs]
        self.assertEqual(len(set(got)), 8, got)
        self.assertTrue(all(g.startswith("L-") for g in got), got)

    def test_owner_case_and_whitespace_normalized(self):
        self.write([ev("L-0001", owner="Lane-A "), ev("L-0002", owner="LANE-B")])
        self.assertEqual(self.next_id(" lane-a"), "L-0001")
        self.assertEqual(self.next_id("Lane-A"), "rc3")      # L-0002 belongs to lane-b

    def test_ticket_id_inside_another_title_means_nothing(self):
        self.write([ev("L-0001"), status("L-0001", "done"),
                    ev("L-0002", title="follow-up to L-0001 and L-0003"), ev("L-0003", owner="lane-b")])
        self.assertEqual(self.next_id("lane-a"), "L-0002")
        self.assertEqual(self.next_id("lane-a"), "rc3")       # L-0001 done, L-0003 not ours

    def test_corrupt_line_is_skipped_and_reported(self):
        self.write([ev("L-0001")], raw=["{not json", "[1,2]", ""])
        r = self.run_next("lane-a")
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "L-0001"))
        self.assertIn("skipped 2 unreadable ledger line(s)", r.stderr)

    def test_100k_line_ledger_under_2_seconds(self):
        evs = []
        for i in range(1, 25001):
            tid = "L-%04d" % i
            evs.append(ev(tid))
            evs.append(status(tid, "active"))
            evs.append({"event": "note", "id": tid, "note": "x", "ts": "2026-01-03T00:00:00Z"})
            evs.append(status(tid, "done"))
        self.write(evs)
        self.assertEqual(sum(1 for _ in open(self.ledger)), 100000)
        t = time.time()
        r = self.run_next("lane-a")
        dt = time.time() - t
        self.assertEqual(r.returncode, 3)
        self.assertLess(dt, 2.0, "took %.2fs" % dt)

    def test_real_dot_claude_never_touched(self):
        real = os.path.join(pwd.getpwuid(os.getuid()).pw_dir, ".claude", "hub", "ledger.jsonl")
        before = os.stat(real).st_mtime_ns if os.path.exists(real) else None
        self.write([ev("L-0001")])
        self.next_id("lane-a")
        after = os.stat(real).st_mtime_ns if os.path.exists(real) else None
        self.assertEqual(before, after)
        self.assertNotIn(os.path.join(pwd.getpwuid(os.getuid()).pw_dir, ".claude"),
                         os.path.realpath(self.hk))


class RegistryTests(Base):
    def reg(self, obj):
        with open(os.path.join(self.hk, "state", "lanes.json"), "w") as f:
            json.dump(obj, f)

    def test_unknown_lane_refused_when_registry_exists(self):
        self.write([ev("L-0001")])
        self.reg({"lanes": {"lane-a": {}}})
        r = self.run_next("lane-z")
        self.assertEqual(r.returncode, 2)
        self.assertIn("not in lanes.json", r.stderr)

    def test_alias_and_project_filter(self):
        self.write([ev("L-0001", project="other"), ev("L-0002", owner="al", project="site"), ev("L-0003", project="site")])
        self.reg({"lanes": {"Lane-A": {"aliases": ["AL"], "projects": ["site"]}}})
        self.assertEqual(self.next_id("lane-a"), "L-0002")
        self.assertEqual(self.next_id("lane-a"), "L-0003")
        self.assertEqual(self.next_id("lane-a"), "rc3")      # L-0001 is another project

    def test_broken_registry_fails_loud(self):
        self.write([ev("L-0001")])
        with open(os.path.join(self.hk, "state", "lanes.json"), "w") as f:
            f.write("{oops")
        self.assertEqual(self.run_next("lane-a").returncode, 2)

    def test_lanes_command_lists(self):
        self.reg({"lanes": {"b": {}, "a": {}}})
        r = subprocess.run([sys.executable, TOOL, "lanes"], capture_output=True, text=True, env=self.env)
        self.assertEqual(r.stdout.split(), ["a", "b"])

    def test_missing_lane_arg_is_usage_error(self):
        r = subprocess.run([sys.executable, TOOL, "next"], capture_output=True, text=True, env=self.env)
        self.assertEqual(r.returncode, 2)


if __name__ == "__main__":
    unittest.main(verbosity=1)
