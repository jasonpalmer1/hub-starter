"""Tests for work_loop.py tick/touch/lane/hold. Run through test.sh (scratch HOME). Python 3.9, stdlib only."""
import json
import os
import pwd
import subprocess
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.environ.get("WORK_LOOP") or os.path.join(HERE, "work_loop.py")
NOW = 1_800_000_000


def ev(tid, **kw):
    e = {"event": "created", "id": tid, "title": "t " + tid, "ts": "2026-01-01T00:00:00Z", "asked_by": "hub"}
    e.update(kw)
    return e


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.hk = os.path.join(self.tmp.name, "hk")
        self.sd = os.path.join(self.hk, "state")
        os.makedirs(self.sd)
        self.ledger = os.path.join(self.sd, "ledger.jsonl")
        self.env = dict(os.environ, HUBKIT_HOME=self.hk, PYTHONDONTWRITEBYTECODE="1")
        self.env.pop("LEDGER_PATH", None)
        self.write([ev("L-0001")])

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, events):
        with open(self.ledger, "w") as f:
            for e in events:
                f.write(json.dumps(e) + "\n")

    def wl(self, *args, now=NOW, extra=None):
        env = dict(self.env, WORK_LOOP_NOW=str(now))
        env.update(extra or {})
        return subprocess.run([sys.executable, TOOL] + list(args), capture_output=True, text=True, env=env)

    def lane(self, name="lane-a", *extra):
        r = self.wl("lane", "add", name, *extra)
        self.assertEqual(r.returncode, 0, r.stderr)

    def touch(self, name="lane-a", at=NOW - 11 * 60):
        r = self.wl("touch", "--lane", name, now=at)
        self.assertEqual(r.returncode, 0, r.stderr)

    def inbox(self, name="lane-a"):
        p = os.path.join(self.sd, "inbox", name + ".txt")
        return open(p).read() if os.path.exists(p) else None

    def events(self):
        p = os.path.join(self.sd, "events.jsonl")
        return [json.loads(x) for x in open(p)] if os.path.exists(p) else []

    def snapshot(self):
        out = {}
        for root, dirs, files in os.walk(self.hk):
            for n in dirs + files:
                p = os.path.join(root, n)
                out[p] = os.stat(p).st_mtime_ns
        return out


class BlockTests(Base):
    def test_lane_idle_under_10_min_not_fed(self):
        self.lane(); self.touch(at=NOW - 599)
        r = self.wl("tick")
        self.assertIn("active 9m ago", r.stdout)
        self.assertIsNone(self.inbox())

    def test_second_feed_inside_30_min_blocked(self):
        self.write([ev("L-0001"), ev("L-0002")])
        self.lane(); self.touch()
        self.wl("tick")
        self.assertIn("L-0001", self.inbox())
        r = self.wl("tick", now=NOW + 30 * 60 - 1)          # lane still idle, 29m59s later
        self.assertIn("fed 29m ago", r.stdout)
        self.assertIn("L-0001", self.inbox())
        r = self.wl("tick", now=NOW + 30 * 60)              # exactly 30 min: allowed again
        self.assertIn("L-0002", self.inbox())

    def test_held_lane_not_fed(self):
        self.lane(); self.touch()
        self.assertEqual(self.wl("hold", "--lane", "lane-a", "--minutes", "60").returncode, 0)
        r = self.wl("tick")
        self.assertIn("held", r.stdout)
        self.assertIsNone(self.inbox())

    def test_corrupt_hold_file_counts_as_held(self):
        self.lane(); self.touch()
        os.makedirs(os.path.join(self.sd, "holds"))
        open(os.path.join(self.sd, "holds", "lane-a.json"), "w").write("{nope")
        self.wl("tick")
        self.assertIsNone(self.inbox())

    def test_hold_needs_expiry(self):
        self.lane()
        self.assertEqual(self.wl("hold", "--lane", "lane-a").returncode, 2)
        self.assertEqual(self.wl("hold", "--lane", "lane-a", "--minutes", "0").returncode, 2)

    def test_ticket_claimed_between_pick_and_send_is_not_sent(self):
        self.lane(); self.touch()

        def claim():
            time.sleep(0.5)
            with open(self.ledger, "a") as f:
                f.write(json.dumps({"event": "status", "id": "L-0001", "status": "active", "ts": "2026-01-02T00:00:00Z"}) + "\n")
        th = threading.Thread(target=claim); th.start()
        r = self.wl("tick", extra={"WORK_LOOP_PRESEND_SLEEP_S": "1.5"})
        th.join()
        self.assertIn("taken before send", r.stdout)
        self.assertIsNone(self.inbox())
        self.assertFalse(os.path.exists(os.path.join(self.sd, "loop-feed.json")))   # no offer recorded
        self.assertEqual([e for e in self.events() if e["event"] == "loop.feed"], [])

    def test_bad_lane_names_refused(self):
        for bad in ("../x", "a;rm", "A B", "a" * 33, "$(id)", "a/b", ""):
            r = self.wl("lane", "add", bad)
            self.assertEqual(r.returncode, 2, bad)
            self.assertEqual(self.wl("touch", "--lane", bad).returncode, 2, bad)
            self.assertEqual(self.wl("hold", "--lane", bad, "--minutes", "5").returncode, 2, bad)
            self.assertEqual(self.wl("next", "--lane", bad).returncode, 2, bad)
        self.assertFalse(os.path.exists(os.path.join(self.sd, "lanes.json")))
        self.assertFalse(os.path.exists(os.path.join(self.hk, "x")))

    def test_hand_edited_bad_lane_in_registry_is_skipped(self):
        json.dump({"lanes": {"../x": {}, "a;rm": {}, "lane-a": {}}}, open(os.path.join(self.sd, "lanes.json"), "w"))
        self.touch()
        r = self.wl("tick")
        self.assertEqual(r.stdout.count("bad lane name"), 2)
        self.assertIn("L-0001", self.inbox())
        self.assertFalse(os.path.exists(os.path.join(self.sd, "inbox", "..")) and os.path.exists(os.path.join(self.hk, "x.txt")))

    def test_shell_metacharacters_in_cmd_refused(self):
        for bad in ("echo;id", "a|b", "$(id)", "`id`", "a&b", "a>b", "x y", "a\nb", "*"):
            r = self.wl("lane", "add", "lane-a", "--cmd", bad)
            self.assertEqual(r.returncode, 2, bad)
        self.assertEqual(self.wl("lane", "add", "lane-a", "--cmd", "/bin/echo", "--cmd", "$(id)").returncode, 2)
        self.assertFalse(os.path.exists(os.path.join(self.sd, "lanes.json")))

    def test_hand_edited_bad_cmd_never_runs(self):
        mark = os.path.join(self.tmp.name, "pwned")
        json.dump({"lanes": {"lane-a": {"deliver": {"type": "cmd", "argv": ["/bin/sh", "-c", "touch " + mark]}}}},
                  open(os.path.join(self.sd, "lanes.json"), "w"))
        self.touch()
        r = self.wl("tick")
        self.assertIn("argv refused", r.stdout)
        self.assertFalse(os.path.exists(mark))
        self.assertEqual([e["event"] for e in self.events()], ["loop.feed_failed"])

    def test_future_touch_is_not_idle_and_reported(self):
        self.lane(); self.touch(at=NOW + 3600)
        r = self.wl("tick")
        self.assertIn("clock skew", r.stdout)
        self.assertIn("ahead of the clock", r.stdout)
        self.assertIsNone(self.inbox())

    def test_never_touched_lane_not_fed(self):
        self.lane()
        self.assertIn("never touched", self.wl("tick").stdout)
        self.assertIsNone(self.inbox())

    def test_deleted_lane_not_fed_and_files_gone(self):
        self.lane(); self.touch()
        self.wl("hold", "--lane", "lane-a", "--minutes", "5")
        self.assertEqual(self.wl("lane", "remove", "lane-a").returncode, 0)
        self.assertFalse(os.path.exists(os.path.join(self.sd, "touch", "lane-a")))
        self.assertEqual(self.wl("touch", "--lane", "lane-a").returncode, 2)     # unknown now
        self.lane("lane-b"); self.touch("lane-b")
        r = self.wl("tick")
        self.assertNotIn("lane-a", r.stdout)
        self.assertIsNone(self.inbox("lane-a"))

    def test_overlapping_ticks_only_one_feeds(self):
        self.write([ev("L-0001"), ev("L-0002")])
        self.lane(); self.lane("lane-b"); self.touch(); self.touch("lane-b")
        env = dict(self.env, WORK_LOOP_NOW=str(NOW), WORK_LOOP_PRESEND_SLEEP_S="1.5")
        p1 = subprocess.Popen([sys.executable, TOOL, "tick"], env=env, stdout=subprocess.PIPE, text=True)
        time.sleep(0.4)
        p2 = subprocess.Popen([sys.executable, TOOL, "tick"], env=env, stdout=subprocess.PIPE, text=True)
        o1, o2 = p1.communicate()[0], p2.communicate()[0]
        self.assertIn("another tick is running", o1 + o2)
        feeds = [e for e in self.events() if e["event"] == "loop.feed"]
        self.assertEqual(len(feeds), 2)                                 # one tick, two lanes, two tickets
        self.assertEqual(len({e["ticket"] for e in feeds}), 2)


class AllowTests(Base):
    def test_idle_lane_open_ticket_inbox_holds_id(self):
        self.lane(); self.touch()
        r = self.wl("tick")
        self.assertIn("fed lane-a L-0001", r.stdout)
        self.assertTrue(self.inbox().startswith("L-0001\t"))
        names = [e["event"] for e in self.events()]
        self.assertEqual(names, ["loop.next", "loop.feed"])

    def test_exactly_10_min_idle_is_fed(self):
        self.lane(); self.touch(at=NOW - 600)
        self.wl("tick")
        self.assertIn("L-0001", self.inbox())

    def test_hold_expired_is_fed(self):
        self.lane(); self.touch()
        self.wl("hold", "--lane", "lane-a", "--minutes", "5", now=NOW - 400)    # expired 100 s ago
        self.wl("tick")
        self.assertIn("L-0001", self.inbox())

    def test_hold_clear(self):
        self.lane(); self.touch()
        self.wl("hold", "--lane", "lane-a", "--minutes", "60")
        self.wl("hold", "--lane", "lane-a", "--clear")
        self.wl("tick")
        self.assertIn("L-0001", self.inbox())

    def test_cmd_adapter_runs_argv_without_shell(self):
        out = os.path.join(self.tmp.name, "got.txt")
        script = os.path.join(self.tmp.name, "deliver.sh")
        open(script, "w").write('#!/bin/sh\nprintf "%s %s" "$WORK_LOOP_LANE" "$WORK_LOOP_TICKET" > "$1"\n')
        os.chmod(script, 0o755)
        self.lane("lane-a", "--cmd", script, "--cmd", out)
        self.touch()
        self.wl("tick")
        self.assertEqual(open(out).read(), "lane-a L-0001")
        self.assertIsNone(self.inbox())

    def test_failed_cmd_is_not_recorded_so_next_tick_retries(self):
        self.lane("lane-a", "--cmd", "/bin/false"); self.touch()
        r = self.wl("tick")
        self.assertIn("delivery failed", r.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.sd, "loop-feed.json")))
        self.assertEqual([e["event"] for e in self.events()], ["loop.feed_failed"])

    def test_title_with_control_chars_is_cleaned(self):
        self.write([ev("L-0001", title="a\nb\x1b[31mred")])
        self.lane(); self.touch(); self.wl("tick")
        self.assertEqual(self.inbox().count("\n"), 1)
        self.assertNotIn("\x1b", self.inbox())

    def test_two_lanes_get_two_tickets(self):
        self.write([ev("L-0001"), ev("L-0002")])
        self.lane(); self.lane("lane-b"); self.touch(); self.touch("lane-b")
        self.wl("tick")
        self.assertEqual({self.inbox().split("\t")[0], self.inbox("lane-b").split("\t")[0]}, {"L-0001", "L-0002"})

    def test_heartbeat_written_in_deadman_format_even_with_no_lanes(self):
        self.wl("tick")
        beat = open(os.path.join(self.sd, "heartbeats", "work-loop-tick")).read().split()
        self.assertEqual(beat, [str(NOW), "1800"])
        self.assertEqual(open(os.path.join(self.sd, "registry", "work-loop-tick")).read().strip(), "1800")

    def test_missing_state_dir_does_not_crash(self):
        env = dict(self.env, HUBKIT_HOME=os.path.join(self.tmp.name, "nope"), WORK_LOOP_NOW=str(NOW))
        for args in (["tick"], ["tick", "--dry-run"]):
            r = subprocess.run([sys.executable, TOOL] + args, capture_output=True, text=True, env=env)
            self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(subprocess.run([sys.executable, TOOL, "touch", "--lane", "a"], env=env).returncode, 0)

    def test_touch_unregistered_ok_without_registry(self):
        self.assertEqual(self.wl("touch", "--lane", "solo").returncode, 0)

    def test_next_keeps_last_feed_memory(self):
        self.write([ev("L-0001"), ev("L-0002")])
        self.lane(); self.touch(); self.wl("tick")
        self.wl("next", "--lane", "lane-a")
        r = self.wl("tick", now=NOW + 60)
        self.assertIn("fed 1m ago", r.stdout)


class DryRunTests(Base):
    def test_dry_run_writes_nothing(self):
        self.lane(); self.touch(); self.wl("hold", "--lane", "lane-b", "--minutes", "5")
        before = self.snapshot()
        time.sleep(0.05)
        r = self.wl("tick", "--dry-run")
        self.assertIn("would-feed lane-a L-0001", r.stdout)
        self.assertEqual(self.snapshot(), before)           # same files, same mtimes, no new ones

    def test_dry_run_on_starved_lane_writes_no_finding(self):
        self.write([ev("L-0001", owner="other")])
        self.lane(); self.touch(at=NOW - 3600)
        before = self.snapshot()
        r = self.wl("tick", "--dry-run")
        self.assertIn("STARVED", r.stdout)
        self.assertEqual(self.snapshot(), before)


class StarvationTests(Base):
    def setUp(self):
        super().setUp()
        self.write([ev("L-0001", owner="someone-else")])    # open work exists, none for this lane
        self.lane(); self.touch(at=NOW - 40 * 60)

    def findings(self):
        d = os.path.join(self.sd, "findings")
        return sorted(os.listdir(d)) if os.path.isdir(d) else []

    def starved_events(self):
        return [e for e in self.events() if e["event"] == "loop.starved"]

    def test_starvation_reported_once_per_24h(self):
        r = self.wl("tick")
        self.assertIn("STARVED lane-a", r.stdout)
        self.assertEqual(self.findings(), ["starved-lane-a.json"])
        self.assertEqual(len(self.starved_events()), 1)
        self.wl("tick", now=NOW + 3600)
        self.wl("tick", now=NOW + 24 * 3600 - 1)
        self.assertEqual(len(self.starved_events()), 1)
        self.touch(at=NOW + 24 * 3600 - 3600 * 2)
        r = self.wl("tick", now=NOW + 24 * 3600 + 3 * 3600)
        self.assertEqual(len(self.starved_events()), 2)

    def wake_decisions(self):
        return [e for e in self.events() if e["event"] == "wake.decision"]

    def test_starvation_goes_through_wake_gate(self):
        r = self.wl("tick")
        self.assertIn("wake-gate WAKE: starved|lane-a", r.stdout)
        d = self.wake_decisions()
        self.assertEqual([e["verdict"] for e in d], ["WAKE"])
        with open(os.path.join(self.sd, "wake-seen.json")) as fh:
            self.assertEqual(list(json.load(fh)), ["starved|lane-a"])
        with open(os.path.join(self.sd, "findings", "starved-lane-a.json")) as fh:
            self.assertEqual(json.load(fh)["wake_gate"], "WAKE")

    def test_starvation_key_woke_recently_is_logged_not_woken(self):
        with open(os.path.join(self.sd, "wake-seen.json"), "w") as fh:
            json.dump({"starved|lane-a": NOW - 100}, fh)
        r = self.wl("tick")
        self.assertIn("wake-gate LOG", r.stdout)
        self.assertEqual([e["verdict"] for e in self.wake_decisions()], ["LOG"])
        with open(os.path.join(self.sd, "findings", "starved-lane-a.json")) as fh:
            self.assertEqual(json.load(fh)["wake_gate"], "LOG")

    def test_dry_run_starvation_asks_wake_gate_nothing(self):
        self.wl("tick", "--dry-run")
        self.assertEqual(self.wake_decisions(), [])
        self.assertFalse(os.path.exists(os.path.join(self.sd, "wake-seen.json")))

    def test_without_wake_gate_installed_finding_is_still_written(self):
        solo = os.path.join(self.tmp.name, "solo")
        os.makedirs(solo)
        import shutil
        for f in ("work_loop.py",):
            shutil.copy(os.path.join(os.path.dirname(TOOL), f), solo)
        shutil.copy(os.path.join(os.path.dirname(TOOL), "..", "ledger", "ledger_lib.py"), solo) \
            if os.path.isfile(os.path.join(os.path.dirname(TOOL), "..", "ledger", "ledger_lib.py")) else \
            shutil.copy(os.path.join(os.path.dirname(TOOL), "ledger_lib.py"), solo)
        env = dict(self.env, WORK_LOOP_NOW=str(NOW))
        r = subprocess.run([sys.executable, os.path.join(solo, "work_loop.py"), "tick"],
                           capture_output=True, text=True, env=env)
        self.assertIn("STARVED lane-a", r.stdout)
        self.assertEqual(self.findings(), ["starved-lane-a.json"])
        self.assertEqual(self.wake_decisions(), [])
        with open(os.path.join(self.sd, "findings", "starved-lane-a.json")) as fh:
            self.assertEqual(json.load(fh)["wake_gate"], "NO-GATE")

    def test_under_30_min_idle_is_not_starved(self):
        self.touch(at=NOW - 20 * 60)
        self.assertNotIn("STARVED", self.wl("tick").stdout)
        self.assertEqual(self.findings(), [])

    def test_empty_queue_is_idle_declared_not_starved(self):
        self.write([ev("L-0001"), {"event": "status", "id": "L-0001", "status": "done", "ts": "2026-01-02T00:00:00Z"}])
        r = self.wl("tick")
        self.assertIn("IDLE", r.stdout)
        self.assertNotIn("STARVED", r.stdout)
        self.assertEqual([e["event"] for e in self.events()], ["loop.idle_declared"])
        self.wl("tick", now=NOW + 600)
        self.assertEqual(len(self.events()), 1)             # declared once per 24 h


class SafetyTests(Base):
    def test_real_dot_claude_never_touched(self):
        home = pwd.getpwuid(os.getuid()).pw_dir
        probe = [os.path.join(home, ".claude", n) for n in ("hub", "hubkit", "settings.json")]
        before = [os.stat(p).st_mtime_ns if os.path.exists(p) else None for p in probe]
        self.lane(); self.touch(); self.wl("tick"); self.wl("tick", "--dry-run")
        after = [os.stat(p).st_mtime_ns if os.path.exists(p) else None for p in probe]
        self.assertEqual(before, after)
        self.assertNotIn(os.path.join(home, ".claude"), os.path.realpath(self.hk))

    def test_broken_registry_fails_loud(self):
        open(os.path.join(self.sd, "lanes.json"), "w").write("{oops")
        self.assertEqual(self.wl("tick").returncode, 2)
        self.assertEqual(self.wl("tick", "--dry-run").returncode, 2)

    def test_lane_add_duplicate_and_list(self):
        self.lane(); self.lane("lane-b", "--cmd", "/bin/true")
        self.assertEqual(self.wl("lane", "add", "lane-a").returncode, 2)
        self.assertEqual(self.wl("lane", "list").stdout.split("\n")[:2], ["lane-a\tfile", "lane-b\tcmd"])
        self.assertEqual(self.wl("lane", "remove", "nope").returncode, 2)


if __name__ == "__main__":
    unittest.main(verbosity=1)
