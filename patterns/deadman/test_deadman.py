#!/usr/bin/env python3
"""Tests for deadman.py. Each case runs the command line as a user would, in a scratch HOME."""
import os
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
DEADMAN = os.environ.get("DEADMAN", os.path.join(HERE, "deadman.py"))
T0 = 1_800_000_000   # a fixed "now" so ages are exact


def slurp(path):
    with open(path) as f:
        return f.read()


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.home = os.path.join(self.tmp, "home")
        self.hk = os.path.join(self.home, ".claude", "hubkit")
        os.makedirs(self.home)
        self.notes = os.path.join(self.tmp, "notified.txt")
        self.env = dict(os.environ, HOME=self.home, HUBKIT_HOME=self.hk, PYTHONDONTWRITEBYTECODE="1")
        self.env.pop("HUBKIT_NOTIFY_CMD", None)
        self.env.pop("DEADMAN_NOW", None)

    def tearDown(self):
        for root, dirs, files in os.walk(self.tmp, topdown=False):
            for f in files:
                os.remove(os.path.join(root, f))
            for d in dirs:
                os.rmdir(os.path.join(root, d))
        os.rmdir(self.tmp)

    def run_cmd(self, *args, at=T0, env=None):
        e = dict(self.env, **(env or {}))
        if at is not None:
            e["DEADMAN_NOW"] = str(at)
        r = subprocess.run([sys.executable, DEADMAN, *args], capture_output=True, text=True, env=e)
        return r.returncode, r.stdout, r.stderr

    def alarms(self):
        p = os.path.join(self.hk, "state", "alarms.log")
        return slurp(p).splitlines() if os.path.exists(p) else []

    def notify_script(self, exit_code=0):
        p = os.path.join(self.tmp, "notify.sh")
        with open(p, "w") as f:
            f.write('#!/bin/sh\nprintf "%%s|%%s\\n" "$1" "$2" >> "%s"\nexit %d\n' % (self.notes, exit_code))
        os.chmod(p, 0o755)
        return p

    def notified(self):
        return slurp(self.notes).splitlines() if os.path.exists(self.notes) else []


class Beat(Case):
    def test_beat_then_check_is_fresh(self):
        self.assertEqual(self.run_cmd("beat", "backup")[0], 0)
        rc, out, _ = self.run_cmd("check", "backup", "--max-age", "25m", at=T0 + 60)
        self.assertEqual(rc, 0)
        self.assertIn("ok", out)
        self.assertEqual(self.alarms(), [])

    def test_exactly_at_the_limit_is_fresh_one_second_over_is_stale(self):
        self.run_cmd("beat", "j")
        self.assertEqual(self.run_cmd("check", "j", "--max-age", "10m", at=T0 + 600)[0], 0)
        self.assertEqual(self.run_cmd("check", "j", "--max-age", "10m", at=T0 + 601)[0], 1)

    def test_stale_alarms_once_and_says_why(self):
        self.run_cmd("beat", "j")
        rc, out, _ = self.run_cmd("check", "j", "--max-age", "10m", at=T0 + 3 * 3600)
        self.assertEqual(rc, 1)
        self.assertIn("STALE", out)
        self.assertIn("3.0h", out)
        self.assertEqual(len(self.alarms()), 1)
        self.assertIn("j: last beat", self.alarms()[0])

    def test_missing_heartbeat_alarms_never_silent(self):
        rc, out, _ = self.run_cmd("check", "never-ran", "--max-age", "1h")
        self.assertEqual(rc, 1)
        self.assertIn("no heartbeat", out)
        self.assertEqual(len(self.alarms()), 1)

    def test_missing_heartbeat_directory_alarms_too(self):
        self.assertFalse(os.path.exists(os.path.join(self.hk, "state")))
        self.assertEqual(self.run_cmd("check", "x", "--max-age", "1h")[0], 1)

    def test_corrupt_heartbeat_alarms(self):
        d = os.path.join(self.hk, "state", "heartbeats")
        os.makedirs(d)
        for i, junk in enumerate(["", "abc\n", "1 2 3\n", "-5\n", "12abc 4\n"]):
            with open(os.path.join(d, "bad%d" % i), "w") as f:
                f.write(junk)
            rc, out, _ = self.run_cmd("check", "bad%d" % i, "--max-age", "1h")
            self.assertEqual(rc, 1, junk)
            self.assertIn("corrupt", out)

    def test_future_dated_heartbeat_is_stale_not_fresh(self):
        d = os.path.join(self.hk, "state", "heartbeats")
        os.makedirs(d)
        with open(os.path.join(d, "f"), "w") as f:
            f.write(str(T0 + 99999) + "\n")
        rc, out, _ = self.run_cmd("check", "f", "--max-age", "1h")
        self.assertEqual(rc, 1)
        self.assertIn("future", out)

    def test_beat_is_a_fresh_overwrite_not_an_append(self):
        self.run_cmd("beat", "j", at=T0)
        self.run_cmd("beat", "j", at=T0 + 5000)
        self.assertEqual(self.run_cmd("check", "j", "--max-age", "10m", at=T0 + 5100)[0], 0)
        with open(os.path.join(self.hk, "state", "heartbeats", "j")) as f:
            self.assertEqual(len(f.read().split()), 1)
        self.assertEqual([n for n in os.listdir(os.path.join(self.hk, "state", "heartbeats"))], ["j"])


class Limits(Case):
    def test_age_formats(self):
        self.run_cmd("beat", "j")
        for fmt, secs in (("90s", 90), ("2m", 120), ("1h", 3600), ("1d", 86400), ("300", 300)):
            self.assertEqual(self.run_cmd("check", "j", "--max-age", fmt, at=T0 + secs)[0], 0, fmt)
            self.assertEqual(self.run_cmd("check", "j", "--max-age", fmt, at=T0 + secs + 1)[0], 1, fmt)

    def test_bad_ages_are_usage_errors(self):
        for bad in ("0", "-1", "abc", "5x", "", "1.5h"):
            self.assertEqual(self.run_cmd("check", "j", "--max-age", bad)[0], 2, bad)

    def test_beat_remembers_max_age_for_check_all(self):
        self.run_cmd("beat", "a", "--max-age", "10m")
        self.run_cmd("beat", "b", "--max-age", "5h")
        rc, out, _ = self.run_cmd("check", "--all", at=T0 + 3600)
        self.assertEqual(rc, 1)
        self.assertIn("STALE a:", out)
        self.assertIn("b: ok", out)

    def test_check_without_any_limit_is_not_called_ok(self):
        self.run_cmd("beat", "j")
        rc, out, _ = self.run_cmd("check", "j")
        self.assertEqual(rc, 1)
        self.assertIn("no max age", out)

    def test_flag_overrides_remembered_limit(self):
        self.run_cmd("beat", "j", "--max-age", "10m")
        self.assertEqual(self.run_cmd("check", "j", "--max-age", "2h", at=T0 + 3600)[0], 0)

    def test_check_all_with_nothing_registered_is_a_failure_not_a_pass(self):
        rc, out, _ = self.run_cmd("check", "--all")
        self.assertEqual(rc, 1)
        self.assertIn("nothing to check", out)

    # --- check --all must never go quiet ---

    def test_all_with_missing_empty_or_unreadable_folder_alarms_and_notifies(self):
        hb = os.path.join(self.hk, "state", "heartbeats")
        notify = self.notify_script()
        env = {"HUBKIT_NOTIFY_CMD": notify}
        # (1) folder missing, (2) folder empty, (3) folder unreadable (3 only where permissions bind)
        cases = ["missing", "empty"] + ([] if os.geteuid() == 0 else ["unreadable"])
        for i, kind in enumerate(cases):
            for p in (os.path.join(self.hk, "state", "alarms.log"), self.notes):
                if os.path.exists(p):
                    os.remove(p)
            stamp = os.path.join(self.hk, "state", "deadman", "deadman-watcher.alarm")
            if os.path.exists(stamp):
                os.remove(stamp)
            if os.path.isdir(hb):
                os.chmod(hb, 0o700)
                os.rmdir(hb)
            if kind != "missing":
                os.makedirs(hb)
            if kind == "unreadable":
                os.chmod(hb, 0)
            try:
                rc, out, _ = self.run_cmd("check", "--all", env=env)
            finally:
                if os.path.isdir(hb):
                    os.chmod(hb, 0o700)
            self.assertEqual(rc, 1, kind)
            self.assertEqual(len(self.alarms()), 1, kind)
            self.assertIn("nothing to check", self.alarms()[0], kind)
            self.assertEqual(len(self.notified()), 1, kind)

    def test_all_alarms_for_a_registered_job_whose_beat_file_was_deleted(self):
        self.run_cmd("beat", "jobA", "--max-age", "1h")
        self.run_cmd("beat", "jobB", "--max-age", "1h")
        os.remove(os.path.join(self.hk, "state", "heartbeats", "jobB"))
        rc, out, _ = self.run_cmd("check", "--all", at=T0 + 60)
        self.assertEqual(rc, 1)
        self.assertIn("jobA: ok", out)
        self.assertIn("STALE jobB", out)
        self.assertEqual(len(self.alarms()), 1)
        self.assertIn("jobB", self.alarms()[0])

    def test_all_alarms_when_the_whole_state_folder_was_wiped_but_registry_survives(self):
        self.run_cmd("beat", "jobA", "--max-age", "1h")
        import shutil
        shutil.rmtree(os.path.join(self.hk, "state", "heartbeats"))
        rc, out, _ = self.run_cmd("check", "--all", at=T0 + 60)
        self.assertEqual(rc, 1)
        self.assertIn("STALE jobA", out)

    def test_plain_beat_keeps_the_registered_limit(self):
        self.run_cmd("beat", "jobA", "--max-age", "10m")
        self.run_cmd("beat", "jobA")
        rc, out, _ = self.run_cmd("check", "--all", at=T0 + 3600)
        self.assertEqual(rc, 1)
        self.assertIn("STALE jobA", out)
        self.assertIn("limit 10m", out)

    def test_beat_without_max_age_does_not_register(self):
        self.run_cmd("beat", "plain")
        self.assertEqual(os.listdir(os.path.join(self.hk, "state")), ["heartbeats"])

    def test_invalid_utf8_beat_alarms_that_job_and_the_rest_are_still_checked(self):
        self.run_cmd("beat", "jobA", "--max-age", "1h")
        self.run_cmd("beat", "jobB", "--max-age", "1h")
        with open(os.path.join(self.hk, "state", "heartbeats", "jobB"), "wb") as f:
            f.write(b"\xff\xfe x")
        rc, out, err = self.run_cmd("check", "--all", at=T0 + 7200)
        self.assertEqual(rc, 1, err)
        self.assertNotIn("Traceback", err)
        self.assertIn("STALE jobA", out)
        self.assertIn("STALE jobB", out)
        self.assertIn("corrupt", out)
        self.assertEqual(len(self.alarms()), 2)

    def test_non_ascii_digits_are_corrupt_not_a_crash(self):
        self.run_cmd("beat", "jobA", "--max-age", "1h")
        with open(os.path.join(self.hk, "state", "heartbeats", "jobA"), "w") as f:
            f.write("\u00b2 3\n")
        rc, out, err = self.run_cmd("check", "--all")
        self.assertEqual(rc, 1)
        self.assertNotIn("Traceback", err)
        self.assertIn("corrupt", out)

    def test_list_survives_an_invalid_utf8_beat(self):
        self.run_cmd("beat", "jobA", "--max-age", "1h")
        self.run_cmd("beat", "jobB", "--max-age", "1h")
        with open(os.path.join(self.hk, "state", "heartbeats", "jobB"), "wb") as f:
            f.write(b"\xff\xfe")
        rc, out, err = self.run_cmd("list")
        self.assertEqual(rc, 0, err)
        self.assertIn("jobA", out)
        self.assertIn("jobB", out)

    def test_list_shows_a_registered_job_whose_beat_is_gone(self):
        self.run_cmd("beat", "jobA", "--max-age", "1h")
        os.remove(os.path.join(self.hk, "state", "heartbeats", "jobA"))
        out = self.run_cmd("list")[1]
        self.assertIn("jobA", out)
        self.assertIn("no heartbeat recorded", out)

    def test_invalid_utf8_alarm_stamp_does_not_crash(self):
        self.run_cmd("beat", "jobA", "--max-age", "1m")
        d = os.path.join(self.hk, "state", "deadman")
        os.makedirs(d)
        with open(os.path.join(d, "jobA.alarm"), "wb") as f:
            f.write(b"\xff\xfe")
        rc, out, err = self.run_cmd("check", "--all", at=T0 + 9999)
        self.assertEqual(rc, 1, err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(len(self.alarms()), 1)

    def test_check_all_includes_corrupt_beats(self):
        d = os.path.join(self.hk, "state", "heartbeats")
        os.makedirs(d)
        with open(os.path.join(d, "broken"), "w") as f:
            f.write("zzz\n")
        rc, out, _ = self.run_cmd("check", "--all")
        self.assertEqual(rc, 1)
        self.assertIn("corrupt", out)

    def test_several_names_one_bad_exits_one(self):
        self.run_cmd("beat", "good")
        rc, out, _ = self.run_cmd("check", "good", "bad", "--max-age", "1h", at=T0 + 10)
        self.assertEqual(rc, 1)
        self.assertIn("good: ok", out)
        self.assertIn("STALE bad", out)


class Alarms(Case):
    def stale(self, at):
        return self.run_cmd("check", "j", "--max-age", "1m", at=at)

    def test_second_check_inside_the_window_does_not_repeat(self):
        self.run_cmd("beat", "j")
        self.stale(T0 + 600)
        rc, out, _ = self.stale(T0 + 600 + 3600)
        self.assertEqual(rc, 1)                  # still reported as stale
        self.assertIn("not repeating", out)
        self.assertEqual(len(self.alarms()), 1)  # but not alarmed again

    def test_alarm_repeats_after_the_window(self):
        self.run_cmd("beat", "j")
        self.stale(T0 + 600)
        self.stale(T0 + 600 + 7200 + 1)
        self.assertEqual(len(self.alarms()), 2)

    def test_realarm_flag_changes_the_window(self):
        self.run_cmd("beat", "j")
        self.run_cmd("check", "j", "--max-age", "1m", "--realarm", "10m", at=T0 + 600)
        self.run_cmd("check", "j", "--max-age", "1m", "--realarm", "10m", at=T0 + 600 + 601)
        self.assertEqual(len(self.alarms()), 2)

    def test_recovery_resets_so_the_next_failure_alarms_at_once(self):
        self.run_cmd("beat", "j", at=T0)
        self.stale(T0 + 600)
        self.run_cmd("beat", "j", at=T0 + 700)
        self.assertEqual(self.run_cmd("check", "j", "--max-age", "1m", at=T0 + 710)[0], 0)
        self.stale(T0 + 700 + 600)               # fails again, well inside 2h of the first alarm
        self.assertEqual(len(self.alarms()), 2)

    def test_each_job_has_its_own_window(self):
        self.run_cmd("beat", "a")
        self.run_cmd("beat", "b")
        self.run_cmd("check", "a", "b", "--max-age", "1m", at=T0 + 600)
        self.assertEqual(len(self.alarms()), 2)

    def test_notify_command_gets_title_and_reason(self):
        env = {"HUBKIT_NOTIFY_CMD": self.notify_script()}
        self.run_cmd("beat", "j")
        rc, out, _ = self.run_cmd("check", "j", "--max-age", "1m", at=T0 + 600, env=env)
        self.assertEqual(rc, 1)
        self.assertIn("notified", out)
        lines = self.notified()
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("deadman: j is not beating|j: last beat 10m ago"))

    def test_notify_not_called_for_a_repeat_or_a_fresh_job(self):
        env = {"HUBKIT_NOTIFY_CMD": self.notify_script()}
        self.run_cmd("beat", "j")
        self.run_cmd("check", "j", "--max-age", "1h", at=T0 + 10, env=env)
        self.assertEqual(self.notified(), [])
        self.run_cmd("check", "j", "--max-age", "1m", at=T0 + 600, env=env)
        self.run_cmd("check", "j", "--max-age", "1m", at=T0 + 700, env=env)
        self.assertEqual(len(self.notified()), 1)

    def test_failing_or_missing_notify_never_hides_the_alarm(self):
        self.run_cmd("beat", "j")
        for cmd in (self.notify_script(exit_code=3), "/no/such/command"):
            rc, out, err = self.run_cmd("check", "j", "--max-age", "1m", at=T0 + 600,
                                        env={"HUBKIT_NOTIFY_CMD": cmd})
            self.assertEqual(rc, 1)
            self.assertIn("alarm logged", out)
            self.assertIn("failed", out)
            self.assertNotIn("Traceback", err)
            os.remove(os.path.join(self.hk, "state", "deadman", "j.alarm"))
        self.assertEqual(len(self.alarms()), 2)

    def test_corrupt_alarm_stamp_still_alarms(self):
        self.run_cmd("beat", "j")
        d = os.path.join(self.hk, "state", "deadman")
        os.makedirs(d)
        with open(os.path.join(d, "j.alarm"), "w") as f:
            f.write("garbage")
        self.stale(T0 + 600)
        self.assertEqual(len(self.alarms()), 1)

    def test_events_log_records_what_ran_locally(self):
        self.run_cmd("beat", "j")
        self.stale(T0 + 600)
        with open(os.path.join(self.hk, "state", "events.log")) as f:
            self.assertIn("deadman_alarm j", f.read())


class Safety(Case):
    def test_names_cannot_escape_the_heartbeat_folder(self):
        for bad in ("../x", "a/b", "/etc/x", ".hidden", "", "a b", "x" * 65):
            for sub in ("beat", "check"):
                args = [sub, bad] + (["--max-age", "1h"] if sub == "check" else [])
                self.assertEqual(self.run_cmd(*args)[0], 2, (sub, bad))
        self.assertFalse(os.path.exists(os.path.join(self.hk, "state", "x")))

    def test_writes_stay_inside_the_install_folder(self):
        self.run_cmd("beat", "j", "--max-age", "1m")
        self.run_cmd("check", "j", at=T0 + 600)
        for root, _, files in os.walk(self.home):
            for f in files:
                self.assertTrue(os.path.join(root, f).startswith(self.hk), os.path.join(root, f))

    def test_usage_errors_exit_two(self):
        for args in ([], ["frob"], ["beat"], ["beat", "a", "b"], ["check"], ["check", "a", "--all"],
                     ["check", "a", "--nope"], ["beat", "a", "--all"]):
            self.assertEqual(self.run_cmd(*args)[0], 2, args)

    def test_unwritable_state_still_reports_stale(self):
        self.run_cmd("beat", "j")
        os.chmod(os.path.join(self.hk, "state"), 0o500)
        try:
            rc, out, _ = self.run_cmd("check", "j", "--max-age", "1m", at=T0 + 600)
        finally:
            os.chmod(os.path.join(self.hk, "state"), 0o700)
        self.assertEqual(rc, 1)
        self.assertIn("STALE", out)

    def test_runs_on_the_real_clock(self):
        self.run_cmd("beat", "j", at=None)
        self.assertEqual(self.run_cmd("check", "j", "--max-age", "5m", at=None)[0], 0)
        self.assertEqual(self.run_cmd("check", "j", "--max-age", "5m", at=int(time.time()) + 600)[0], 1)

    def test_list_shows_ages(self):
        self.run_cmd("beat", "a", "--max-age", "1h")
        rc, out, _ = self.run_cmd("list", at=T0 + 120)
        self.assertEqual(rc, 0)
        self.assertIn("a ", out)
        self.assertIn("2m", out)
        self.assertIn("limit 1.0h", out)


class RegistryHardening(Case):
    """S3 re-gate notes: registry temp files, retiring a job, and four lines that no test covered."""

    def reg(self, name):
        return os.path.join(self.hk, "state", "registry", name)

    def test_registry_temp_file_is_not_a_job(self):
        self.run_cmd("beat", "A", "--max-age", "1h")
        with open(self.reg("A.tmp-999"), "w") as f:
            f.write("3600\n")
        rc, out, _ = self.run_cmd("check", "--all", at=T0 + 60)
        self.assertEqual(rc, 0, out)
        self.assertNotIn("tmp-999", out)
        self.assertEqual(self.alarms(), [])

    def test_retire_removes_the_job_so_it_never_alarms_again(self):
        self.run_cmd("beat", "A", "--max-age", "1h")
        self.run_cmd("beat", "B", "--max-age", "1h")
        rc, out, _ = self.run_cmd("retire", "A")
        self.assertEqual(rc, 0, out)
        self.assertIn("retired A", out)
        for sub in ("heartbeats", "registry"):
            self.assertFalse(os.path.exists(os.path.join(self.hk, "state", sub, "A")), sub)
        rc, out, _ = self.run_cmd("check", "--all", at=T0 + 10 * 3600)
        self.assertNotIn("A:", out)          # A is gone; only B (now stale) is judged
        self.assertIn("B: last beat", out)

    def test_retire_clears_the_alarm_stamp_and_unknown_name_is_an_error(self):
        self.run_cmd("beat", "A", "--max-age", "1h")
        self.run_cmd("check", "--all", at=T0 + 5 * 3600)           # alarms, writes a stamp
        self.assertEqual(self.run_cmd("retire", "A")[0], 0)
        self.assertFalse(os.path.exists(os.path.join(self.hk, "state", "deadman", "A.alarm")))
        rc, out, err = self.run_cmd("retire", "nosuchjob")
        self.assertEqual(rc, 1)
        self.assertIn("nothing to retire", out + err)

    def test_retire_rejects_bad_names_and_extra_arguments(self):
        self.assertEqual(self.run_cmd("retire", "../x")[0], 2)
        self.assertEqual(self.run_cmd("retire")[0], 2)
        self.assertEqual(self.run_cmd("retire", "a", "b")[0], 2)

    def test_stale_alarm_text_says_how_to_retire(self):
        self.run_cmd("beat", "A", "--max-age", "1h")
        rc, out, _ = self.run_cmd("check", "--all", at=T0 + 5 * 3600)
        self.assertIn("deadman.py retire A", out)

    def test_watcher_alarm_clears_when_there_is_something_to_watch(self):
        rc, out, _ = self.run_cmd("check", "--all", at=T0)
        self.assertEqual(rc, 1)
        self.assertEqual(len(self.alarms()), 1)
        self.run_cmd("beat", "A", "--max-age", "1h", at=T0 + 10)
        self.assertEqual(self.run_cmd("check", "--all", at=T0 + 20)[0], 0)
        self.run_cmd("retire", "A", at=T0 + 30)
        # Recovered and broken again: a second alarm now, not "already sent" for two hours.
        rc, out, _ = self.run_cmd("check", "--all", at=T0 + 40)
        self.assertEqual(rc, 1)
        self.assertNotIn("not repeating", out)
        self.assertEqual(len(self.alarms()), 2)

    def test_watcher_alarm_uses_its_own_notify_title(self):
        notify = self.notify_script()
        rc, out, _ = self.run_cmd("check", "--all", env={"HUBKIT_NOTIFY_CMD": notify})
        self.assertEqual(rc, 1)
        self.assertEqual(len(self.notified()), 1)
        self.assertTrue(self.notified()[0].startswith("deadman: the watcher has nothing to watch|"),
                        self.notified())

    def test_registry_file_with_zero_or_junk_gives_no_limit_not_a_crash(self):
        self.run_cmd("beat", "A", "--max-age", "1h")
        with open(os.path.join(self.hk, "state", "heartbeats", "A"), "w") as f:
            f.write("%d\n" % T0)             # a beat with no limit of its own
        for junk in (b"0\n", b"abc\n", b"\xff\xfe\x00 9\n"):
            with open(self.reg("A"), "wb") as f:
                f.write(junk)
            rc, out, err = self.run_cmd("check", "--all", at=T0 + 60)
            self.assertEqual(rc, 1, junk)
            self.assertIn("no max age known", out, junk)
            self.assertNotIn("Traceback", err, junk)


if __name__ == "__main__":
    unittest.main(verbosity=1)
