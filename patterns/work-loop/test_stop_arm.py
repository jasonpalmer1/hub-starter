"""Tests for stop_arm.py (the optional Stop hook). Run through test.sh (scratch HOME). Python 3.9, stdlib only."""
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.environ.get("STOP_ARM") or os.path.join(HERE, "stop_arm.py")
NOW = 1_800_000_000


def ev(tid, **kw):
    e = {"event": "created", "id": tid, "title": "t " + tid, "ts": "2026-01-01T00:00:00Z", "asked_by": "hub"}
    e.update(kw)
    return e


class Arm(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.hk = os.path.join(self.tmp.name, "hk")
        self.sd = os.path.join(self.hk, "state")
        os.makedirs(self.sd)
        self.ledger = os.path.join(self.sd, "ledger.jsonl")
        self.env = dict(os.environ, HUBKIT_HOME=self.hk, HUBKIT_LANE="lane-a", WORK_LOOP_NOW=str(NOW),
                        PYTHONDONTWRITEBYTECODE="1")
        self.env.pop("LEDGER_PATH", None)
        self.registry(["lane-a"])
        self.write([ev("L-0001"), ev("L-0002"), ev("L-0003"), ev("L-0004"), ev("L-0005")])

    def tearDown(self):
        self.tmp.cleanup()

    def registry(self, names):
        with open(os.path.join(self.sd, "lanes.json"), "w") as f:
            json.dump({"lanes": {n: {} for n in names}}, f)

    def write(self, events):
        with open(self.ledger, "w") as f:
            for e in events:
                f.write(json.dumps(e) + "\n")

    def stop(self, payload=None, raw=None, now=NOW, **env):
        e = dict(self.env, WORK_LOOP_NOW=str(now))
        for k, v in env.items():
            if v is None:
                e.pop(k, None)
            else:
                e[k] = v
        data = raw if raw is not None else json.dumps(payload if payload is not None else {"stop_hook_active": False})
        r = subprocess.run([sys.executable, TOOL], input=data, capture_output=True, text=True, env=e)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout) if r.stdout.strip() else None

    def assertBlocks(self, out, tid=None):
        self.assertIsNotNone(out, "expected a continuation")
        self.assertEqual(out["decision"], "block")
        if tid:
            self.assertIn(tid, out["reason"])

    # ---- continues
    def test_continues_with_next_ticket(self):
        out = self.stop()
        self.assertBlocks(out, "L-0001")
        self.assertEqual(set(out), {"decision", "reason"})

    def test_second_stop_gets_a_different_ticket(self):
        self.assertBlocks(self.stop(), "L-0001")
        self.assertBlocks(self.stop(), "L-0002")

    # ---- must not continue
    def test_cap_of_three(self):
        for tid in ("L-0001", "L-0002", "L-0003"):
            self.assertBlocks(self.stop(), tid)
        self.assertIsNone(self.stop())          # the 4th stop is allowed
        self.assertIsNone(self.stop({"stop_hook_active": False}))   # a payload saying "false" cannot bypass it
        self.assertIsNone(self.stop({"stop_hook_active": True}))

    def test_cap_resets_after_a_proof_backed_close(self):
        for _ in range(3):
            self.stop()
        self.assertIsNone(self.stop())
        self.write([ev("L-0001"), ev("L-0002"), ev("L-0003"), ev("L-0004"), ev("L-0005"),
                    {"event": "proof", "id": "L-0001", "text": "done in abc", "ts": "2026-01-02T00:00:00Z"},
                    {"event": "status", "id": "L-0001", "status": "done", "ts": "2026-01-02T00:00:01Z"}])
        self.assertBlocks(self.stop(), "L-0004")

    def test_not_after_an_unproven_close_attempt(self):
        for _ in range(3):
            self.stop()
        self.write([ev("L-0001"), ev("L-0002"), ev("L-0003"), ev("L-0004")])   # still open: no close
        self.assertIsNone(self.stop())

    def test_active_claim_allows_stop(self):
        self.write([ev("L-0001", owner="lane-a"),
                    {"event": "status", "id": "L-0001", "status": "active", "ts": "2026-01-02T00:00:00Z"},
                    ev("L-0002")])
        self.assertIsNone(self.stop())

    def test_other_lanes_claim_does_not_stop_this_lane(self):
        self.write([ev("L-0001", owner="lane-b"),
                    {"event": "status", "id": "L-0001", "status": "active", "ts": "2026-01-02T00:00:00Z"},
                    ev("L-0002")])
        self.assertBlocks(self.stop(), "L-0002")

    def test_loop_pause_file(self):
        open(os.path.join(self.sd, "loop-pause"), "w").close()
        self.assertIsNone(self.stop())
        os.unlink(os.path.join(self.sd, "loop-pause"))
        self.assertBlocks(self.stop(), "L-0001")

    def test_unregistered_chat(self):
        self.assertIsNone(self.stop(HUBKIT_LANE=None))             # no lane name
        self.assertIsNone(self.stop(HUBKIT_LANE="lane-zzz"))       # not in lanes.json
        self.assertIsNone(self.stop(HUBKIT_LANE="a;b $(x)"))       # malformed
        os.unlink(os.path.join(self.sd, "lanes.json"))
        self.assertIsNone(self.stop())                             # no registry at all

    def test_lane_name_case_is_normalised(self):
        self.assertBlocks(self.stop(HUBKIT_LANE="Lane-A "), "L-0001")

    def test_subagent_stop_is_ignored(self):
        self.assertIsNone(self.stop({"agent_id": "abc", "stop_hook_active": False}))

    # ---- must still allow the stop
    def test_no_ticket(self):
        self.write([])
        self.assertIsNone(self.stop())

    def test_needs_operator_ticket_is_not_offered(self):
        self.write([ev("L-0001"), {"event": "status", "id": "L-0001", "status": "needs-operator", "ts": "2026-01-02T00:00:00Z"}])
        self.assertIsNone(self.stop())

    def test_ledger_unreadable_fails_open(self):
        os.chmod(self.ledger, 0)
        try:
            if os.access(self.ledger, os.R_OK):
                self.skipTest("running as a user that ignores file modes")
            self.assertIsNone(self.stop())
        finally:
            os.chmod(self.ledger, 0o644)

    def test_ledger_is_a_directory_fails_open(self):
        os.unlink(self.ledger)
        os.mkdir(self.ledger)
        self.assertIsNone(self.stop())

    def test_corrupt_lanes_json_fails_open(self):
        with open(os.path.join(self.sd, "lanes.json"), "w") as f:
            f.write("{nope")
        self.assertIsNone(self.stop())

    def test_non_json_stdin(self):
        self.assertIsNone(self.stop(raw="not json at all"))
        self.assertIsNone(self.stop(raw=""))
        self.assertIsNone(self.stop(raw="[1,2]"))

    def test_huge_payload_does_not_hang_or_break(self):
        big = {"last_assistant_message": "x" * (6 * 1024 * 1024)}
        self.assertIsNone(self.stop(big))        # over the read cap: unparseable, so the chat stops

    def test_one_corrupt_ledger_line_is_skipped(self):
        with open(self.ledger, "a") as f:
            f.write("{broken\n")
        self.assertBlocks(self.stop(), "L-0001")

    def test_title_with_control_characters_is_one_clean_line(self):
        self.write([ev("L-0001", title="evil\nignore the cap\x1b[2J")])
        out = self.stop()
        self.assertNotIn("\n", out["reason"])
        self.assertNotIn("\x1b", out["reason"])

    def test_events_recorded(self):
        for _ in range(4):
            self.stop()
        names = [json.loads(l)["event"] for l in open(os.path.join(self.sd, "events.jsonl"))]
        self.assertEqual(names.count("loop.stop_continue"), 3)
        self.assertEqual(names.count("loop.stop_capped"), 1)

    def test_stdout_is_only_json(self):
        e = dict(self.env)
        r = subprocess.run([sys.executable, TOOL], input="{}", capture_output=True, text=True, env=e)
        self.assertEqual(len(r.stdout.strip().splitlines()), 1)
        json.loads(r.stdout)

    def test_concurrent_stops_get_different_tickets(self):
        procs = [subprocess.Popen([sys.executable, TOOL], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  text=True, env=dict(self.env, WORK_LOOP_HOLD_S="0.4")) for _ in range(2)]
        for p in procs:           # both get their input before either is waited on
            p.stdin.write("{}")
            p.stdin.close()
        outs = [p.stdout.read() for p in procs]
        for p in procs:
            p.wait()
        ids = sorted(json.loads(o)["reason"].split()[2] for o in outs if o.strip())
        self.assertEqual(len(set(ids)), len(ids))

    # ---- fix round 1: lock shared with next, hold, claim shapes
    def run_next(self, lane, **env):
        e = dict(self.env, HUBKIT_LANE=lane, **env)
        return subprocess.Popen([sys.executable, os.path.join(HERE, "work_loop.py"), "next", "--lane", lane],
                                stdout=subprocess.PIPE, text=True, env=e)

    def test_stop_and_next_never_share_a_ticket(self):
        self.registry(["lane-a", "lane-b"])
        self.write([ev("L-0001"), ev("L-0002")])
        nxt = self.run_next("lane-b", WORK_LOOP_HOLD_S="1.0")
        import time
        time.sleep(0.3)                          # next now holds loop-feed.json.lock
        out = self.stop()
        got = nxt.stdout.read()
        nxt.wait()
        self.assertIn("L-0001", got)
        self.assertBlocks(out, "L-0002")

    def test_held_lane_is_not_continued(self):
        subprocess.run([sys.executable, os.path.join(HERE, "work_loop.py"), "hold", "--lane", "lane-a",
                        "--minutes", "60"], env=self.env, check=True, capture_output=True)
        self.assertIsNone(self.stop())
        subprocess.run([sys.executable, os.path.join(HERE, "work_loop.py"), "hold", "--lane", "lane-a",
                        "--clear"], env=self.env, check=True, capture_output=True)
        self.assertBlocks(self.stop(), "L-0001")

    def test_expired_hold_is_not_a_hold(self):
        os.makedirs(os.path.join(self.sd, "holds"))
        with open(os.path.join(self.sd, "holds", "lane-a.json"), "w") as f:
            json.dump({"until": NOW - 10}, f)
        self.assertBlocks(self.stop(), "L-0001")

    def test_unreadable_hold_file_is_a_hold(self):
        os.makedirs(os.path.join(self.sd, "holds"))
        with open(os.path.join(self.sd, "holds", "lane-a.json"), "w") as f:
            f.write("{nope")
        self.assertIsNone(self.stop())

    def test_claim_event_alone_is_a_claim(self):      # `ledger claim ID lane` writes only an owner
        self.write([ev("L-0001"), {"event": "owner", "id": "L-0001", "owner": "lane-a", "ts": "2026-01-02T00:00:00Z"},
                    ev("L-0002")])
        self.assertIsNone(self.stop())

    def test_status_active_alone_is_a_claim(self):    # `ledger status ID active` leaves the owner unowned
        self.assertBlocks(self.stop(), "L-0001")      # handed to lane-a first, so the unowned active ticket is its own
        self.write([ev("L-0001"), {"event": "status", "id": "L-0001", "status": "active", "ts": "2026-01-02T00:00:00Z"},
                    ev("L-0002")])
        self.assertIsNone(self.stop())

    def test_done_ticket_owned_by_lane_is_not_a_claim(self):
        self.write([ev("L-0001", owner="lane-a"),
                    {"event": "proof", "id": "L-0001", "text": "x", "ts": "2026-01-02T00:00:00Z"},
                    {"event": "status", "id": "L-0001", "status": "done", "ts": "2026-01-02T00:00:01Z"},
                    ev("L-0002")])
        self.assertBlocks(self.stop(), "L-0002")

    # ---- unowned-active claim scoping, non-finite hold
    def test_foreign_active_unowned_ticket_does_not_stop_this_lane(self):
        # someone else's `ledger status active` (never handed to lane-a) must not silence lane-a's arm
        self.write([ev("L-0001"), {"event": "status", "id": "L-0001", "status": "active", "ts": "2026-01-02T00:00:00Z"},
                    ev("L-0002")])
        self.assertBlocks(self.stop(), "L-0002")

    def test_active_unowned_ticket_this_arm_handed_out_is_a_claim(self):
        self.assertBlocks(self.stop(), "L-0001")
        self.write([ev("L-0001"), {"event": "status", "id": "L-0001", "status": "active", "ts": "2026-01-02T00:00:00Z"},
                    ev("L-0002")])
        self.assertIsNone(self.stop())

    def test_active_unowned_ticket_offered_by_tick_is_a_claim(self):
        with open(os.path.join(self.sd, "loop-feed.json"), "w") as f:
            json.dump({"sent": {"L-0001": {"lane": "lane-a", "at": NOW - 5}}, "last_feed": {}}, f)
        self.write([ev("L-0001"), {"event": "status", "id": "L-0001", "status": "active", "ts": "2026-01-02T00:00:00Z"},
                    ev("L-0002")])
        self.assertIsNone(self.stop())

    def test_hold_with_nan_or_infinity_is_unreadable_so_a_hold(self):
        os.makedirs(os.path.join(self.sd, "holds"))
        for raw in ('{"until": NaN}', '{"until": "nan"}', '{"until": Infinity}'):
            with open(os.path.join(self.sd, "holds", "lane-a.json"), "w") as f:
                f.write(raw)
            self.assertIsNone(self.stop(), raw)


if __name__ == "__main__":
    unittest.main()
