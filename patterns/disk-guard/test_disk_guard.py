#!/usr/bin/env python3
"""Tests for disk-guard.py, run as a user would, in a scratch HOME."""
import os
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
GUARD = os.environ.get("GUARD", os.path.join(HERE, "disk-guard.py"))


def slurp(path):
    with open(path) as f:
        return f.read()


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.home = os.path.join(self.tmp, "home")
        self.hk = os.path.join(self.home, ".claude", "hubkit")
        os.makedirs(self.home)
        self.log = os.path.join(self.tmp, "dg.log")
        self.env = dict(os.environ, HOME=self.home, HUBKIT_HOME=self.hk, DISK_GUARD_LOG=self.log,
                        PYTHONDONTWRITEBYTECODE="1")
        for k in ("DISK_GUARD_WARN_GI", "DISK_GUARD_CRIT_GI", "DISK_GUARD_PATH",
                  "DISK_GUARD_FREE_GI_OVERRIDE", "DISK_GUARD_PS_FILE", "DISK_GUARD_CACHE_SECS",
                  "DISK_GUARD_CACHE_MIN_GI", "DISK_GUARD_CACHE_FILE"):
            self.env.pop(k, None)

    def tearDown(self):
        for root, dirs, files in os.walk(self.tmp, topdown=False):
            for f in files:
                os.remove(os.path.join(root, f))
            for d in dirs:
                os.rmdir(os.path.join(root, d))
        os.rmdir(self.tmp)

    def run_guard(self, *args, free=None, ps=None, env=None):
        e = dict(self.env, **(env or {}))
        if free is not None:
            e["DISK_GUARD_FREE_GI_OVERRIDE"] = str(free)
        if ps is not None:
            p = os.path.join(self.tmp, "ps.txt")
            with open(p, "w") as f:
                f.write("\n".join(ps) + "\n")
            e["DISK_GUARD_PS_FILE"] = p
        r = subprocess.run([sys.executable, GUARD, *args], capture_output=True, text=True, env=e)
        return r.returncode, r.stdout.strip()


class Thresholds(Case):
    def test_plenty_is_ok(self):
        rc, out = self.run_guard(free=100, ps=[])
        self.assertEqual(rc, 0)
        self.assertIn("100Gi free - OK", out)

    def test_boundaries_default_20_and_5(self):
        for free, rc, word in ((20, 0, "OK"), (19, 1, "WARN"), (5, 1, "WARN"), (4, 2, "CRIT"), (0, 2, "CRIT")):
            code, out = self.run_guard(free=free, ps=[])
            self.assertEqual(code, rc, free)
            self.assertIn(" " + word + " ", out + " ")

    def test_thresholds_are_settable(self):
        env = {"DISK_GUARD_WARN_GI": "40", "DISK_GUARD_CRIT_GI": "15"}
        self.assertEqual(self.run_guard(free=39, ps=[], env=env)[0], 1)
        self.assertEqual(self.run_guard(free=14, ps=[], env=env)[0], 2)
        self.assertEqual(self.run_guard(free=40, ps=[], env=env)[0], 0)

    def test_bad_settings_are_an_error_not_a_pass(self):
        for env in ({"DISK_GUARD_WARN_GI": "lots"}, {"DISK_GUARD_CRIT_GI": "-3"},
                    {"DISK_GUARD_WARN_GI": "5", "DISK_GUARD_CRIT_GI": "10"}):
            rc, out = self.run_guard(free=500, ps=[], env=env)
            self.assertEqual(rc, 2, env)
            self.assertIn("bad setting", out)

    def test_unreadable_disk_is_crit_never_ok(self):
        rc, out = self.run_guard("--path", os.path.join(self.tmp, "no-such-dir"), ps=[])
        self.assertEqual(rc, 2)
        self.assertIn("treating as CRIT", out)

    def test_unparseable_override_is_crit(self):
        rc, out = self.run_guard(free="lots", ps=[])
        self.assertEqual(rc, 2)

    def test_real_disk_read_gives_a_verdict(self):
        rc, out = self.run_guard("--path", self.tmp, ps=[])
        self.assertIn(rc, (0, 1, 2))
        self.assertRegex(out, r"^disk-guard: \d+Gi free - (OK|WARN|CRIT) ")


class Builds(Case):
    def test_counts_real_build_tools_only(self):
        ps = ["/usr/local/bin/node /usr/local/bin/npm run build",
              "node /opt/tools/.bin/vite build",
              "/usr/bin/make all",
              "/bin/zsh -c cd /work/webpack-notes",          # a folder name is not a build
              "vim makefile.txt",
              "/usr/bin/python3 summarize.py --next-week",
              "/usr/bin/python3 tool.py --mode make",      # tool name far from the program: not a build
              "pnpm install"]
        rc, out = self.run_guard(free=100, ps=ps)
        self.assertTrue(out.endswith("build tools running: 4"), out)

    def test_no_processes(self):
        self.assertTrue(self.run_guard(free=100, ps=[])[1].endswith("running: 0"))

    def test_count_does_not_change_the_exit_code(self):
        self.assertEqual(self.run_guard(free=100, ps=["npm run build"] * 5)[0], 0)

    def test_missing_ps_output_is_zero_not_a_crash(self):
        rc, out = self.run_guard(free=100, env={"DISK_GUARD_PS_FILE": os.path.join(self.tmp, "nope")})
        self.assertEqual(rc, 0)
        self.assertTrue(out.endswith("running: 0"))


class Safety(Case):
    def test_writes_only_its_log_line(self):
        self.run_guard(free=100, ps=[])
        self.assertIn("100Gi free - OK", slurp(self.log))
        before = set(os.listdir(self.tmp))
        self.run_guard(free=1, ps=[])
        self.assertEqual(set(os.listdir(self.tmp)), before)
        self.assertEqual(os.listdir(self.home), [])

    def test_log_defaults_under_the_install_folder(self):
        env = dict(self.env)
        del env["DISK_GUARD_LOG"]
        env["DISK_GUARD_FREE_GI_OVERRIDE"] = "100"
        subprocess.run([sys.executable, GUARD], capture_output=True, text=True, env=env)
        self.assertTrue(os.path.isfile(os.path.join(self.hk, "state", "disk-guard.log")))

    def test_unwritable_log_never_blocks_the_verdict(self):
        rc, out = self.run_guard(free=100, ps=[], env={"DISK_GUARD_LOG": "/proc/none/x.log"})
        self.assertEqual(rc, 0)

    def test_unknown_argument_is_a_usage_error(self):
        self.assertEqual(self.run_guard("--bogus", free=100, ps=[])[0], 2)


class OkCache(Case):
    def cenv(self, **kw):
        d = dict(DISK_GUARD_CACHE_FILE=os.path.join(self.tmp, "ok-cache.txt"))
        d.update(kw)
        return d

    def test_comfortable_ok_is_reused_and_logged_once(self):
        rc, out = self.run_guard(free=500, ps=[], env=self.cenv())
        self.assertEqual((rc, "cached" in out), (0, False))
        rc, out = self.run_guard(free=1, ps=[], env=self.cenv())      # live disk is now CRIT, the cache still answers
        self.assertEqual(rc, 0)
        self.assertIn("500Gi free - OK", out)
        self.assertIn("[cached ", out)
        self.assertEqual(len(slurp(self.log).strip().splitlines()), 1)  # the cached call wrote no log line

    def test_warn_crit_and_low_ok_are_never_cached(self):
        for free in (4, 10, 50):                                       # CRIT, WARN, OK under the 100Gi floor
            self.run_guard(free=free, ps=[], env=self.cenv())
            self.assertFalse(os.path.exists(os.path.join(self.tmp, "ok-cache.txt")), free)
        rc, out = self.run_guard(free=4, ps=[], env=self.cenv())
        self.assertEqual(rc, 2)

    def test_cache_off_and_expired_and_other_path_and_garbage(self):
        cf = os.path.join(self.tmp, "ok-cache.txt")
        self.run_guard(free=500, ps=[], env=self.cenv())
        rc, out = self.run_guard(free=1, ps=[], env=self.cenv(DISK_GUARD_CACHE_SECS="0"))
        self.assertEqual(rc, 2)                                        # switched off: live read
        os.utime(cf, (time.time() - 4000, time.time() - 4000))
        self.assertEqual(self.run_guard(free=1, ps=[], env=self.cenv())[0], 2)   # older than 30 min
        with open(cf, "w") as f:
            f.write("not a saved line\n")
        self.assertEqual(self.run_guard(free=1, ps=[], env=self.cenv())[0], 2)   # garbage never answers OK
        self.run_guard(free=500, ps=[], env=self.cenv())
        self.assertEqual(self.run_guard("--path", self.tmp, free=1, ps=[], env=self.cenv())[0], 2)  # different path

    def test_test_overrides_bypass_the_cache_by_default(self):
        self.run_guard(free=500, ps=[])
        self.assertFalse(os.path.exists(os.path.join(self.hk, "state", "disk-guard-ok-cache.txt")))


if __name__ == "__main__":
    unittest.main(verbosity=1)
