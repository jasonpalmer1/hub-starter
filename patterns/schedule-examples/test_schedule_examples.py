#!/usr/bin/env python3
"""Tests for the schedule examples: they parse, they follow the rules the deadman README gives,
and the command lines in them really record a beat on success and not on failure."""
import os
import plistlib
import re
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
EX = os.environ.get("EXAMPLES_DIR", HERE)
DEADMAN = os.path.join(HERE, "..", "deadman", "deadman.py")


def slurp(name):
    with open(os.path.join(EX, name)) as f:
        return f.read()


def plist(name, home="/h"):
    return plistlib.loads(slurp(name).replace("__HOME__", home).encode())


class Plists(unittest.TestCase):
    def test_both_parse_and_have_a_label_and_a_schedule(self):
        for name in ("launchd-job.plist.example", "launchd-deadman-check.plist.example"):
            p = plist(name)
            self.assertTrue(p["Label"].startswith("com.example."), name)
            self.assertTrue("StartInterval" in p or "StartCalendarInterval" in p, name)
            self.assertTrue(p["ProgramArguments"], name)

    def test_the_watcher_is_its_own_job(self):
        job, watch = plist("launchd-job.plist.example"), plist("launchd-deadman-check.plist.example")
        self.assertNotEqual(job["Label"], watch["Label"])
        self.assertEqual(watch["StartInterval"], 900)
        self.assertEqual(watch["ProgramArguments"][-2:], ["check", "--all"])
        self.assertNotIn("beat", " ".join(watch["ProgramArguments"]))

    def test_job_beats_only_on_success(self):
        cmd = plist("launchd-job.plist.example")["ProgramArguments"][-1]
        self.assertIn(" && ", cmd)
        self.assertNotIn(";", cmd)
        self.assertRegex(cmd, r"beat \S+ --max-age \d+[mhd]")

    def test_watcher_plist_can_pass_the_notify_command(self):
        env = plist("launchd-deadman-check.plist.example").get("EnvironmentVariables", {})
        self.assertIn("HUBKIT_NOTIFY_CMD", env)
        self.assertTrue(env["HUBKIT_NOTIFY_CMD"].startswith("/h/"))
        self.assertIn("EnvironmentVariables", slurp("README.md"))

    def test_paths_use_the_placeholder(self):
        for name in ("launchd-job.plist.example", "launchd-deadman-check.plist.example"):
            body = re.sub(r"<!--.*?-->", "", slurp(name), flags=re.S)
            self.assertIn("__HOME__", body)
            self.assertNotRegex(body, r"(?<!__HOME__)/\.claude/")

    def test_plutil_accepts_them_when_present(self):
        if subprocess.run(["which", "plutil"], capture_output=True).returncode != 0:
            self.skipTest("plutil is macOS only")
        for name in ("launchd-job.plist.example", "launchd-deadman-check.plist.example"):
            with tempfile.NamedTemporaryFile("w", suffix=".plist", delete=False) as f:
                f.write(slurp(name).replace("__HOME__", "/h"))
            try:
                r = subprocess.run(["plutil", "-lint", f.name], capture_output=True, text=True)
            finally:
                os.remove(f.name)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


class Cron(unittest.TestCase):
    def lines(self):
        return [l for l in slurp("crontab.example.txt").splitlines() if l.strip() and not l.startswith("#")]

    def test_two_entries_with_five_time_fields(self):
        ls = self.lines()
        self.assertEqual(len(ls), 2)
        for l in ls:
            self.assertGreaterEqual(len(l.split(None, 5)), 6, l)
            self.assertRegex(l.split()[0], r"^(\*/\d+|\d+|\*)$")

    def test_job_line_uses_and_and_the_watcher_is_separate(self):
        job, watch = self.lines()
        self.assertIn(" && ", job)
        self.assertNotIn(";", job)
        self.assertIn("beat my-job", job)
        self.assertIn("check --all", watch)
        self.assertNotIn("beat", watch)

    def run_job(self, job_cmd):
        home = tempfile.mkdtemp()
        try:
            os.makedirs(os.path.join(home, ".claude", "hubkit", "deadman"))
            os.symlink(os.path.abspath(DEADMAN), os.path.join(home, ".claude", "hubkit", "deadman", "deadman.py"))
            line = self.lines()[0]
            cmd = line.split(None, 5)[5].replace("YOUR_JOB_COMMAND", job_cmd)
            env = dict(os.environ, HOME=home, PYTHONDONTWRITEBYTECODE="1")
            env.pop("HUBKIT_HOME", None)
            subprocess.run(["sh", "-c", cmd], env=env, capture_output=True)
            return os.path.exists(os.path.join(home, ".claude", "hubkit", "state", "heartbeats", "my-job"))
        finally:
            for root, dirs, files in os.walk(home, topdown=False):
                for f in files:
                    os.remove(os.path.join(root, f))
                for d in dirs:
                    p = os.path.join(root, d)
                    os.rmdir(p) if not os.path.islink(p) else os.remove(p)
            os.rmdir(home)

    @unittest.skipUnless(os.path.isfile(DEADMAN), "needs the deadman pattern next to this one")
    def test_the_cron_command_beats_when_the_job_succeeds(self):
        self.assertTrue(self.run_job("true"))

    @unittest.skipUnless(os.path.isfile(DEADMAN), "needs the deadman pattern next to this one")
    def test_the_cron_command_does_not_beat_when_the_job_fails(self):
        self.assertFalse(self.run_job("false"))

    def test_notify_variable_is_explained(self):
        self.assertRegex(slurp("crontab.example.txt"), r"(?m)^# HUBKIT_NOTIFY_CMD=/")


class WindowsClock(unittest.TestCase):
    """The WSL2 pieces: keep-alive task XML, the loop cron lines, the toast script. None of this was run on a
    real Windows PC; these tests only check what can be checked here."""
    ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
    NS = "{http://schemas.microsoft.com/windows/2004/02/mit/task}"

    def test_keepalive_xml_parses_and_has_the_power_and_time_settings(self):
        import xml.etree.ElementTree as ET
        t = ET.fromstring(slurp("windows-keepalive.task.xml").encode("utf-8"))
        s = t.find(self.NS + "Settings")
        val = lambda n: s.find(self.NS + n).text
        self.assertEqual(val("DisallowStartIfOnBatteries"), "false")
        self.assertEqual(val("StopIfGoingOnBatteries"), "false")
        self.assertEqual(val("ExecutionTimeLimit"), "PT0S")
        self.assertEqual(val("MultipleInstancesPolicy"), "IgnoreNew")
        self.assertEqual(val("Hidden"), "true")
        self.assertIsNotNone(t.find(self.NS + "Triggers").find(self.NS + "LogonTrigger"))
        ex = t.find(self.NS + "Actions").find(self.NS + "Exec")
        self.assertEqual(ex.find(self.NS + "Command").text, "conhost.exe")
        self.assertEqual(ex.find(self.NS + "Arguments").text, "--headless wsl.exe --exec sleep infinity")

    def test_keepalive_xml_has_no_user_host_or_path(self):
        x = slurp("windows-keepalive.task.xml")
        self.assertNotRegex(x, r"(?i)\bUserId\b|\\Users\\|/Users/|/home/|C:\\|-d\s")
        self.assertNotIn("<Arguments>-d", x)

    def test_loop_cron_lines_equal_the_ones_in_always_running(self):
        want = [l for l in open(os.path.join(self.ROOT, "docs", "ALWAYS-RUNNING.md"), encoding="utf-8").read().splitlines()
                if re.match(r"^    \S+ \S+ \S+ \S+ \S+ python3 \$HOME/", l)]
        got = [l for l in slurp("crontab.loop.example.txt").splitlines() if l.strip() and not l.startswith("#")]
        self.assertEqual(len(got), 4)
        self.assertEqual(got, [w.strip() for w in want])
        self.assertRegex(slurp("crontab.loop.example.txt"), r"(?m)^# HUBKIT_NOTIFY_CMD=/")

    def _bin(self, root, with_stub):
        b = os.path.join(root, "bin")
        os.makedirs(b)
        import shutil
        for tool in ("bash", "base64", "tr", "iconv"):
            p = shutil.which(tool)
            self.assertTrue(p, tool + " needed for this test")
            os.symlink(p, os.path.join(b, tool))
        if with_stub:
            stub = os.path.join(b, "powershell.exe")
            with open(stub, "w") as f:
                f.write('#!/bin/sh\nprintf \'%s\' "$HK_TITLE_B64" > "$STUB_OUT.title"\n'
                        'printf \'%s\' "$HK_REASON_B64" > "$STUB_OUT.reason"\nprintf \'%s\\n\' "$@" > "$STUB_OUT.args"\nexit 0\n')
            os.chmod(stub, 0o755)
        return b

    def _notify(self, title, reason, with_stub=True):
        import base64
        import shutil
        root = tempfile.mkdtemp()
        try:
            env = {"PATH": self._bin(root, with_stub), "STUB_OUT": os.path.join(root, "out"), "HOME": root}
            r = subprocess.run([shutil.which("bash"), os.path.join(EX, "notify-windows.example.sh"), title, reason],
                               cwd=root, env=env, capture_output=True, text=True, timeout=30)
            got = {}
            for k in ("title", "reason", "args"):
                p = os.path.join(root, "out." + k)
                got[k] = open(p, encoding="utf-8").read() if os.path.exists(p) else None
            for k in ("title", "reason"):
                if got[k] is not None:
                    got[k] = base64.b64decode(got[k]).decode("utf-8")
            files = sorted(f for f in os.listdir(root) if f not in ("bin",) and not f.startswith("out."))
            return r, got, files
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_notify_passes_title_and_reason_as_data(self):
        r, got, files = self._notify("deadman: my-job is not beating", "no beat for 26h")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(got["title"], "deadman: my-job is not beating")
        self.assertEqual(got["reason"], "no beat for 26h")
        self.assertIn("-EncodedCommand", got["args"])

    def test_notify_hostile_text_runs_nothing_and_arrives_intact(self):
        nasty = ["it's", 'say "hi"', "$(touch X)", "`touch X2`", "line1\nline2 $(touch X3)", "a;touch X4;b", "-Command touch X5"]
        for text in nasty:
            with self.subTest(text=text):
                r, got, files = self._notify(text, text)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(got["title"], text)
                self.assertEqual(got["reason"], text)
                self.assertNotIn("touch", got["args"])
                self.assertEqual(files, [], "a hostile title created a file: %s" % files)

    def test_notify_without_powershell_exits_0_with_one_stderr_line(self):
        r, got, files = self._notify("t", "r", with_stub=False)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(len(r.stderr.strip().splitlines()), 1, r.stderr)
        self.assertIsNone(got["title"])

    def test_notify_script_is_executable_and_untested_claims_are_honest(self):
        self.assertTrue(os.access(os.path.join(HERE, "notify-windows.example.sh"), os.X_OK))
        self.assertIn("UNTESTED", slurp("notify-windows.example.sh"))
        self.assertIn("UNTESTED", slurp("windows-keepalive.task.xml"))


class Hygiene(unittest.TestCase):
    def test_no_personal_paths(self):
        for name in ("launchd-job.plist.example", "launchd-deadman-check.plist.example", "crontab.example.txt"):
            text = slurp(name)
            self.assertNotRegex(text, r"/(Users|home)/[A-Za-z]")


if __name__ == "__main__":
    unittest.main(verbosity=1)
