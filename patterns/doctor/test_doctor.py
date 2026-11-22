#!/usr/bin/env python3
"""Tests for doctor.py. The rule: break ONE thing in a healthy scratch HOME and exactly that
check flips to FAIL (or WARN); everything else stays as it was."""
import datetime
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
DOCTOR = os.environ.get("DOCTOR", os.path.join(HERE, "doctor.py"))
T0 = 1_800_000_000
HOOK_FILES = {
    "model-guard/model-guard.py": "PreToolUse",
    "dup-worker-check/dup-worker-hook.py": "PreToolUse",
    "report-gate/report-gate.py": "SubagentStop",
    "ledger/ledger-banner-hook.py": "UserPromptSubmit",
}


def slurp(path):
    with open(path) as f:
        return f.read()


def spit(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.user = "zebrauser77"
        self.home = os.path.join(self.tmp, self.user)
        self.hk = os.path.join(self.home, ".claude", "hubkit")
        self.settings = os.path.join(self.home, ".claude", "settings.json")
        self.env = dict(os.environ, HOME=self.home, HUBKIT_HOME=self.hk, USER=self.user, LOGNAME=self.user,
                        DOCTOR_NOW=str(T0), DOCTOR_FREE_GI_OVERRIDE="100", PYTHONDONTWRITEBYTECODE="1")
        for k in ("LEDGER_PATH", "DOCTOR_SETTINGS", "DISK_GUARD_WARN_GI", "DISK_GUARD_CRIT_GI"):
            self.env.pop(k, None)
        self.healthy()

    def tearDown(self):
        for root, dirs, files in os.walk(self.tmp, topdown=False):
            for f in files:
                os.remove(os.path.join(root, f))
            for d in dirs:
                p = os.path.join(root, d)
                os.rmdir(p) if not os.path.islink(p) else os.remove(p)
        os.rmdir(self.tmp)

    def healthy(self):
        hooks = {}
        for rel, event in HOOK_FILES.items():
            spit(os.path.join(self.hk, rel), "# stand-in\n")
            hooks.setdefault(event, []).append(
                {"hooks": [{"type": "command", "command": "python3 ~/.claude/hubkit/" + rel}]})
        self.write_settings({"hooks": hooks})
        spit(os.path.join(self.hk, "turn-cap-builder", "turn-cap.py"), "# stand-in\n")
        spit(os.path.join(self.home, ".claude", "agents", "builder.md"),
             "---\nname: builder\nhooks: python3 ~/.claude/hubkit/turn-cap-builder/turn-cap.py\n---\n")
        spit(os.path.join(self.hk, "state", "ledger.jsonl"), '{"id":"L-0001"}\n{"id":"L-0002"}\n')
        spit(os.path.join(self.hk, "state", "heartbeats", "backup"), "%d %d\n" % (T0 - 600, 3600))
        os.makedirs(os.path.join(self.home, "work"))
        self.cwd = os.path.join(self.home, "work")

    def write_settings(self, data):
        spit(self.settings, json.dumps(data))

    def doctor(self, *args, env=None):
        r = subprocess.run([sys.executable, DOCTOR, *args], capture_output=True, text=True,
                           env=dict(self.env, **(env or {})), cwd=self.cwd)
        return r.returncode, r.stdout

    @staticmethod
    def statuses(out):
        res = {}
        for ln in out.splitlines():
            if ln[:5].strip() in ("PASS", "WARN", "FAIL", "SKIP"):
                res[ln[6:].split(":")[0]] = ln[:5].strip()
        return res

    def only_changed(self, out, expect):
        """expect: {check name: status}. Every other check must be as in the healthy run."""
        base = self.base
        now = self.statuses(out)
        diff = {k: v for k, v in now.items() if base.get(k) != v}
        self.assertEqual(diff, expect, out)


class Healthy(Case):
    def setUp(self):
        super().setUp()

    def test_healthy_hub_is_all_pass_exit_zero(self):
        rc, out = self.doctor()
        self.assertEqual(rc, 0, out)
        st = self.statuses(out)
        self.assertEqual({k for k, v in st.items() if v != "PASS"}, {"alarms"}, out)   # no alarms log yet
        for name in ("python", "home", "hooks-enabled", "hook model-guard.py", "hook dup-worker-hook.py",
                     "hook report-gate.py", "hook ledger-banner-hook.py", "agent", "ledger", "job backup", "disk"):
            self.assertIn(name, st)

    def test_empty_install_skips_instead_of_failing(self):
        for root, dirs, files in os.walk(self.hk, topdown=False):
            for f in files:
                os.remove(os.path.join(root, f))
            for d in dirs:
                os.rmdir(os.path.join(root, d))
        rc, out = self.doctor()
        self.assertEqual(rc, 0, out)
        self.assertIn("SKIP  hooks", out)


class Broken(Case):
    def setUp(self):
        super().setUp()
        self.base = self.statuses(self.doctor("--no-log")[1])

    def test_not_wired(self):
        s = json.loads(slurp(self.settings))
        del s["hooks"]["SubagentStop"]
        self.write_settings(s)
        rc, out = self.doctor()
        self.assertEqual(rc, 1)
        self.only_changed(out, {"hook report-gate.py": "FAIL"})
        self.assertIn("fix: add to the", out)
        self.assertIn("SubagentStop", out)

    def test_wired_under_wrong_event(self):
        s = json.loads(slurp(self.settings))
        s["hooks"]["PostToolUse"] = s["hooks"].pop("PreToolUse")[:1]
        s["hooks"]["PreToolUse"] = s["hooks"]["PostToolUse"][:0]
        self.write_settings(s)
        rc, out = self.doctor()
        self.assertEqual(rc, 1)
        self.assertIn("wired under PostToolUse but it must be under PreToolUse", out)

    def test_wired_to_a_missing_file(self):
        s = json.loads(slurp(self.settings))
        s["hooks"]["SubagentStop"][0]["hooks"][0]["command"] = "python3 ~/.claude/hubkit/elsewhere/report-gate.py"
        self.write_settings(s)
        rc, out = self.doctor()
        self.assertEqual(rc, 1)
        self.only_changed(out, {"hook report-gate.py": "FAIL"})
        self.assertIn("does not exist", out)

    def test_hooks_switched_off(self):
        s = json.loads(slurp(self.settings))
        s["disableAllHooks"] = True
        self.write_settings(s)
        rc, out = self.doctor()
        self.assertEqual(rc, 1)
        self.only_changed(out, {"hooks-enabled": "FAIL"})

    def test_settings_missing(self):
        os.remove(self.settings)
        rc, out = self.doctor()
        self.assertEqual(rc, 1)
        self.assertIn("FAIL  hooks: settings file", out)
        self.assertIn("--settings", out)

    def test_settings_not_json(self):
        spit(self.settings, "{ not json")
        rc, out = self.doctor()
        self.assertEqual(rc, 1)
        self.assertIn("not valid JSON", out)
        self.assertIn("fix the typo", out)
        self.assertNotIn("create it", out)
        self.assertNotIn("--settings FILE;", out)

    def test_settings_missing_still_says_create_it(self):
        os.remove(self.settings)
        rc, out = self.doctor()
        self.assertIn("create it", out)

    def test_settings_flag_reads_another_file(self):
        other = os.path.join(self.tmp, "other.json")
        spit(other, json.dumps({"hooks": {}}))
        rc, out = self.doctor("--settings", other)
        self.assertEqual(rc, 1)
        self.assertEqual(out.count("installed but not wired"), 4)

    def test_agent_missing(self):
        os.remove(os.path.join(self.home, ".claude", "agents", "builder.md"))
        rc, out = self.doctor()
        self.assertEqual(rc, 1)
        self.only_changed(out, {"agent": "FAIL"})
        self.assertIn("builder.md is missing", out)

    def test_agent_without_the_cap(self):
        spit(os.path.join(self.home, ".claude", "agents", "builder.md"), "---\nname: builder\n---\n")
        rc, out = self.doctor()
        self.assertEqual(rc, 1)
        self.only_changed(out, {"agent": "FAIL"})
        self.assertIn("does not call turn-cap.py", out)

    def test_project_scope_agent_warns(self):
        spit(os.path.join(self.cwd, ".claude", "agents", "builder.md"), "---\nname: builder\n---\n")
        rc, out = self.doctor()
        self.assertEqual(rc, 0)
        self.only_changed(out, {"agent": "WARN"})
        self.assertIn("trusted", out)

    def test_ledger_bad_line(self):
        spit(os.path.join(self.hk, "state", "ledger.jsonl"), '{"id":"L-0001"}\n{broken\n\n{"id":"L-0003"}\n')
        rc, out = self.doctor()
        self.assertEqual(rc, 1)
        self.only_changed(out, {"ledger": "FAIL"})
        self.assertIn("first at line 2", out)

    def test_ledger_line_that_is_not_an_object(self):
        spit(os.path.join(self.hk, "state", "ledger.jsonl"), '[1,2]\n')
        self.assertEqual(self.doctor()[0], 1)

    def test_ledger_path_override_is_used(self):
        other = os.path.join(self.tmp, "l.jsonl")
        spit(other, "oops\n")
        rc, out = self.doctor(env={"LEDGER_PATH": other})
        self.assertEqual(rc, 1)
        self.only_changed(out, {"ledger": "FAIL"})

    def test_stale_job(self):
        spit(os.path.join(self.hk, "state", "heartbeats", "backup"), "%d %d\n" % (T0 - 7200, 3600))
        rc, out = self.doctor()
        self.assertEqual(rc, 1)
        self.only_changed(out, {"job backup": "FAIL"})
        self.assertIn("has stopped", out)

    def test_corrupt_job(self):
        spit(os.path.join(self.hk, "state", "heartbeats", "backup"), "yesterday\n")
        rc, out = self.doctor()
        self.assertEqual(rc, 1)
        self.only_changed(out, {"job backup": "FAIL"})

    def test_future_job(self):
        spit(os.path.join(self.hk, "state", "heartbeats", "backup"), "%d 3600\n" % (T0 + 9999))
        rc, out = self.doctor()
        self.assertEqual(rc, 1)
        self.only_changed(out, {"job backup": "FAIL"})

    def test_job_with_no_limit_is_skipped_not_passed(self):
        spit(os.path.join(self.hk, "state", "heartbeats", "backup"), "%d\n" % (T0 - 600))
        rc, out = self.doctor()
        self.assertEqual(rc, 0)
        self.only_changed(out, {"job backup": "SKIP"})

    def test_plain_beat_of_a_registered_job_is_judged_by_its_registry_limit(self):
        # beat without a limit of its own, limit remembered in the registry: deadman says STALE, so must doctor
        spit(os.path.join(self.hk, "state", "heartbeats", "backup"), "%d\n" % (T0 - 7200))
        spit(os.path.join(self.hk, "state", "registry", "backup"), "3600\n")
        rc, out = self.doctor()
        self.assertEqual(rc, 1, out)
        self.only_changed(out, {"job backup": "FAIL"})
        self.assertIn("has stopped", out)

    def test_plain_beat_within_its_registry_limit_passes(self):
        spit(os.path.join(self.hk, "state", "heartbeats", "backup"), "%d\n" % (T0 - 600))
        spit(os.path.join(self.hk, "state", "registry", "backup"), "3600\n")
        rc, out = self.doctor()
        self.assertEqual(rc, 0, out)
        self.only_changed(out, {})

    def test_junk_registry_limit_is_ignored_not_a_crash(self):
        spit(os.path.join(self.hk, "state", "heartbeats", "backup"), "%d\n" % (T0 - 7200))
        for junk in (b"0\n", b"soon\n", b"\xff\xfe 5\n"):
            os.makedirs(os.path.join(self.hk, "state", "registry"), exist_ok=True)
            with open(os.path.join(self.hk, "state", "registry", "backup"), "wb") as f:
                f.write(junk)
            rc, out = self.doctor()
            self.assertEqual(rc, 0, out)
            self.only_changed(out, {"job backup": "SKIP"})

    def test_registry_temp_file_is_not_a_job(self):
        spit(os.path.join(self.hk, "state", "registry", "backup.tmp-123"), "3600\n")
        rc, out = self.doctor()
        self.assertEqual(rc, 0, out)
        self.only_changed(out, {})

    def test_job_exactly_at_limit_passes(self):
        spit(os.path.join(self.hk, "state", "heartbeats", "backup"), "%d 3600\n" % (T0 - 3600))
        self.assertEqual(self.doctor()[0], 0)

    def test_recent_alarm_warns_and_old_one_does_not(self):
        def stamp(sec_ago):
            return datetime.datetime.fromtimestamp(T0 - sec_ago).isoformat(timespec="seconds")
        spit(os.path.join(self.hk, "state", "alarms.log"), "%s deadman ALARM x\n" % stamp(90000))
        rc, out = self.doctor()
        self.assertEqual(rc, 0)
        self.only_changed(out, {"alarms": "PASS"})
        spit(os.path.join(self.hk, "state", "alarms.log"), "%s deadman ALARM x\n" % stamp(600))
        rc, out = self.doctor()
        self.assertEqual(rc, 0)
        self.only_changed(out, {"alarms": "WARN"})

    def test_disk_low_warns_and_critical_fails(self):
        rc, out = self.doctor(env={"DOCTOR_FREE_GI_OVERRIDE": "10"})
        self.assertEqual(rc, 0)
        self.only_changed(out, {"disk": "WARN"})
        rc, out = self.doctor(env={"DOCTOR_FREE_GI_OVERRIDE": "2"})
        self.assertEqual(rc, 1)
        self.only_changed(out, {"disk": "FAIL"})
        rc, out = self.doctor(env={"DOCTOR_FREE_GI_OVERRIDE": "30", "DISK_GUARD_WARN_GI": "40"})
        self.only_changed(out, {"disk": "WARN"})

    def test_home_missing(self):
        env = dict(HUBKIT_HOME=os.path.join(self.home, "nope"))
        rc, out = self.doctor(env=env)
        self.assertEqual(rc, 1)
        self.assertIn("FAIL  home: install folder", out)
        self.assertIn("does not exist", out)

    @unittest.skipIf(os.geteuid() == 0, "root ignores permissions")
    def test_doctor_creates_nothing_and_a_second_run_still_fails_home(self):
        empty = os.path.join(self.tmp, "fresh-home")
        os.makedirs(empty)
        env = dict(HOME=empty, HUBKIT_HOME=os.path.join(empty, ".claude", "hubkit"))
        for _ in range(2):
            rc, out = self.doctor(env=env)
            self.assertEqual(rc, 1, out)
            self.assertIn("FAIL  home", out)
            self.assertEqual(os.listdir(empty), [])
        rc, out = self.doctor("--paste", env=env)
        self.assertEqual(rc, 1)
        self.assertEqual(os.listdir(empty), [])

    def test_registered_job_with_a_deleted_beat_fails(self):
        spit(os.path.join(self.hk, "state", "registry", "nightly"), "3600\n")
        rc, out = self.doctor()
        self.assertEqual(rc, 1)
        self.only_changed(out, {"job nightly": "FAIL"})
        self.assertIn("heartbeat file is gone", out)

    def test_invalid_utf8_beat_fails_that_job_without_crashing(self):
        p = os.path.join(self.hk, "state", "heartbeats", "backup")
        with open(p, "wb") as f:
            f.write(b"\xff\xfe x")
        rc, out = self.doctor()
        self.assertEqual(rc, 1, out)
        self.only_changed(out, {"job backup": "FAIL"})

    def test_non_ascii_digit_beat_fails_without_crashing(self):
        spit(os.path.join(self.hk, "state", "heartbeats", "backup"), "\u00b2 3\n")
        rc, out = self.doctor()
        self.assertEqual(rc, 1, out)
        self.only_changed(out, {"job backup": "FAIL"})

    def test_home_not_writable(self):
        os.chmod(self.hk, 0o500)
        try:
            rc, out = self.doctor("--no-log")
        finally:
            os.chmod(self.hk, 0o700)
        self.assertEqual(rc, 1)
        self.assertIn("not writable", out)

    def test_old_python_fails(self):
        spec = importlib.util.spec_from_file_location("doctor_under_test", DOCTOR)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        real = sys.version_info
        try:
            sys.version_info = (3, 8, 10, "final", 0)
            r = mod.Report()
            mod.check_python(r)
        finally:
            sys.version_info = real
        self.assertEqual(r.rows[0][0], "FAIL")
        r = mod.Report()
        mod.check_python(r)
        self.assertEqual(r.rows[0][0], "PASS")

    def test_every_break_names_one_fix_line(self):
        os.remove(self.settings)
        rc, out = self.doctor()
        for status, line in ((l[:5].strip(), l) for l in out.splitlines()):
            if status == "FAIL":
                self.assertIn("fix:", out.split(line)[1].splitlines()[1])


def isoz(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


class Loop(Case):
    """The always-running loop checks. They exist only when work-loop (or a Stop entry for it) is present."""

    def st(self, out, name):
        return self.statuses(out).get(name)

    def install_loop(self, lanes=("lane-a",), beat_ago=300):
        spit(os.path.join(self.hk, "work-loop", "work_loop.py"), "# stand-in\n")
        spit(os.path.join(self.hk, "work-loop", "stop_arm.py"), "# stand-in\n")
        if lanes is not None:
            spit(os.path.join(self.hk, "state", "lanes.json"), json.dumps({"lanes": {n: {} for n in lanes}}))
        if beat_ago is not None:
            spit(os.path.join(self.hk, "state", "heartbeats", "work-loop-tick"), "%d 1800\n" % (T0 - beat_ago))
            spit(os.path.join(self.hk, "state", "registry", "work-loop-tick"), "1800\n")

    def wire_stop(self, cmd="python3 ~/.claude/hubkit/work-loop/stop_arm.py"):
        s = json.loads(slurp(self.settings))
        s["hooks"]["Stop"] = [{"hooks": [{"type": "command", "command": cmd}]}]
        self.write_settings(s)

    def test_nothing_installed_adds_no_loop_rows(self):
        rc, out = self.doctor()
        self.assertEqual(rc, 0, out)
        for name in ("loop-tick", "loop-feed", "loop-lanes", "stop-arm", "failure-register"):
            self.assertNotIn(name, self.statuses(out))

    def test_fresh_tick_passes_and_is_judged_once(self):
        self.install_loop()
        rc, out = self.doctor()
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.st(out, "loop-tick"), "PASS", out)
        self.assertNotIn("job work-loop-tick", self.statuses(out))

    def test_stale_tick_fails_with_a_fix_and_only_one_row(self):
        self.install_loop(beat_ago=1801)
        rc, out = self.doctor()
        self.assertEqual(rc, 1, out)
        self.assertEqual(self.st(out, "loop-tick"), "FAIL", out)
        self.assertNotIn("job work-loop-tick", self.statuses(out))
        self.assertIn("no longer being fed", out)

    def test_tick_exactly_at_the_limit_passes(self):
        self.install_loop(beat_ago=1800)
        self.assertEqual(self.st(self.doctor()[1], "loop-tick"), "PASS")

    def test_tick_that_never_ran_warns_not_fails(self):
        self.install_loop(beat_ago=None)
        rc, out = self.doctor()
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.st(out, "loop-tick"), "WARN", out)
        self.assertIn("every 10 minutes", out)

    def test_corrupt_and_future_tick_fail(self):
        self.install_loop()
        hb = os.path.join(self.hk, "state", "heartbeats", "work-loop-tick")
        spit(hb, "yesterday\n")
        self.assertEqual(self.st(self.doctor()[1], "loop-tick"), "FAIL")
        spit(hb, "%d 1800\n" % (T0 + 9999))
        self.assertEqual(self.st(self.doctor()[1], "loop-tick"), "FAIL")

    def test_no_lanes_is_a_skip_even_with_a_stale_beat(self):
        self.install_loop(lanes=None, beat_ago=99999)
        rc, out = self.doctor()
        self.assertEqual(self.st(out, "loop-tick"), "SKIP", out)

    def test_unreadable_lanes_file_fails(self):
        self.install_loop()
        spit(os.path.join(self.hk, "state", "lanes.json"), "{not json")
        rc, out = self.doctor()
        self.assertEqual(rc, 1, out)
        self.assertEqual(self.st(out, "loop-lanes"), "FAIL", out)

    def test_last_fed_ticket_is_reported_from_events(self):
        self.install_loop()
        spit(os.path.join(self.hk, "state", "events.jsonl"),
             json.dumps({"ts": isoz(T0 - 3000), "event": "loop.feed"}) + "\nnot json\n"
             + json.dumps({"ts": isoz(T0 - 600), "event": "loop.feed"}) + "\n"
             + json.dumps({"ts": isoz(T0 - 10), "event": "loop.next"}) + "\n")
        rc, out = self.doctor()
        self.assertEqual(self.st(out, "loop-feed"), "PASS", out)
        self.assertIn("last ticket fed 10m ago", out)

    def test_no_feed_yet_is_a_skip(self):
        self.install_loop()
        self.assertEqual(self.st(self.doctor()[1], "loop-feed"), "SKIP")

    def test_stop_arm_wired_to_a_missing_file_fails_even_without_the_pattern(self):
        self.wire_stop("python3 ~/.claude/hubkit/work-loop/stop_arm.py")
        rc, out = self.doctor()
        self.assertEqual(rc, 1, out)
        self.assertEqual(self.st(out, "stop-arm"), "FAIL", out)
        self.assertIn("fix:", out)

    def test_stop_arm_with_a_bare_name_cannot_be_found_and_fails(self):
        self.install_loop()
        self.wire_stop("python3 stop_arm.py")
        self.assertEqual(self.st(self.doctor()[1], "stop-arm"), "FAIL")

    def test_stop_arm_wired_and_present_passes(self):
        self.install_loop()
        self.wire_stop()
        rc, out = self.doctor()
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.st(out, "stop-arm"), "PASS", out)

    def test_stop_arm_with_no_lane_warns(self):
        self.install_loop(lanes=None, beat_ago=None)
        self.wire_stop()
        rc, out = self.doctor()
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.st(out, "stop-arm"), "WARN", out)

    def test_unwired_stop_arm_has_no_row(self):
        self.install_loop()
        self.assertNotIn("stop-arm", self.statuses(self.doctor()[1]))

    def test_other_stop_hooks_are_not_judged(self):
        self.wire_stop("python3 ~/.claude/hubkit/some-other-stop.py")
        self.assertNotIn("stop-arm", self.statuses(self.doctor()[1]))

    def register(self, tick_ago=600, failures=True):
        spit(os.path.join(self.hk, "failure-register", "failure_register.py"), "# stand-in\n")
        if failures:
            spit(os.path.join(self.hk, "state", "failures.jsonl"), "{}\n")
        if tick_ago is not None:
            spit(os.path.join(self.hk, "state", "failure-register.tick"), isoz(T0 - tick_ago) + "\n")

    def test_register_fresh_passes(self):
        self.register()
        self.assertEqual(self.st(self.doctor()[1], "failure-register"), "PASS")

    def test_register_older_than_two_hours_warns_exit_zero(self):
        self.register(tick_ago=7201)
        rc, out = self.doctor()
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.st(out, "failure-register"), "WARN", out)

    def test_register_exactly_two_hours_passes(self):
        self.register(tick_ago=7200)
        self.assertEqual(self.st(self.doctor()[1], "failure-register"), "PASS")

    def test_register_never_ran_warns_and_nothing_recorded_skips(self):
        self.register(tick_ago=None)
        self.assertEqual(self.st(self.doctor()[1], "failure-register"), "WARN")
        os.remove(os.path.join(self.hk, "state", "failures.jsonl"))
        self.assertEqual(self.st(self.doctor()[1], "failure-register"), "SKIP")

    def test_register_future_or_garbled_tick_warns(self):
        self.register(tick_ago=-5000)
        self.assertEqual(self.st(self.doctor()[1], "failure-register"), "WARN")
        spit(os.path.join(self.hk, "state", "failure-register.tick"), "soon\n")
        self.assertEqual(self.st(self.doctor()[1], "failure-register"), "WARN")

    def test_loop_checks_write_nothing(self):
        self.install_loop()
        self.wire_stop()
        self.register()

        def tree():
            return sorted((os.path.join(d, f), os.path.getmtime(os.path.join(d, f)))
                          for d, _, fs in os.walk(self.home) for f in fs)
        before = tree()
        self.doctor("--no-log")
        self.assertEqual(tree(), before)


class Paste(Case):
    def test_paste_has_no_home_user_or_foreign_paths(self):
        os.remove(self.settings)                              # make it print long paths and fixes
        spit(os.path.join(self.hk, "state", "heartbeats", "backup"), "%d 60\n" % (T0 - 9999))
        rc, out = self.doctor("--paste")
        self.assertEqual(rc, 1)
        self.assertNotIn(self.home, out)
        self.assertNotIn(self.user, out)
        self.assertNotIn(self.tmp, out)
        self.assertIn("~", out)

    def test_plain_output_names_the_real_install_path(self):
        rc, out = self.doctor()
        self.assertIn(self.hk, out)

    def test_paste_strips_an_absolute_path_outside_home(self):
        elsewhere = os.path.join(self.tmp, "client-secret-project", "hub")
        rc, out = self.doctor("--paste", env={"HUBKIT_HOME": elsewhere})
        self.assertIn("does not exist", out)
        self.assertNotIn("client-secret-project", out)
        self.assertIn("<path>/hub", out)

    def test_paste_strips_the_user_name_even_outside_a_path(self):
        spit(os.path.join(self.hk, "state", "heartbeats", self.user), "%d 60\n" % (T0 - 9999))
        rc, out = self.doctor("--paste")
        self.assertIn("job <user>", out)
        self.assertNotIn(self.user, out)

    def test_paste_scrubs_the_user_name_and_home_in_any_letter_case(self):
        spit(os.path.join(self.hk, "state", "heartbeats", self.user.upper() + "-nightly"), "%d 60\n" % (T0 - 9999))
        os.remove(self.settings)
        rc, out = self.doctor("--paste", env={"HUBKIT_HOME": self.hk.replace(self.user, self.user.upper())})
        self.assertNotIn(self.user.lower(), out.lower())
        self.assertNotIn(self.tmp.lower(), out.lower())
        # the whole home path, typed in a different case, still collapses to ~ (not just to <path>)
        rc, out = self.doctor("--paste", env={"HUBKIT_HOME": self.hk.upper()})
        self.assertIn("install folder ~/.CLAUDE/HUBKIT", out)

    def test_paste_keeps_the_fix_text_usable(self):
        os.remove(self.settings)
        rc, out = self.doctor("--paste")
        self.assertIn("--settings FILE", out)


class Safety(Case):
    def snapshot(self):
        snap = {}
        for root, _, files in os.walk(self.tmp):
            for f in files:
                p = os.path.join(root, f)
                snap[p] = slurp(p)
        return snap

    def test_no_log_writes_nothing_at_all(self):
        before = self.snapshot()
        self.doctor("--no-log")
        self.assertEqual(self.snapshot(), before)

    def test_default_run_only_adds_the_local_events_line(self):
        before = self.snapshot()
        self.doctor()
        after = self.snapshot()
        new = set(after) - set(before)
        self.assertEqual(new, {os.path.join(self.hk, "state", "events.log")})
        self.assertEqual({k: v for k, v in after.items() if k in before}, before)
        self.assertIn("doctor_run fail=0", slurp(os.path.join(self.hk, "state", "events.log")))

    def test_usage_error(self):
        self.assertEqual(self.doctor("--bogus")[0], 2)
        self.assertEqual(self.doctor("--settings")[0], 2)

    def test_unwritable_log_does_not_crash(self):
        rc, out = self.doctor("--no-log")
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main(verbosity=1)
