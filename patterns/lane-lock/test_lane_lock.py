#!/usr/bin/env python3
"""Tests for lane-lock.py. Every case runs the command line as a user would, in a scratch HOME."""
import contextlib
import importlib.util
import io
import json
import os
import runpy
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
LL = os.environ.get("LANE_LOCK", os.path.join(HERE, "lane-lock.py"))


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.home = os.path.join(self.tmp, "home")
        self.hk = os.path.join(self.home, ".claude", "hubkit")
        os.makedirs(self.home)
        self.env = dict(os.environ, HOME=self.home, HUBKIT_HOME=self.hk, PYTHONDONTWRITEBYTECODE="1",
                        LANE_BREAKER_THRESHOLD="2")
        for k in ("LANE_LOCK_ROOT", "LANE_LOCK_ALARMS_LOG", "LANE_LOCK_WAIT_S"):
            self.env.pop(k, None)
        self.procs = []
        self.me = os.getpid()      # a live pid that is not the CLI's own

    def tearDown(self):
        for p in self.procs:
            p.kill()
            p.wait()
        for root, dirs, files in os.walk(self.tmp, topdown=False):
            for f in files:
                os.remove(os.path.join(root, f))
            for d in dirs:
                os.rmdir(os.path.join(root, d))
        os.rmdir(self.tmp)

    def ll(self, *args, env=None):
        r = subprocess.run([sys.executable, LL, *args], capture_output=True, text=True,
                           env=dict(self.env, **(env or {})))
        return r.returncode, r.stdout.strip(), r.stderr.strip()

    def sleeper(self):
        p = subprocess.Popen(["sleep", "60"])
        self.procs.append(p)
        return p.pid

    def lane_dir(self, lane):
        return os.path.join(self.hk, "state", "lanes", lane)

    def alarms(self):
        p = os.path.join(self.hk, "state", "alarms.log")
        return open(p).read() if os.path.exists(p) else ""

    def claim(self, lane, owner, pid=None, **env):
        return self.ll("claim", lane, "--owner", owner, "--pid", str(pid or self.me), env=env)

    def edit_owner(self, lane, fn):
        p = os.path.join(self.lane_dir(lane), "owner.json")
        d = json.load(open(p))
        fn(d)
        json.dump(d, open(p, "w"))


class Ownership(Case):
    def test_claim_then_owner_releases(self):
        rc, out, _ = self.claim("repo", "a")
        self.assertEqual(rc, 0)
        self.assertTrue(out.startswith("CLAIMED"), out)
        rc, out, _ = self.ll("release", "repo", "--owner", "a")
        self.assertEqual((rc, out.split()[0]), (0, "RELEASED"))
        self.assertIn("owner=(none, unheld)", self.ll("status", "repo")[1])

    def test_a_live_other_owner_is_refused_exit_2(self):
        self.claim("repo", "a")
        rc, out, _ = self.claim("repo", "b", pid=self.sleeper())
        self.assertEqual(rc, 2)
        self.assertTrue(out.startswith("REFUSED"), out)
        self.assertIn("held by 'a'", out)

    def test_the_same_owner_can_claim_again(self):
        self.claim("repo", "a")
        self.assertEqual(self.claim("repo", "a")[0], 0)

    def test_a_peer_cannot_release_someone_elses_claim(self):
        self.claim("repo", "a")
        rc, out, _ = self.ll("release", "repo", "--owner", "b")
        self.assertEqual(rc, 2)
        self.assertIn("REFUSED release", out)
        self.assertIn("owner=a", self.ll("status", "repo")[1])

    def test_releasing_an_unheld_lane_is_a_no_op(self):
        rc, out, _ = self.ll("release", "repo", "--owner", "a")
        self.assertEqual(rc, 0)
        self.assertIn("already unheld", out)

    def test_a_dead_holder_self_heals(self):
        p = self.sleeper()
        self.assertEqual(self.claim("repo", "dead", pid=p)[0], 0)
        self.procs[0].kill()
        self.procs[0].wait()
        rc, out, _ = self.claim("repo", "c")
        self.assertEqual(rc, 0)
        self.assertIn("self-healed", out)
        self.assertIn("owner=c", self.ll("status", "repo")[1])

    def test_claim_without_pid_is_refused_and_leaves_the_lane_free(self):
        rc, out, err = self.ll("claim", "repo", "--owner", "x")
        self.assertEqual(rc, 1)
        self.assertIn("--pid is required", err)
        self.assertIn("owner=(none, unheld)", self.ll("status", "repo")[1])

    def test_zero_and_negative_pids_are_rejected(self):
        for pid in ("0", "-5"):
            rc, _, err = self.ll("claim", "repo", "--owner", "x", "--pid", pid)
            self.assertEqual(rc, 1, pid)
            self.assertIn("positive integer", err)
        self.assertIn("owner=(none, unheld)", self.ll("status", "repo")[1])

    def test_usage_errors_exit_1_never_2(self):
        # exit 2 means "held by another live owner"; a typo must not look like that
        self.assertEqual(self.ll("claim", "repo", "--pid", "5")[0], 1)     # no --owner
        self.assertEqual(self.ll("frobnicate")[0], 1)
        self.assertEqual(self.ll("record-fail", "repo", "--ticket", "L-0001", "--gate", "bogus")[0], 1)

    def test_bad_lane_names_are_rejected_before_anything_is_written(self):
        for bad in ("../escape", "a/b", ".hidden", "", "x" * 65, "abc\n", "abc\r", "a\tb", "abc\x1b[0m"):
            rc, _, err = self.ll("claim", bad, "--owner", "a", "--pid", str(self.me))
            self.assertEqual(rc, 1, bad)
            self.assertIn("bad lane name", err)
        self.assertFalse(os.path.exists(os.path.join(self.hk, "state", "lanes")))
        self.assertFalse(os.path.exists(os.path.join(self.home, ".claude", "escape")))

    def test_status_and_list_never_create_a_lane(self):
        self.assertIn("owner=(none, unheld)", self.ll("status", "ghost")[1])
        self.assertFalse(os.path.exists(self.lane_dir("ghost")))
        rc, out, _ = self.ll("list")
        self.assertEqual((rc, out), (0, "(no lanes yet)"))
        self.claim("one", "a")
        self.claim("two", "b")
        out = self.ll("list")[1]
        self.assertIn("lane=one owner=a", out)
        self.assertIn("lane=two owner=b", out)


class Identity(Case):
    def test_alive_pid_with_a_mismatched_start_time_is_a_different_process(self):
        self.claim("repo", "orig")
        # current tagged format, wrong value: a genuine same-format mismatch
        self.edit_owner("repo", lambda d: d.update(start_key={"fmt": "lstart-C-UTC-v1", "value": "Mon Jan  1 00:00:00 1990"}))
        rc, out, _ = self.claim("repo", "new")
        self.assertEqual(rc, 0)
        self.assertIn("self-healed", out)
        self.assertIn("reused", self.status_after_mismatch())

    def status_after_mismatch(self):
        self.claim("other", "orig")
        self.edit_owner("other", lambda d: d.update(start_key={"fmt": "lstart-C-UTC-v1", "value": "Mon Jan  1 00:00:00 1990"}))
        return self.ll("status", "other")[1]

    def test_a_matching_start_time_keeps_the_lane_held(self):
        p = self.sleeper()
        self.claim("repo", "real", pid=p)
        rec = json.load(open(os.path.join(self.lane_dir("repo"), "owner.json")))
        self.assertEqual(rec["start_key"]["fmt"], "lstart-C-UTC-v1")
        self.assertEqual(self.claim("repo", "intruder")[0], 2)

    def test_a_different_timezone_does_not_look_like_a_different_process(self):
        p = self.sleeper()
        rc, out, _ = self.claim("repo", "real", pid=p, TZ="America/Chicago")
        self.assertEqual(rc, 0, out)
        rc, out, _ = self.claim("repo", "intruder", TZ="UTC")
        self.assertEqual(rc, 2, out)
        self.assertIn("REFUSED", out)
        self.assertEqual(self.procs[0].poll(), None)       # the holder was never touched
        rc, out, _ = self.claim("repo", "intruder", TZ="Asia/Tokyo", LC_ALL="de_DE.UTF-8")
        self.assertEqual(rc, 2, out)

    def test_a_legacy_format_start_key_is_unknown_not_dead(self):
        p = self.sleeper()
        self.claim("repo", "legacy", pid=p)
        self.edit_owner("repo", lambda d: d.update(start_key="Sun Sep 27 23:12:15 2026"))   # bare string
        rc, out, _ = self.claim("repo", "intruder")
        self.assertEqual(rc, 2, out)
        self.assertIn("unrecognized or legacy format", self.alarms())

    def test_an_unreadable_ps_means_the_holder_is_presumed_alive(self):
        # ps cannot run (not on PATH here, or exits non-zero): identity is unknown, so a live pid
        # is never stolen from. The lane must stay refused, not be healed.
        p = self.sleeper()
        self.claim("repo", "real", pid=p)
        bindir = os.path.join(self.tmp, "fakebin")
        os.makedirs(bindir)
        fake = os.path.join(bindir, "ps")
        with open(fake, "w") as f:
            f.write("#!/bin/sh\nexit 1\n")
        os.chmod(fake, 0o755)
        rc, out, _ = self.claim("repo", "intruder", PATH=bindir + os.pathsep + os.environ["PATH"])
        self.assertEqual(rc, 2, out)
        self.assertIn("REFUSED", out)
        rc, out, _ = self.claim("repo", "intruder", PATH=bindir)       # ps missing entirely
        self.assertEqual(rc, 2, out)
        self.assertEqual(self.procs[0].poll(), None)

    def test_a_record_with_no_start_key_falls_back_to_liveness(self):
        p = self.sleeper()
        self.claim("repo", "old", pid=p)
        self.edit_owner("repo", lambda d: d.pop("start_key"))
        self.assertEqual(self.claim("repo", "intruder")[0], 2)
        self.procs[0].kill()
        self.procs[0].wait()
        self.assertEqual(self.claim("repo", "intruder")[0], 0)


class Corruption(Case):
    def put(self, lane, name, data):
        d = self.lane_dir(lane)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, name), "wb") as f:
            f.write(data)

    def test_corrupt_breaker_fails_closed_and_alarms(self):
        self.put("repo", "breaker.json", b'{ "fail_streak": 2, "tripped": tru')
        rc, out, err = self.claim("repo", "sneak")
        self.assertEqual(rc, 3)
        self.assertIn("TRIPPED", out)
        self.assertIn("WARNING", err)
        self.assertIn("breaker.json is CORRUPT", self.alarms())

    def test_unreadable_or_non_object_breaker_also_fails_closed(self):
        for i, junk in enumerate((b"\xff\xfe\x00 not text", b"[]", b"5", b"")):
            lane = "b%d" % i
            self.put(lane, "breaker.json", junk)
            self.assertEqual(self.claim(lane, "sneak")[0], 3, junk)

    def test_corrupt_owner_fails_closed_for_claim_and_release(self):
        self.put("repo", "owner.json", b'{ "owner": "real", "pid": 1, "brok')
        rc, out, _ = self.claim("repo", "intruder")
        self.assertEqual(rc, 2)
        self.assertIn("force-release", out)
        rc, out, _ = self.ll("release", "repo", "--owner", "intruder")
        self.assertEqual(rc, 2)
        self.assertIn("CORRUPT", out)
        self.assertIn("owner.json is CORRUPT", self.alarms())

    def test_unreadable_or_non_object_owner_also_fails_closed(self):
        for i, junk in enumerate((b"\xff\xfe\x00", b"[]", b'"x"')):
            lane = "o%d" % i
            self.put(lane, "owner.json", junk)
            self.assertEqual(self.claim(lane, "sneak")[0], 2, junk)
            self.assertIn("UNKNOWN", self.ll("status", lane)[1])

    def test_force_release_recovers_a_corrupt_owner_and_logs_who_and_why(self):
        self.put("repo", "owner.json", b"{ broken")
        rc, out, _ = self.ll("force-release", "repo", "--by", "ops", "--reason", "clearing corrupt record")
        self.assertEqual(rc, 0)
        self.assertIn("FORCE-RELEASED", out)
        self.assertIn("FORCE-RELEASED by ops", self.alarms())
        self.assertIn("clearing corrupt record", self.alarms())
        self.assertEqual(self.claim("repo", "fresh")[0], 0)

    def test_force_release_of_a_stuck_live_claim_works_and_unheld_is_a_no_op(self):
        self.claim("repo", "a")
        self.assertEqual(self.claim("repo", "b", pid=self.sleeper())[0], 2)
        self.assertEqual(self.ll("force-release", "repo", "--by", "ops", "--reason", "owner is gone")[0], 0)
        self.assertEqual(self.claim("repo", "b", pid=self.sleeper())[0], 0)
        self.ll("release", "repo", "--owner", "b")
        rc, out, _ = self.ll("force-release", "repo", "--by", "ops", "--reason", "again")
        self.assertEqual(rc, 0)
        self.assertIn("no-op", out)
        self.assertEqual(self.alarms().count("FORCE-RELEASED"), 1)      # a no-op is not logged as a release

    def test_a_wrong_type_lock_path_fails_fast_instead_of_hanging(self):
        os.makedirs(self.lane_dir("repo"))
        open(os.path.join(self.lane_dir("repo"), ".critsec.lock"), "w").close()   # a file, not a folder
        t0 = time.time()
        rc, _, err = self.claim("repo", "x", LANE_LOCK_WAIT_S="1")
        self.assertLess(time.time() - t0, 8)
        self.assertEqual(rc, 1)
        self.assertIn("not a directory", err)

    def test_a_stale_lock_folder_is_stolen_once_then_the_claim_works(self):
        os.makedirs(os.path.join(self.lane_dir("repo"), ".critsec.lock"))
        t0 = time.time()
        rc, out, _ = self.claim("repo", "x", LANE_LOCK_WAIT_S="0.3")
        self.assertEqual(rc, 0, out)
        self.assertLess(time.time() - t0, 8)


class Breaker(Case):
    def record_fail(self, lane="repo", detail="x"):
        return self.ll("record-fail", lane, "--ticket", "L-0001", "--gate", "bug-gate", "--detail", detail)

    def test_two_fails_trip_it_and_claims_are_refused(self):
        rc, out, _ = self.record_fail()
        self.assertEqual(rc, 0)
        self.assertTrue(out.startswith("OK"), out)
        rc, out, _ = self.record_fail(detail="second")
        self.assertEqual(rc, 3)
        self.assertTrue(out.startswith("TRIPPED"), out)
        self.assertIn("BREAKER TRIPPED", self.alarms())
        rc, out, _ = self.claim("repo", "d")
        self.assertEqual(rc, 3)
        self.assertIn("TRIPPED", out)
        self.assertIn("REFUSED", out)

    def test_the_threshold_comes_from_the_environment(self):
        self.assertEqual(self.ll("record-fail", "repo", "--ticket", "L-0001", "--gate", "playtest",
                                 env={"LANE_BREAKER_THRESHOLD": "3"})[0], 0)
        self.assertEqual(self.ll("record-fail", "repo", "--ticket", "L-0001", "--gate", "playtest",
                                 env={"LANE_BREAKER_THRESHOLD": "3"})[0], 0)
        self.assertEqual(self.ll("record-fail", "repo", "--ticket", "L-0001", "--gate", "playtest",
                                 env={"LANE_BREAKER_THRESHOLD": "3"})[0], 3)

    def test_a_pass_between_fails_restarts_the_count(self):
        self.record_fail()
        self.ll("record-pass", "repo", "--ticket", "L-0001", "--gate", "bug-gate")
        rc, _, _ = self.record_fail()
        self.assertEqual(rc, 0)
        self.assertEqual(self.claim("repo", "a")[0], 0)

    def test_a_pass_after_a_trip_does_not_reopen_the_lane(self):
        self.record_fail()
        self.record_fail()
        rc, out, _ = self.ll("record-pass", "repo", "--ticket", "L-0001", "--gate", "bug-gate")
        self.assertEqual(rc, 0)
        self.assertIn("still TRIPPED", out)
        self.assertEqual(self.claim("repo", "e")[0], 3)

    def test_reset_reopens_the_lane_and_is_logged(self):
        self.record_fail()
        self.record_fail()
        os.makedirs(os.path.join(self.hk, "reports", "judge"))
        with open(os.path.join(self.hk, "reports", "judge", "repo-ruling.md"), "w") as f:
            f.write("# Judge\n\nVERDICT: CONTINUE\n\nLane repo\n")
        rc, out, _ = self.ll("reset", "repo", "--by", "judge", "--reason", "read the diff, see reports/judge/repo-ruling.md")
        self.assertEqual(rc, 0, out)
        self.assertIn("RESET by judge", self.alarms())
        self.assertEqual(self.claim("repo", "f")[0], 0)
        self.assertIn("breaker=closed fail_streak=0", self.ll("status", "repo")[1])

    def test_reset_on_a_healthy_lane_says_so_and_logs_nothing(self):
        rc, out, _ = self.ll("reset", "repo", "--by", "ops", "--reason", "just checking")
        self.assertEqual(rc, 0)
        self.assertIn("was not tripped", out)
        self.assertNotIn("RESET", self.alarms())

    def test_history_is_bounded(self):
        for _ in range(30):
            self.ll("record-pass", "repo", "--ticket", "L-0001", "--gate", "other")
        d = json.load(open(os.path.join(self.lane_dir("repo"), "breaker.json")))
        self.assertEqual(len(d["history"]), 20)

    def test_lanes_do_not_share_a_breaker(self):
        self.record_fail("one")
        self.record_fail("one")
        self.assertEqual(self.claim("one", "a")[0], 3)
        self.assertEqual(self.claim("two", "a")[0], 0)

    def test_status_shows_a_trip(self):
        self.record_fail()
        self.record_fail()
        self.assertIn("breaker=TRIPPED", self.ll("status", "repo")[1])


class ResetGuard(Case):
    """A reset of a tripped lane needs --by judge|hub AND a cited judge ruling. Fake HUBKIT_HOME only."""

    def setUp(self):
        super().setUp()
        self.rj = os.path.join(self.hk, "reports", "judge")
        os.makedirs(self.rj)
        self.n = 0

    def rec_fail(self, lane, ticket="PRJ-1", times=2):
        for _ in range(times):
            self.ll("record-fail", lane, "--ticket", ticket, "--gate", "bug-gate", "--detail", "x")

    def new(self, fails=2):
        self.n += 1
        self.lane = "g%d" % self.n
        os.makedirs(self.lane_dir(self.lane), exist_ok=True)
        self.rec_fail(self.lane, times=fails)
        return self.lane

    def ruling(self, name, body):
        p = os.path.join(self.rj, name)
        with open(p, "w") as f:
            f.write(body)
        return p

    def good(self):
        self.ruling("ok.md", "# Judge\n\nVERDICT: CONTINUE\n\nLane %s, ticket PRJ-1\n" % self.lane)

    def reset(self, by, reason, lane=None):
        return self.ll("reset", lane or self.lane, "--by", by, "--reason", reason)

    def breaker(self, lane=None):
        return json.load(open(os.path.join(self.lane_dir(lane or self.lane), "breaker.json")))

    def test_long_underscore_lane_cell_is_refused_fast_and_italic_row_still_resets(self):
        import time
        self.new()
        self.ruling("u.md", "# Judge\n\nVERDICT: CONTINUE\n\n| Lane |\n|--|\n| %s |\n" % ("_" * 30000))
        t0 = time.time()
        self.assertEqual(self.reset("judge", "reports/judge/u.md")[0], 2)
        self.assertLess(time.time() - t0, 1.0)
        self.assertTrue(self.breaker()["tripped"])
        self.ruling("i.md", "# Judge\n\nVERDICT: CONTINUE\n\n| Lane |\n|--|\n| _%s_ |\n" % self.lane)
        self.assertEqual(self.reset("judge", "reports/judge/i.md")[0], 0)
        self.assertFalse(self.breaker()["tripped"])

    # --- must BLOCK
    def test_lead_reset_of_a_tripped_lane_is_blocked_even_citing_a_ruling(self):
        self.new()
        self.good()
        for by in ("lead", "Judge", " hub ", "ops"):
            rc, _, err = self.reset(by, "reports/judge/ok.md")
            self.assertEqual(rc, 2, by)
            self.assertIn("REFUSED", err)
        self.assertTrue(self.breaker()["tripped"])
        self.assertIn("reset REFUSED", self.alarms())

    def test_judge_without_a_ruling_is_blocked(self):
        self.new()
        self.good()
        for reason in ("judge said ok", "", "reports/judge/missing.md", "ok.md"):
            self.assertEqual(self.reset("judge", reason)[0], 2, reason)
        self.assertTrue(self.breaker()["tripped"])

    def test_a_ruling_must_be_real_named_and_newer_than_the_trip(self):
        self.new()
        self.ruling("no-verdict.md", "# Notes\n\n%s PRJ-1\n```\nVERDICT: CUT\n```\n" % self.lane)
        self.ruling("no-names.md", "# Judge\n\nVERDICT: CONTINUE\n")
        self.ruling("substr.md", "# Judge\n\nVERDICT: CONTINUE\n\nPRJ-10 only\n")
        self.good()
        old = self.ruling("old.md", "# Judge\n\nVERDICT: CONTINUE\n\nLane %s\n" % self.lane)
        os.utime(old, (946684800, 946684800))
        for name in ("no-verdict", "no-names", "substr", "old"):
            self.assertEqual(self.reset("judge", "reports/judge/%s.md" % name)[0], 2, name)
        rc, _, err = self.reset("judge", "see reports/judge/ok.md.")
        self.assertEqual(rc, 2)
        self.assertIn("trailing punctuation", err)
        self.assertTrue(self.breaker()["tripped"])

    def test_a_ruling_binds_to_the_exact_lane_token_not_a_substring(self):
        """A ruling for lane `deploy-main` must not reset lane `main`; exact names, CRLF and BOM still work."""
        self.n += 1
        self.lane = "main"
        os.makedirs(self.lane_dir("main"), exist_ok=True)
        self.rec_fail("main", ticket="PRJ-9")
        bad = {
            "other-lane": "Lane deploy-main\n",
            "dotted": "Lane deploy.main and a/main and main-2 and main.v2\n",
            "prose": "the main branch is fine\n",
            "ticket-sub": "Lane deploy-main, ticket PRJ-90\n",
        }
        for nm, body in bad.items():
            self.ruling(nm + ".md", "# Judge\n\nVERDICT: CONTINUE\n\n" + body)
            self.assertEqual(self.reset("judge", "reports/judge/%s.md" % nm, "main")[0], 2, nm)
        self.assertTrue(self.breaker("main")["tripped"])
        for nm, body in {"plain": "# Judge\n\nVERDICT: CONTINUE\n\nLane main.\n",
                         "crlf": "# Judge\r\n\r\nVERDICT: CONTINUE\r\n\r\nLane: `main`\r\n"}.items():
            self.ruling(nm + ".md", body)
        with open(os.path.join(self.rj, "bom.md"), "wb") as f:
            f.write("\ufeff# Judge\r\n\r\nVerdict: RESET\r\n\r\nlane main, ticket PRJ-9\r\n".encode("utf-8"))
        for nm in ("plain", "crlf", "bom"):
            self.rec_fail("main", ticket="PRJ-9", times=2)
            os.utime(os.path.join(self.rj, nm + ".md"), (time.time() + 5, time.time() + 5))  # a ruling must be newer than the trip
            self.assertEqual(self.reset("judge", "reports/judge/%s.md" % nm, "main")[0], 0, nm)

    def test_traversal_and_absolute_citations_are_blocked(self):
        self.new()
        self.good()
        outside = os.path.join(self.hk, "reports", "outside.md")
        with open(outside, "w") as f:
            f.write("# Judge\n\nVERDICT: SHIP PRJ-1 %s\n" % self.lane)
        for reason in ("reports/judge/../outside.md", "reports/judge/../../../../etc/passwd", outside,
                       "reports/judge/sub/ok.md", "xreports/judge/ok.md"):
            self.assertEqual(self.reset("hub", reason)[0], 2, reason)

    def test_symlinked_ruling_file_and_symlinked_folder_are_blocked(self):
        self.new()
        outside = os.path.join(self.tmp, "outside")
        os.makedirs(outside)
        with open(os.path.join(outside, "judge-r1.md"), "w") as f:
            f.write("# Judge\n\nVERDICT: CONTINUE\n\n%s PRJ-1\n" % self.lane)
        os.symlink(os.path.join(outside, "judge-r1.md"), os.path.join(self.rj, "link.md"))
        self.assertEqual(self.reset("judge", "reports/judge/link.md")[0], 2)
        os.symlink(outside, os.path.join(self.hk, "reports", "PRJ-1"))      # folder pointing outside reports/
        try:
            self.assertEqual(self.reset("judge", "reports/PRJ-1/judge-r1.md")[0], 2)
            self.assertTrue(self.breaker()["tripped"])
        finally:
            os.unlink(os.path.join(self.hk, "reports", "PRJ-1"))

    def test_a_pass_while_tripped_launders_nothing(self):
        self.new()
        self.good()
        rc, out, _ = self.ll("record-pass", self.lane, "--ticket", "PRJ-1", "--gate", "bug-gate")
        self.assertEqual(rc, 0)
        self.assertIn("still TRIPPED", out)
        self.ll("record-pass", self.lane, "--ticket", "PRJ-1", "--gate", "bug-gate")
        self.assertEqual(self.breaker()["fail_streak"], 2)
        self.assertEqual(self.reset("lead", "reports/judge/ok.md")[0], 2)
        self.assertEqual(self.reset("lead", "x")[0], 2)
        self.assertTrue(self.breaker()["tripped"])

    def test_tripped_floor_survives_a_rolled_off_history(self):
        # a tripped lane whose FAIL rows rolled off the 20-row window and whose fail_streak is 0 still needs a judge
        self.new()
        self.good()
        p = os.path.join(self.lane_dir(self.lane), "breaker.json")
        b = self.breaker()
        b["fail_streak"], b["history"] = 0, []
        json.dump(b, open(p, "w"))
        self.assertEqual(self.reset("lead", "x")[0], 2)
        self.assertEqual(self.reset("judge", "x")[0], 2)
        self.assertEqual(self.reset("judge", "per reports/judge/ok.md")[0], 0)

    def test_corrupt_and_odd_typed_breaker_need_a_judge(self):
        self.new(fails=0)
        p = os.path.join(self.lane_dir(self.lane), "breaker.json")
        open(p, "w").write('{ "fail_streak": 2, "tripped": tru')
        self.assertEqual(self.reset("lead", "x")[0], 2)
        self.good()
        os.utime(self.rj + "/ok.md", (time.time() + 5, time.time() + 5))   # newer than the corrupt file's own mtime
        self.assertEqual(self.reset("judge", "reports/judge/ok.md")[0], 0)
        self.new(fails=0)
        p = os.path.join(self.lane_dir(self.lane), "breaker.json")
        json.dump({"fail_streak": 0, "tripped": "false", "history": []}, open(p, "w"))
        self.assertEqual(self.reset("lead", "x")[0], 2)

    def test_corrupt_breaker_streak_is_floored_after_a_pass(self):
        self.new(fails=0)
        p = os.path.join(self.lane_dir(self.lane), "breaker.json")
        open(p, "w").write("[]")
        self.ll("record-pass", self.lane, "--ticket", "PRJ-1", "--gate", "bug-gate")
        self.assertGreaterEqual(self.breaker()["fail_streak"], 2)
        self.assertEqual(self.reset("lead", "x")[0], 2)

    def test_a_plain_reset_row_does_not_anchor_the_count(self):
        # trip, judge reset (anchors), then a single FAIL: lead may reset (1 FAIL since the ruled reset)
        self.new()
        self.good()
        self.assertEqual(self.reset("judge", "reports/judge/ok.md")[0], 0)
        self.rec_fail(self.lane, times=1)
        self.assertEqual(self.reset("lead", "x")[0], 0)

    def test_history_rows_that_must_not_anchor_the_count(self):
        # hand-edited / legacy breaker.json: untripped, fail_streak 0, but two FAIL rows since the last real PASS
        now = "2030-01-01T00:00:00+00:00"
        fail = {"ts": now, "ticket": "PRJ-1", "gate": "bug-gate", "result": "FAIL", "detail": ""}
        rows = {
            "tagged-pass": [fail, fail, {"ts": now, "ticket": "PRJ-1", "gate": "bug-gate", "result": "PASS", "detail": "",
                                         "while_tripped": True}],
            "plain-reset": [fail, fail, {"ts": now, "ticket": "", "gate": "reset", "result": "RESET by ops", "detail": ""}],
        }
        for name, hist in rows.items():
            self.new(fails=0)
            json.dump({"fail_streak": 0, "tripped": False, "tripped_at": None, "history": hist},
                      open(os.path.join(self.lane_dir(self.lane), "breaker.json"), "w"))
            self.assertEqual(self.reset("lead", "x")[0], 2, name)
        # control: a real PASS row after the FAILs anchors, so a lead reset is allowed
        self.new(fails=0)
        ok = {"ts": now, "ticket": "PRJ-1", "gate": "bug-gate", "result": "PASS", "detail": ""}
        json.dump({"fail_streak": 0, "tripped": False, "tripped_at": None, "history": [fail, fail, ok]},
                  open(os.path.join(self.lane_dir(self.lane), "breaker.json"), "w"))
        self.assertEqual(self.reset("lead", "x")[0], 0)

    def test_a_traversal_anywhere_in_the_reason_is_blocked(self):
        self.new()
        self.good()
        self.assertEqual(self.reset("judge", "reports/judge/ok.md and ../x")[0], 2)
        self.assertEqual(self.reset("judge", "reports/judge/ok.md")[0], 0)

    # --- must ALLOW
    def test_judge_and_hub_reset_with_a_ruling_are_allowed(self):
        for by in ("judge", "hub"):
            self.new()
            self.good()
            rc, out, _ = self.reset(by, "per reports/judge/ok.md")
            self.assertEqual(rc, 0, out)
            self.assertFalse(self.breaker()["tripped"])
            self.assertTrue(self.breaker()["history"][-1].get("ruled"))
            self.assertIn("RESET by %s" % by, self.alarms())
            self.assertEqual(self.claim(self.lane, "w")[0], 0)

    def test_a_ticket_ruling_naming_the_failed_ticket_is_allowed(self):
        self.new()
        os.makedirs(os.path.join(self.hk, "reports", "PRJ-1"))
        with open(os.path.join(self.hk, "reports", "PRJ-1", "judge-r1.md"), "w") as f:
            f.write("# Round 1\n\nVerdict: RESET\n\nPRJ-1\n")
        self.assertEqual(self.reset("judge", "reports/PRJ-1/judge-r1.md")[0], 0)
        # a ticket that never failed cannot be cited
        self.new()
        os.makedirs(os.path.join(self.hk, "reports", "PRJ-2"))
        with open(os.path.join(self.hk, "reports", "PRJ-2", "judge-r1.md"), "w") as f:
            f.write("# Round 1\n\nVerdict: RESET\n\n%s PRJ-2\n" % self.lane)
        self.assertEqual(self.reset("judge", "reports/PRJ-2/judge-r1.md")[0], 2)

    def test_lead_reset_of_a_never_tripped_lane_is_allowed(self):
        for fails in (0, 1):
            self.new(fails=fails)
            self.assertEqual(self.reset("lead", "x")[0], 0)
        self.new(fails=2)                                   # trip, then ruled reset, then FAIL, PASS, FAIL
        self.good()
        self.assertEqual(self.reset("judge", "reports/judge/ok.md")[0], 0)
        self.rec_fail(self.lane, times=1)
        self.ll("record-pass", self.lane, "--ticket", "PRJ-1", "--gate", "bug-gate")
        self.rec_fail(self.lane, times=1)
        self.assertEqual(self.reset("lead", "x")[0], 0)

    def test_pass_on_an_untripped_lane_still_zeroes_the_streak(self):
        self.new(fails=1)
        self.ll("record-pass", self.lane, "--ticket", "PRJ-1", "--gate", "bug-gate")
        self.assertEqual(self.breaker()["fail_streak"], 0)


class Paths(Case):
    def test_everything_lands_under_hubkit_home_by_default(self):
        self.claim("repo", "a")
        self.ll("record-fail", "repo", "--ticket", "L-0001", "--gate", "other")
        self.ll("record-fail", "repo", "--ticket", "L-0001", "--gate", "other")
        seen = []
        for root, _, files in os.walk(self.home):
            seen += [os.path.join(root, f) for f in files]
        self.assertTrue(seen)
        for p in seen:
            self.assertTrue(p.startswith(self.hk + os.sep), p)

    def test_root_and_alarm_log_can_be_overridden(self):
        root, alarms = os.path.join(self.tmp, "r"), os.path.join(self.tmp, "a.log")
        env = {"LANE_LOCK_ROOT": root, "LANE_LOCK_ALARMS_LOG": alarms, "LANE_BREAKER_THRESHOLD": "1"}
        self.ll("claim", "repo", "--owner", "a", "--pid", str(self.me), env=env)
        self.ll("record-fail", "repo", "--ticket", "L-0001", "--gate", "other", env=env)
        self.assertTrue(os.path.isfile(os.path.join(root, "repo", "owner.json")))
        self.assertIn("BREAKER TRIPPED", open(alarms).read())
        self.assertFalse(os.path.exists(self.hk))

    def test_alarm_log_failure_never_crashes_the_command(self):
        bad = os.path.join(self.tmp, "notadir")
        open(bad, "w").close()
        env = {"LANE_LOCK_ALARMS_LOG": os.path.join(bad, "x.log"), "LANE_BREAKER_THRESHOLD": "1"}
        rc, out, _ = self.ll("record-fail", "repo", "--ticket", "L-0001", "--gate", "other", env=env)
        self.assertEqual(rc, 3)
        self.assertTrue(out.startswith("TRIPPED"))

    def test_two_claims_at_once_one_wins(self):
        procs = [subprocess.Popen([sys.executable, LL, "claim", "repo", "--owner", o, "--pid", str(self.me)],
                                  stdout=subprocess.PIPE, text=True, env=self.env) for o in ("a", "b", "c", "d")]
        out = [p.communicate()[0] for p in procs]
        # all four use the same live pid, so the first claim is held by a live process and the rest lose
        self.assertEqual(sum(o.startswith("CLAIMED") for o in out), 1, out)
        self.assertEqual(sum(o.startswith("REFUSED") for o in out), 3, out)


class LaneBinding(unittest.TestCase):
    """The binding function itself (_names_lane_or_ticket), loaded straight from the file under test."""

    @classmethod
    def setUpClass(cls):
        import importlib.util
        spec = importlib.util.spec_from_file_location("lane_lock_under_test", LL)
        cls.m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.m)

    def names(self, body, lane, tickets=()):
        return self.m._names_lane_or_ticket(body, list(tickets) + [lane], lane)

    def test_colon_lane_name_binds_as_a_whole_token(self):
        ln = "claude-config:L-7"
        for body in ("Lane claude-config:L-7\n", "lane: `claude-config:L-7`.\n", "lane=claude-config:L-7 ok\n",
                     "| Lane | Ruling |\n|--|--|\n| claude-config:L-7 | OK |\n", "Lanes: hub:L-1, claude-config:L-7\n"):
            self.assertTrue(self.names(body, ln), body)
        for body in ("Lane xclaude-config:L-7\n", "Lane x:claude-config:L-7\n", "Lane claude-config:L-7-b\n",
                     "Lane claude-config:L-70\n", "the claude-config:L-7 work\n", "Lane claude-config:L-7/x\n",
                     "| Lane |\n|--|\n| x:claude-config:L-7 |\n"):
            self.assertFalse(self.names(body, ln), body)

    def test_ticket_keeps_the_base_matching(self):
        for body in ("see reports/ZZ-1/judge-r3.md", "ticket:ZZ-1", "tickets ZZ-1/ZZ-2", "proj:ZZ-1: closed", "@ZZ-1", "(ZZ-1)", "ZZ-1."):
            self.assertTrue(self.names(body, "main", ["ZZ-1"]), body)
        for body in ("ZZ-10", "ZZ-1-r3", "xZZ-1", "ZZ_1"):
            self.assertFalse(self.names(body, "main", ["ZZ-1"]), body)

    def test_lane_needs_a_marker_next_to_it(self):
        for body in ("Lane: main", "lane main.", "lane=main x", "**Lane:** **`main`**", "Lane | main", "| Lane | main | OK |",
                     "Lanes: deploy, main, hub", "lane:main", "Lane\tmain", "| Lane | Ruling |\n|---|---|\n| main | CONTINUE |\n",
                     "| Ticket | Lane |\n|--|--|\n| L-1 | **main** |\n"):
            self.assertTrue(self.names(body, "main"), body)
        for body in ("the main branch is fine", "In the fast lane the main branch is fine", "the lane-lock work merged to main",
                     "lane\nmain", "Planes: main", "sublane main", "lane main/hub", "lane main-2", "lane main.v2", "lane deploy-main",
                     "Lanes: deploy-main, xmain", "| Lane |\n|--|\n| deploy-main |\n", "| Lane |\n|--|\n| x:main |\n",
                     "| Lane |\n|--|\n| @main |\n", "| Lane |\n|--|\n| +main |\n", "| Lane |\n|--|\n| a.main |\n",
                     "| Owner | Lane |\n|--|--|\n| main | hub |\n", "| Owner |\n|--|\n| main |\n"):
            self.assertFalse(self.names(body, "main"), body)

    def test_italic_underscores_unwrap_but_name_underscores_stay(self):
        for cell in ("_main_", "__main__", "**_main_**"):
            self.assertTrue(self.names("| Lane |\n|--|\n| %s |\n" % cell, "main"), cell)
        self.assertTrue(self.names("| Lane |\n|--|\n| my_lane |\n", "my_lane"))
        self.assertTrue(self.names("| Lane |\n|--|\n| _my_lane_ |\n", "my_lane"))
        for cell in ("_main", "main_", "x_main_"):
            self.assertFalse(self.names("| Lane |\n|--|\n| %s |\n" % cell, "main"), cell)

    def test_long_underscore_cell_is_linear(self):
        import subprocess, sys
        code = ("import importlib.util as u,sys;s=u.spec_from_file_location('m',sys.argv[1]);m=u.module_from_spec(s);"
                "s.loader.exec_module(m);b='| Lane |\\n|--|\\n| '+'_'*30000+' |\\n';"
                "print(m._names_lane_or_ticket(b,['main'],'main'))")
        r = subprocess.run([sys.executable, "-c", code, LL], capture_output=True, text=True, timeout=5)
        self.assertEqual(r.stdout.strip(), "False", r.stderr)


class Aliases(Case):
    """Mixed spellings of one lane (case, zero padding of the trailing ticket number) share ONE breaker.
    Bookkeeping dirs are hidden by exact name only."""

    def rf(self, lane):
        return self.ll("record-fail", lane, "--ticket", "PRJ-1", "--gate", "bug-gate", "--detail", "x")

    def seed(self, name, streak, tripped, raw=None):
        d = self.lane_dir(name)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "breaker.json"), "w") as f:
            f.write(raw if raw is not None else json.dumps(
                {"fail_streak": streak, "tripped": tripped, "tripped_at": "2026-01-01T00:00:00Z" if tripped else None,
                 "history": [{"ts": "2026-01-01T00:00:00Z", "ticket": "PRJ-1", "gate": "bug-gate", "result": "FAIL", "detail": ""}] * streak}))

    def judge_ok(self, lane):
        rj = os.path.join(self.hk, "reports", "judge")
        os.makedirs(rj, exist_ok=True)
        p = os.path.join(rj, "ok.md")
        with open(p, "w") as f:
            f.write("# Judge\n\nVERDICT: CONTINUE\n\nLane %s, ticket PRJ-1\n" % lane)
        os.utime(p, (time.time() + 60, time.time() + 60))

    def test_mixed_spellings_share_one_streak_and_trip(self):
        self.assertEqual(self.rf("repo-L-7")[0], 0)
        rc, out, _ = self.rf("Repo-l-0007")          # second FAIL, other spelling: trips only if the streak is shared
        self.assertEqual(rc, 3, out)
        self.assertEqual(self.claim("repo-L-0007", "a")[0], 3)
        self.assertEqual(self.claim("REPO-L-7", "a")[0], 3)
        self.assertEqual(self.claim("repo-L-70", "a")[0], 0)      # a different number is a different lane

    def test_the_worst_state_of_any_spelling_binds_and_a_refused_claim_creates_nothing(self):
        self.seed("repo-L-7", 0, False)
        self.seed("Repo-L-0007", 2, True)
        rc, out, _ = self.claim("repo-L-007", "a")
        self.assertEqual(rc, 3, out)
        self.assertIn("worst state binds", out)
        self.assertNotIn("repo-L-007", os.listdir(os.path.join(self.hk, "state", "lanes")))
        self.assertIn("breaker=TRIPPED", self.ll("status", "repo-L-7")[1])

    def test_a_corrupt_breaker_under_another_spelling_fails_closed(self):
        self.seed("repo-L-7", 0, False)
        self.seed("repo-l-0007", 0, False, raw="{not json")
        self.assertEqual(self.claim("repo-L-7", "a")[0], 3)

    def test_a_pass_reaches_every_spelling(self):
        self.seed("repo-L-7", 1, False)
        self.seed("repo-l-0007", 1, False)
        self.assertEqual(self.ll("record-pass", "repo-L-07", "--ticket", "PRJ-1", "--gate", "bug-gate")[0], 0)
        for n in ("repo-L-7", "repo-l-0007"):
            self.assertEqual(json.load(open(os.path.join(self.lane_dir(n), "breaker.json")))["fail_streak"], 0, n)

    def test_reset_clears_every_spelling_or_none(self):
        self.seed("repo-L-7", 2, True)
        self.seed("Repo-l-0007", 2, True)
        rc, _, _ = self.ll("reset", "repo-L-7", "--by", "ops", "--reason", "x")
        self.assertEqual(rc, 2)
        for n in ("repo-L-7", "Repo-l-0007"):
            self.assertTrue(json.load(open(os.path.join(self.lane_dir(n), "breaker.json")))["tripped"], n)
        self.judge_ok("repo-L-7")
        rc, out, _ = self.ll("reset", "repo-L-7", "--by", "judge", "--reason", "reports/judge/ok.md")
        self.assertEqual(rc, 0, out)
        self.assertIn("cleared 2 dirs", out)
        for n in ("repo-L-7", "Repo-l-0007"):
            self.assertFalse(json.load(open(os.path.join(self.lane_dir(n), "breaker.json")))["tripped"], n)

    def test_bookkeeping_dir_is_hidden_but_a_near_miss_name_is_not(self):
        self.assertEqual(self.claim("repo", "a")[0], 0)       # creates .keylocks via the key lock
        self.assertTrue(os.path.isdir(os.path.join(self.hk, "state", "lanes", ".keylocks")))
        self.seed(".keylocks2", 2, True)
        self.seed(".keylocks", 2, True)
        out = self.ll("list")[1]
        self.assertIn("lane=.keylocks2 breaker=TRIPPED", out)
        self.assertNotIn("lane=.keylocks ", out)
        self.assertIn("lane=repo ", out)

    def test_distinct_numbers_and_empty_or_oversize_names_are_not_folded_or_accepted(self):
        self.seed("node-18", 2, True)
        self.assertEqual(self.claim("node-180", "a")[0], 0)
        self.assertEqual(self.claim("node-018", "a")[0], 3)
        for bad in ("", "x" * 65, "a b", ".hidden"):
            self.assertEqual(self.claim(bad, "a")[0], 1, repr(bad))

    def test_status_streak_is_the_max_across_spellings_not_the_sum(self):
        self.seed("repo-L-7", 1, False)
        self.seed("repo-l-0007", 1, False)
        self.assertIn("breaker=closed fail_streak=1", self.ll("status", "repo-L-7")[1])

    def test_record_fail_and_claim_take_the_key_lock(self):
        import hashlib
        kl = os.path.join(self.hk, "state", "lanes", ".keylocks")
        os.makedirs(kl)
        with open(os.path.join(kl, "n-" + hashlib.sha1(b"repo-l-7").hexdigest()[:20]), "w") as f:
            f.write("not a dir")                       # a wrong-type lock path fails at once, never hangs
        for args in (("record-fail", "repo-L-0007", "--ticket", "PRJ-1", "--gate", "bug-gate"),
                     ("record-pass", "repo-L-7", "--ticket", "PRJ-1", "--gate", "bug-gate"),
                     ("claim", "Repo-l-7", "--owner", "a", "--pid", str(self.me)),
                     ("reset", "repo-L-7", "--by", "hub", "--reason", "x")):
            rc, out, err = self.ll(*args)
            self.assertEqual(rc, 1, (args[0], out, err))
            self.assertIn("not a directory", out + err, args[0])

    def test_a_ticket_ruling_with_a_ruling_label_line_is_accepted(self):
        self.seed("main", 2, True)
        d = os.path.join(self.hk, "reports", "PRJ-1")
        os.makedirs(d)
        p = os.path.join(d, "judge-r2.md")
        with open(p, "w") as f:
            f.write("# Round 2\n\nRuling: **RESET**\n\nPRJ-1\n")
        os.utime(p, (time.time() + 60, time.time() + 60))
        self.assertEqual(self.ll("reset", "main", "--by", "judge", "--reason", "reports/PRJ-1/judge-r2.md")[0], 0)

    def test_record_fail_stores_tip_and_verdict_block(self):
        v = os.path.join(self.tmp, "v-ok.md")
        with open(v, "w") as f:
            f.write("# gate\nVERDICT: FAIL\nSAW: ran the suite on tip abc, 2 reds\n")
        bad = os.path.join(self.tmp, "v-bad.md")
        with open(bad, "w") as f:
            f.write("just prose\n")
        self.assertEqual(self.ll("record-fail", "lane-x", "--ticket", "L-0001", "--gate", "bug-gate",
                                 "--verdict", v, "--tip", "ABCDEF1234")[0], 0)
        self.assertEqual(self.ll("record-fail", "lane-y", "--ticket", "L-0001", "--gate", "bug-gate",
                                 "--verdict", bad)[0], 0)
        self.assertEqual(self.ll("record-fail", "lane-z", "--ticket", "L-0001", "--gate", "bug-gate",
                                 "--verdict", "/no/such/file")[0], 0)   # never stops the FAIL being recorded

        def last(lane):
            for root, _d, files in os.walk(os.path.join(self.hk, "state", "lanes")):
                if "breaker.json" in files and lane in root:
                    return json.load(open(os.path.join(root, "breaker.json")))["history"][-1]
        a, b, c = last("lane-x"), last("lane-y"), last("lane-z")
        self.assertEqual((a["block"], a["tip"]), ("ok", "abcdef1234"))
        self.assertEqual(len(a["saw_hash"]), 12)
        self.assertEqual(b["block"], "missing")
        self.assertNotIn("saw_hash", b)
        self.assertNotIn("block", c)


class NativeWindows(unittest.TestCase):
    """On native Windows os.kill(pid, 0) can terminate a process. With sys.platform faked to win32,
    nothing may reach os.kill or ps. os.kill is REPLACED by a recorder here, never wrapped, so this
    test can not kill a real process even against old code."""

    def setUp(self):
        self.kills = []
        self.runs = []
        self.tmp = tempfile.mkdtemp()
        self.patches = [
            mock.patch.object(sys, "platform", "win32"),
            mock.patch.object(os, "kill", lambda pid, sig: self.kills.append((pid, sig))),
            mock.patch.object(subprocess, "run", lambda *a, **k: self.runs.append(a) or (_ for _ in ()).throw(OSError("no ps"))),
            mock.patch.dict(os.environ, {"HOME": self.tmp, "HUBKIT_HOME": os.path.join(self.tmp, "hk")}),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def load(self):
        spec = importlib.util.spec_from_file_location("lane_lock_under_test", LL)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_import_and_helpers_never_reach_kill_or_ps(self):
        mod = self.load()                                  # import alone, no main()
        with self.assertRaises(NotImplementedError) as cm:
            mod.pid_alive(1234)
        self.assertIn("docs/WINDOWS.md", str(cm.exception))
        with self.assertRaises(NotImplementedError):
            mod.proc_start_key(1234)
        with self.assertRaises(NotImplementedError):
            mod.pid_is_same_process(1234, None)
        self.assertEqual(self.kills, [])
        self.assertEqual(self.runs, [])

    def test_every_command_exits_1_naming_the_guide_without_killing(self):
        cmds = [["claim", "lane-a", "--owner", "o", "--pid", "1234"], ["release", "lane-a", "--owner", "o"],
                ["status", "lane-a"], ["list"], ["force-release", "lane-a", "--by", "b", "--reason", "r"]]
        for argv in cmds:
            with self.subTest(argv=argv):
                err, out = io.StringIO(), io.StringIO()
                with mock.patch.object(sys, "argv", ["lane-lock.py", *argv]), \
                        contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
                    with self.assertRaises(SystemExit) as cm:
                        runpy.run_path(LL, run_name="__main__")
                self.assertEqual(cm.exception.code, 1)
                self.assertIn("docs/WINDOWS.md", err.getvalue())
                self.assertEqual(self.kills, [])
                self.assertEqual(self.runs, [])
                self.assertFalse(os.path.exists(os.path.join(self.tmp, "hk")), "wrote state on win32")


if __name__ == "__main__":
    unittest.main(verbosity=1)
