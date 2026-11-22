#!/usr/bin/env python3
"""Tests for board_keeper.py. Run: python3 test_board_keeper.py"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import fcntl
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp()
os.environ.update(BK_BOARD=os.path.join(TMP, "board.json"), BK_ITEMS=os.path.join(TMP, "items.json"),
                  BK_STATE_DIR=os.path.join(TMP, "bk"), BK_LIVE_OWNERS=os.path.join(TMP, "live.txt"), BK_NOW="1900000000")
spec = importlib.util.spec_from_file_location("board_keeper", os.path.join(HERE, "board_keeper.py"))
B = importlib.util.module_from_spec(spec)
spec.loader.exec_module(B)


def put(board, items, live=None):
    shutil.rmtree(os.environ["BK_STATE_DIR"], ignore_errors=True)
    with open(os.environ["BK_BOARD"], "w") as f:
        json.dump({"cards": board}, f)
    with open(os.environ["BK_ITEMS"], "w") as f:
        json.dump(items, f)
    if live is not None:
        with open(os.environ["BK_LIVE_OWNERS"], "w") as f:
            f.write("\n".join(live))
        touch_live(float(os.environ["BK_NOW"]))
    elif os.path.exists(os.environ["BK_LIVE_OWNERS"]):
        os.remove(os.environ["BK_LIVE_OWNERS"])


def touch_live(t):
    os.utime(os.environ["BK_LIVE_OWNERS"], (t, t))


def stages():
    with open(os.environ["BK_BOARD"]) as fh:
        return {c["id"]: c["stage"] for c in json.load(fh)["cards"]}


def moves_log():
    p = os.path.join(os.environ["BK_STATE_DIR"], "moves.jsonl")
    if not os.path.exists(p):
        return []
    with open(p) as fh:
        return [json.loads(l) for l in fh]


def card(cid, stage, *linked):
    return {"id": cid, "stage": stage, "linked": list(linked)}


class T(unittest.TestCase):
    def test_all_closed_card_moves_to_done(self):
        put([card("c1", "building", "L-0001", "L-0002")], {"L-0001": {"status": "done"}, "L-0002": {"status": "dropped"}})
        r = B.run()
        self.assertEqual(stages()["c1"], "done")
        self.assertEqual(len(r["fixed"]), 1)

    def test_only_dropped_is_not_a_fix(self):
        put([card("c1", "todo", "L-0001")], {"L-0001": {"status": "dropped"}})
        B.run()
        self.assertEqual(stages()["c1"], "todo")

    def test_parked_item_blocks_the_fix(self):
        put([card("c1", "todo", "L-0001", "L-0002")], {"L-0001": {"status": "done"}, "L-0002": {"status": "backlog"}})
        B.run()
        self.assertEqual(stages()["c1"], "todo")

    def test_open_item_blocks_the_fix(self):
        put([card("c1", "todo", "L-0001", "L-0002")], {"L-0001": {"status": "done"}, "L-0002": {"status": "open"}})
        B.run()
        self.assertEqual(stages()["c1"], "todo")

    def test_unknown_item_flags_and_never_moves(self):
        put([card("c1", "todo", "L-0001", "L-0009")], {"L-0001": {"status": "done"}})
        r = B.run()
        self.assertEqual(stages()["c1"], "todo")
        self.assertEqual(r["flags"][0]["kind"], "unknown-item")

    def test_claim_moves_grooming_to_todo_then_building_for_live_owner(self):
        items = {"L-0001": {"status": "active", "owner": "w1"}}
        put([card("c1", "grooming", "L-0001")], items)
        B.run()
        self.assertEqual(stages()["c1"], "todo")
        put([card("c1", "todo", "L-0001")], items, live=["w1"])
        B.run()
        self.assertEqual(stages()["c1"], "building")

    def test_shared_seat_owner_is_not_a_worker(self):
        put([card("c1", "todo", "L-0001")], {"L-0001": {"status": "active", "owner": "hub"}}, live=["hub"])
        B.run()
        self.assertEqual(stages()["c1"], "todo")

    def test_wait_and_verify_signals(self):
        put([card("c1", "todo", "L-0001"), card("c2", "building", "L-0002")],
            {"L-0001": {"status": "open", "wait": True}, "L-0002": {"status": "active", "owner": "w1", "verify": True}}, live=["w1"])
        B.run()
        self.assertEqual(stages(), {"c1": "blocked", "c2": "verifying"})

    def test_backward_and_skipping_jumps_are_never_made(self):
        # building card whose item now only waits: blocked is a legal edge. A review card never moves back.
        put([card("c1", "review", "L-0001"), card("c2", "grooming", "L-0002")],
            {"L-0001": {"status": "active", "owner": "w1"}, "L-0002": {"status": "open", "wait": True}}, live=["w1"])
        r = B.run()
        self.assertEqual(stages(), {"c1": "review", "c2": "grooming"})
        self.assertEqual([f["kind"] for f in r["flags"]], ["illegal-move-blocked"])

    def test_dead_owner_flag_and_done_but_open(self):
        put([card("c1", "building", "L-0001"), card("c2", "done", "L-0002")],
            {"L-0001": {"status": "active", "owner": "ghost"}, "L-0002": {"status": "open"}}, live=["w1"])
        kinds = sorted(f["kind"] for f in B.run()["flags"])
        self.assertEqual(kinds, ["dead-owner", "done-but-open"])

    def test_kill_switch_touches_nothing(self):
        put([card("c1", "todo", "L-0001")], {"L-0001": {"status": "done"}})
        os.makedirs(os.environ["BK_STATE_DIR"])
        open(os.path.join(os.environ["BK_STATE_DIR"], "board-keeper-off"), "w").close()
        r = B.run()
        self.assertEqual(r["skipped"], "kill-switch")
        self.assertEqual(stages()["c1"], "todo")

    def test_truncated_or_garbage_items_file_is_flag_only(self):
        put([card("c1", "todo", "L-0001")], {"L-0001": {"status": "done"}})
        with open(os.environ["BK_ITEMS"], "w") as f:
            f.write('{"L-0001": {"status": "do')
        r = B.run()
        self.assertIn("items-unreadable", r["skipped"])
        self.assertEqual(stages()["c1"], "todo")

    def test_daily_cap_counts_from_the_log_and_flags_the_rest(self):
        B.CAP = 2
        try:
            put([card("c%d" % i, "todo", "L-%04d" % i) for i in range(1, 5)], {"L-%04d" % i: {"status": "done"} for i in range(1, 5)})
            r = B.run()
            self.assertEqual(len(r["fixed"]), 2)
            self.assertEqual(sorted(f["kind"] for f in r["flags"]), ["cap-reached", "cap-reached"])
            self.assertEqual(len(B.run()["fixed"]), 0)          # a second cycle the same day adds nothing
        finally:
            B.CAP = 25

    def test_move_cap_is_shared_by_all_moves(self):
        B.MOVE_CAP_PER_DAY = 1
        try:
            put([card("c1", "grooming", "L-0001"), card("c2", "grooming", "L-0002")],
                {"L-0001": {"status": "active", "owner": "w1"}, "L-0002": {"status": "active", "owner": "w1"}})
            r = B.run()
            self.assertEqual(len(r["moved"]), 1)
        finally:
            B.MOVE_CAP_PER_DAY = 100

    def test_unreadable_log_fails_closed(self):
        put([card("c1", "todo", "L-0001")], {"L-0001": {"status": "done"}})
        os.makedirs(os.environ["BK_STATE_DIR"])
        os.mkdir(os.path.join(os.environ["BK_STATE_DIR"], "board-keeper.log"))      # a folder where the log should be
        B.run()
        self.assertEqual(stages()["c1"], "todo")

    def test_undo_restores_and_holds(self):
        put([card("c1", "building", "L-0001")], {"L-0001": {"status": "done"}})
        B.run()
        self.assertEqual(stages()["c1"], "done")
        self.assertEqual(B.undo("c1"), 0)
        self.assertEqual(stages()["c1"], "building")
        B.run()                                                  # the fix must not re-apply: a person's undo sticks
        self.assertEqual(stages()["c1"], "building")

    def test_undo_with_nothing_logged_and_bad_ids(self):
        put([card("c1", "todo", "L-0001")], {"L-0001": {"status": "open"}})
        self.assertEqual(B.undo("c1"), 1)
        self.assertEqual(B.main(["x", "undo", "--bad;id"]), 2)

    def test_person_move_holds_card_for_24h_then_releases(self):
        put([card("c1", "todo", "L-0001")], {"L-0001": {"status": "active", "owner": "w1"}}, live=["w1"])
        B.log_move({"card": "c1", "from": "building", "to": "todo", "who": "person", "kind": "move"})
        B.run()
        self.assertEqual(stages()["c1"], "todo")                 # held: not moved up to building
        os.environ["BK_NOW"] = str(1900000000 + 25 * 3600)
        touch_live(1900000000 + 25 * 3600)                        # the live-owners file is re-written by its producer
        try:
            B.run()
            self.assertEqual(stages()["c1"], "building")
        finally:
            os.environ["BK_NOW"] = "1900000000"

    def test_hostile_card_ids_and_links_ignored(self):
        put([card("--delete", "todo", "L-0001"), card("Bad Id", "todo", "L-0001"), {"id": "ok", "stage": "todo", "linked": "{bad"},
             {"id": "ok2", "stage": "nowhere", "linked": ["L-0001"]}, card("ok3", "todo")], {"L-0001": {"status": "done"}})
        r = B.run()
        self.assertEqual(r["fixed"], [])

    def test_dry_run_changes_nothing(self):
        put([card("c1", "todo", "L-0001")], {"L-0001": {"status": "done"}})
        r = B.run(dry=True)
        self.assertEqual(len(r["fixed"]), 1)
        self.assertEqual(stages()["c1"], "todo")
        self.assertFalse(os.path.exists(os.path.join(os.environ["BK_STATE_DIR"], "moves.jsonl")))

    def test_move_log_has_who_why_from_to(self):
        put([card("c1", "todo", "L-0001")], {"L-0001": {"status": "done"}})
        B.run()
        e = moves_log()[0]
        self.assertEqual((e["who"], e["from"], e["to"]), ("board-keeper", "todo", "done"))
        self.assertTrue(e["why"])


class FailClosed(unittest.TestCase):
    """B1: the attempt line and the move line are written BEFORE a move; if they cannot be, nothing moves."""

    def board30(self):
        put([card("c%d" % i, "todo", "L-%04d" % i) for i in range(1, 31)], {"L-%04d" % i: {"status": "done"} for i in range(1, 31)})

    def setUp(self):
        self.real_state, self.real_log = B.STATE, B.log

    def tearDown(self):
        B.STATE, B.log = self.real_state, self.real_log
        shutil.rmtree(os.path.join(TMP, "blocker"), ignore_errors=True)
        if os.path.isfile(os.path.join(TMP, "blocker")):
            os.remove(os.path.join(TMP, "blocker"))

    def test_unwritable_state_dir_moves_zero_cards_and_cap_is_intact(self):
        self.board30()
        with open(os.path.join(TMP, "blocker"), "w") as f:          # a file where the state folder's parent should be
            f.write("x")
        B.STATE = B.Path(TMP) / "blocker" / "bk"
        r = B.run()
        self.assertEqual(r["error"], "log-unwritable")
        self.assertEqual(set(stages().values()), {"todo"})            # ZERO of 30 moved (the gate's probe P1 moved all 30)
        self.assertEqual(r["fixed"], [])
        self.assertEqual(B.main(["x", "run"]), 1)                     # and the exit code is non-zero
        self.assertEqual(set(stages().values()), {"todo"})
        B.STATE = self.real_state
        self.assertEqual(B.fixes_today(), 0)
        self.assertEqual(len(B.run()["fixed"]), 25)                   # once the log works again the cap of 25 is exactly intact
        self.assertEqual(sorted(stages().values()).count("todo"), 5)

    def test_unreadable_log_counts_as_the_cap(self):
        put([card("c1", "todo", "L-0001")], {"L-0001": {"status": "done"}})
        os.makedirs(os.environ["BK_STATE_DIR"])
        os.mkdir(os.path.join(os.environ["BK_STATE_DIR"], "board-keeper.log"))
        self.assertGreaterEqual(B.fixes_today(), B.CAP)
        self.assertGreaterEqual(B.moves_today(), B.MOVE_CAP_PER_DAY)

    def test_real_read_only_state_dir(self):
        if os.geteuid() == 0:
            self.skipTest("root ignores directory permissions")
        self.board30()
        os.makedirs(os.environ["BK_STATE_DIR"])
        os.chmod(os.environ["BK_STATE_DIR"], 0o500)
        try:
            r = B.run()
            self.assertEqual(r["error"], "log-unwritable")
            self.assertEqual(set(stages().values()), {"todo"})
        finally:
            os.chmod(os.environ["BK_STATE_DIR"], 0o700)

    def test_attempt_line_failing_mid_run_stops_the_fixes(self):
        self.board30()

        def flaky(entry):
            return False if entry.get("action") in ("fix", "move") else self.real_log(entry)
        B.log = flaky
        r = B.run()
        self.assertEqual(r["error"], "log-unwritable")
        self.assertEqual(set(stages().values()), {"todo"})

    def test_attempt_line_failing_stops_the_forward_moves(self):
        put([card("c1", "grooming", "L-0001")], {"L-0001": {"status": "active", "owner": "w1"}})

        def flaky(entry):
            return False if entry.get("action") == "move" else self.real_log(entry)
        B.log = flaky
        r = B.run()
        self.assertEqual(r["error"], "log-unwritable")
        self.assertEqual(stages()["c1"], "grooming")

    def test_move_line_unwritable_moves_nothing(self):
        put([card("c1", "todo", "L-0001"), card("c2", "grooming", "L-0002")],
            {"L-0001": {"status": "done"}, "L-0002": {"status": "active", "owner": "w1"}})
        os.makedirs(os.path.join(os.environ["BK_STATE_DIR"], "moves.jsonl"))      # a folder where moves.jsonl should be
        r = B.run()
        self.assertEqual(r["error"], "log-unwritable")
        self.assertEqual(stages(), {"c1": "todo", "c2": "grooming"})
        put([card("c2", "grooming", "L-0002")], {"L-0002": {"status": "active", "owner": "w1"}})
        os.makedirs(os.path.join(os.environ["BK_STATE_DIR"], "moves.jsonl"))
        self.assertEqual(B.run()["error"], "log-unwritable")
        self.assertEqual(stages(), {"c2": "grooming"})

    def test_undo_with_unwritable_move_log_changes_nothing(self):
        put([card("c1", "building", "L-0001")], {"L-0001": {"status": "done"}})
        B.run()
        os.remove(os.path.join(os.environ["BK_STATE_DIR"], "moves.jsonl"))
        with open(os.path.join(os.environ["BK_STATE_DIR"], "moves.jsonl"), "w") as f:
            f.write(json.dumps({"card": "c1", "kind": "fix", "from": "building", "to": "done", "who": "board-keeper", "at": 1}) + "\n")
        os.chmod(os.path.join(os.environ["BK_STATE_DIR"], "moves.jsonl"), 0o400)
        try:
            if os.geteuid() != 0:
                self.assertEqual(B.undo("c1"), 1)
                self.assertEqual(stages()["c1"], "done")
        finally:
            os.chmod(os.path.join(os.environ["BK_STATE_DIR"], "moves.jsonl"), 0o600)


class RestoredGuards(unittest.TestCase):
    def setUp(self):
        self.real_log = B.log

    def tearDown(self):
        B.log = self.real_log

    def test_second_run_while_locked_does_nothing(self):
        put([card("c1", "todo", "L-0001")], {"L-0001": {"status": "done"}})
        os.makedirs(os.environ["BK_STATE_DIR"])
        held = open(os.path.join(os.environ["BK_STATE_DIR"], "board-keeper.lock"), "w")
        fcntl.flock(held, fcntl.LOCK_EX)
        try:
            r = B.run()
            self.assertEqual(r["skipped"], "another run in progress")
            self.assertEqual(stages()["c1"], "todo")
        finally:
            held.close()
        self.assertEqual(len(B.run()["fixed"]), 1)                   # the lock is released at the end of a run

    def test_kill_switch_is_rechecked_before_every_fix(self):
        put([card("c1", "todo", "L-0001"), card("c2", "todo", "L-0002")], {"L-0001": {"status": "done"}, "L-0002": {"status": "done"}})
        kill = os.path.join(os.environ["BK_STATE_DIR"], "board-keeper-off")

        def log_then_kill(entry):
            ok = self.real_log(entry)
            if entry.get("action") == "fix":
                open(kill, "w").close()                              # the switch is thrown after the first card passed its check
            return ok
        B.log = log_then_kill
        r = B.run()
        self.assertEqual(sorted(stages().values()), ["done", "todo"])
        self.assertEqual([f["kind"] for f in r["flags"]], ["kill-switch"])

    def test_kill_switch_is_rechecked_before_every_forward_move(self):
        put([card("c1", "grooming", "L-0001"), card("c2", "grooming", "L-0002")],
            {"L-0001": {"status": "active", "owner": "w1"}, "L-0002": {"status": "active", "owner": "w1"}})
        kill = os.path.join(os.environ["BK_STATE_DIR"], "board-keeper-off")

        def log_then_kill(entry):
            ok = self.real_log(entry)
            if entry.get("action") == "move":
                open(kill, "w").close()
            return ok
        B.log = log_then_kill
        B.run()
        self.assertEqual(sorted(stages().values()), ["grooming", "todo"])

    def test_live_owners_older_than_24h_is_unknown(self):
        items = {"L-0001": {"status": "active", "owner": "w1"}}
        put([card("c1", "todo", "L-0001")], items, live=["w1"])
        touch_live(1900000000 - 10 * 86400)                          # probe P3: a ten day old file
        B.run()
        self.assertEqual(stages()["c1"], "todo")
        touch_live(1900000000 - 3600)                                # an hour old is still trusted
        B.run()
        self.assertEqual(stages()["c1"], "building")

    def test_empty_live_owners_is_unknown_not_everyone_dead(self):
        put([card("c1", "building", "L-0001")], {"L-0001": {"status": "active", "owner": "w1"}}, live=[])
        self.assertEqual(os.path.getsize(os.environ["BK_LIVE_OWNERS"]), 0)
        r = B.run()
        self.assertEqual(r["flags"], [])                             # probe P3b flagged dead-owner here
        self.assertIsNone(B.live_owners())

    def test_concurrent_edit_of_another_card_survives_the_write(self):
        put([card("c1", "todo", "L-0001"), card("c2", "todo", "L-0002")], {"L-0001": {"status": "done"}, "L-0002": {"status": "open"}})

        def person_edits_c2(entry):
            ok = self.real_log(entry)
            if entry.get("action") == "fix":
                with open(os.environ["BK_BOARD"]) as fh:
                    d = json.load(fh)
                d["cards"][1]["stage"] = "review"
                with open(os.environ["BK_BOARD"], "w") as fh:
                    json.dump(d, fh)
            return ok
        B.log = person_edits_c2
        B.run()
        self.assertEqual(stages(), {"c1": "done", "c2": "review"})     # probe P5 lost the person's edit

    def test_card_moved_by_a_person_since_the_read_is_not_overwritten(self):
        put([card("c1", "todo", "L-0001")], {"L-0001": {"status": "done"}})

        def person_moves_c1(entry):
            ok = self.real_log(entry)
            if entry.get("action") == "fix":
                with open(os.environ["BK_BOARD"], "w") as fh:
                    json.dump({"cards": [card("c1", "review", "L-0001")]}, fh)
            return ok
        B.log = person_moves_c1
        r = B.run()
        self.assertEqual(stages()["c1"], "review")
        self.assertEqual([f["kind"] for f in r["flags"]], ["card-changed-since-read"])
        self.assertEqual(r["fixed"], [])

    def test_undo_does_not_force_a_card_a_person_moved_since(self):
        put([card("c1", "building", "L-0001")], {"L-0001": {"status": "done"}})
        B.run()
        with open(os.environ["BK_BOARD"], "w") as fh:
            json.dump({"cards": [card("c1", "review", "L-0001")]}, fh)
        self.assertEqual(B.undo("c1"), 1)                              # probe P7 forced it back to building
        self.assertEqual(stages()["c1"], "review")

    def test_person_move_does_not_block_a_proof_based_close_but_an_undo_does(self):
        put([card("c1", "todo", "L-0001")], {"L-0001": {"status": "done"}})
        B.log_move({"card": "c1", "from": "building", "to": "todo", "who": "person", "kind": "move"})
        self.assertEqual(len(B.run()["fixed"]), 1)
        put([card("c1", "todo", "L-0001")], {"L-0001": {"status": "done"}})
        B.log_move({"card": "c1", "from": "done", "to": "todo", "who": "person", "kind": "undo"})
        r = B.run()
        self.assertEqual(stages()["c1"], "todo")
        self.assertEqual([f["kind"] for f in r["flags"]], ["undone-by-hand"])


if __name__ == "__main__":
    unittest.main()
