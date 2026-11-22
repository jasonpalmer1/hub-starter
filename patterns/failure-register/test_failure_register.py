"""Tests for failure_register.py and record_failure.py. Run through test.sh (scratch HOME). Python 3.9."""
import ast
import gzip
import hashlib
import json
import os
import pwd
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLDIR = os.environ.get("FR_DIR") or HERE
REG = os.path.join(TOOLDIR, "failure_register.py")
REC = os.path.join(TOOLDIR, "record_failure.py")
FIXTURE = os.path.join(HERE, "fixtures", "replay-failures.txt")
NOW = "2026-10-07T20:00:00Z"
REAL_CLAUDE = os.path.join(pwd.getpwuid(os.getuid()).pw_dir, ".claude")

# credential-shaped strings are assembled at run time so no real-looking credential sits in this file
SK = "sk" + "-" + "A1b2C3d4E5f6G7h8I9j0"
GHP = "ghp" + "_" + "A1b2C3d4E5f6G7h8I9j0K1l2"
AKIA = "AKIA" + "ABCDEFGHIJKLMNOP"
JWT = "eyJ" + "hbGciOiJIUzI1" + "." + "eyJzdWIiOiIxMjM0" + "." + "abcDEF123"
BEARER = "Bearer " + "abcdef0123456789ABCDEF"
LONG = "Zx9" + "Qw8Er7Ty6Ui5Op4As3Df2Gh1Jk0Lm9Nb8Vc7Xz"
KV1 = "drowssap"[::-1] + "=" + "hunter2hunter2"
UD = "/" + "Us" + "ers"
U1 = "123e4567-e89b-12d3-a456-" + "426614174000"
U2 = "223e4567-e89b-12d3-a456-" + "426614174001"
U3 = "323e4567-e89b-12d3-a456-" + "426614174002"
KV2 = "nekot"[::-1] + ": " + "abc123abc123"


def mtimes(root):
    out = {}
    for r, dirs, files in os.walk(root):
        if r.count(os.sep) - root.count(os.sep) > 1:
            dirs[:] = []
        out[r] = os.stat(r).st_mtime_ns
    return out


def tree(root):
    out = {}
    for r, _d, files in os.walk(root):
        for f in files:
            p = os.path.join(r, f)
            with open(p, "rb") as fh:
                out[os.path.relpath(p, root)] = hashlib.sha1(fh.read()).hexdigest()
    return out


def rec(day, job="nightly", step="build", reason="boom", hour="01:00:00", **kw):
    d = {"ts": "2026-10-%02dT%sZ" % (day, hour), "job": job, "step": step, "reason": reason}
    d.update(kw)
    return d


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.hk = os.path.join(self.tmp.name, "hk")
        self.sd = os.path.join(self.hk, "state")
        os.makedirs(self.sd)
        self.inp = os.path.join(self.sd, "failures.jsonl")
        self.md = os.path.join(self.sd, "failure-register.md")
        self.tick = os.path.join(self.sd, "failure-register.tick")

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, records, path=None, raw=None):
        with open(path or self.inp, "w") as fh:
            if raw is not None:
                fh.write(raw)
            for r in records:
                fh.write(json.dumps(r) + "\n")

    def run_cli(self, *args):
        env = dict(os.environ, HUBKIT_HOME=self.hk, PYTHONDONTWRITEBYTECODE="1")
        return subprocess.run([sys.executable, REG, "--now", NOW] + list(args), env=env,
                              capture_output=True, text=True)

    def register(self, *args):
        p = self.run_cli(*args)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        name = "failure-register-replay.md" if ("--since" in args or "--until" in args) else "failure-register.md"
        with open(os.path.join(self.sd, name)) as fh:
            return fh.read()

    @staticmethod
    def rows(md):
        return [l for l in md.splitlines() if l.startswith("| rf-")]

    @staticmethod
    def repeats(md):
        return md.split("## Repeats")[1].split("## Standing")[0]


class Replay(Base):
    def run_fixture(self):
        with open(FIXTURE) as fh:
            data = fh.read()
        self.write([], raw=data)
        return self.register("--since", "2026-10-01", "--until", "2026-10-07")

    def test_fixture_flags_count_three_not_two(self):
        md = self.run_fixture()
        rep = self.repeats(md)
        self.assertIn("nightly | build | timeout", rep)
        self.assertIn("| 3 | 3 |", [r for r in self.rows(md) if "nightly | build" in r][0])
        self.assertNotIn("deploy", rep)
        self.assertIn("deploy", md.split("## Below threshold")[1])

    def test_fixture_no_reason_failures_kept_as_unknown(self):
        md = self.run_fixture()
        row = [r for r in self.rows(md) if "sync | pull" in r][0]
        self.assertIn("unknown", row)
        self.assertIn("| 3 |", row)

    def test_fixture_pass_after_last_failure_shown(self):
        md = self.run_fixture()
        row = [r for r in self.rows(md) if "nightly | build" in r][0]
        self.assertIn("last pass 2026-10-05", row)
        self.assertIn("passed-since", row)

    def test_fixture_has_no_names_paths_or_ids(self):
        with open(FIXTURE) as fh:
            data = fh.read()
        self.assertNotIn(UD, data)
        self.assertNotIn("/.claude", data)

    def test_replay_writes_no_heartbeat(self):
        self.run_fixture()
        self.assertFalse(os.path.exists(self.tick))
        self.assertTrue(os.path.exists(os.path.join(self.sd, "failure-register-replay.md")))


class Threshold(Base):
    def test_one_and_two_silent_three_flagged(self):
        recs = [rec(2, step="once")] + [rec(d, step="twice") for d in (3, 4)] + [rec(d, step="thrice") for d in (2, 3, 4)]
        self.write(recs)
        md = self.register()
        rep = self.repeats(md)
        self.assertIn("thrice", rep)
        self.assertNotIn("once", rep)
        self.assertNotIn("twice", rep)
        below = md.split("## Below threshold")[1]
        self.assertIn("once", below)
        self.assertIn("twice", below)

    def test_custom_min_count(self):
        self.write([rec(2), rec(3)])
        self.assertEqual(len(self.rows(self.register("--min-count", "2"))), 1)

    def test_burst_retries_are_one_incident(self):
        self.write([rec(2, hour="01:00:0%d" % i, step="t") for i in range(3)])
        row = [r for r in self.rows(self.register("--min-count", "1")) if "| t |" in r][0]
        self.assertIn("| 1 | 3 |", row)

    def test_window_excludes_old_and_future(self):
        old = [dict(rec(2, step="oldstep"), ts="2026-09-%02dT01:00:00Z" % d) for d in (1, 2, 3)]
        fut = [dict(rec(2, step="futstep"), ts="2026-10-%02dT01:00:00Z" % d) for d in (20, 21, 22)]
        self.write(old + fut + [rec(d, step="nowstep") for d in (4, 5, 6)])
        md = self.register()
        self.assertEqual(len(self.rows(md)), 1)
        self.assertNotIn("oldstep", md)
        self.assertNotIn("futstep", md)

    def test_flapping_signature_counts_incidents_not_days(self):
        self.write([rec(2), rec(3), rec(4), rec(5, hour="01:00:00"), rec(5, hour="01:00:30")])
        row = self.rows(self.register())[0]
        self.assertIn("| 4 | 5 |", row)


class Normalise(Base):
    def test_timestamps_and_pids_in_text_group_to_one_signature(self):
        recs = []
        for d, pid, h in ((2, 111, "ab12cd34ef"), (3, 2222, "99ffee1100"), (4, 33, "deadbeef01")):
            step = "deploy at 2026-10-%02dT01:00:09Z pid=%d commit %s took %d.%ds run %d" % (d, pid, h, 300 + d, d, d * 7)
            recs.append(rec(d, step=step, job="nightly job 2026-10-%02d" % d))
        self.write(recs)
        md = self.register()
        self.assertEqual(len(self.rows(md)), 1, md)
        self.assertIn("| 3 |", self.rows(md)[0])

    def test_uuid_and_clock_collapse(self):
        recs = [rec(d, step="x %s at %02d:10:00" % (u, d)) for d, u in
                ((2, U1), (3, U2), (4, U3))]
        self.write(recs)
        md = self.register()
        self.assertEqual(len(self.rows(md)), 1)
        self.assertIn("x <id> at <t>", md)

    def test_different_failures_stay_separate(self):
        self.write([rec(d, step="alpha") for d in (2, 3, 4)] + [rec(d, step="beta") for d in (2, 3, 4)])
        self.assertEqual(len(self.rows(self.register())), 2)

    def test_exit_code_makes_the_class(self):
        self.write([rec(d, reason="", exit=2, step="a") for d in (2, 3, 4)] + [rec(d, reason="", exit=124, step="b") for d in (2, 3, 4)])
        md = self.register()
        self.assertIn("a | exit-2", md)
        self.assertIn("b | timeout", md)

    def test_no_reason_line_is_unknown_not_dropped(self):
        self.write([rec(d, reason="", step="mystery") for d in (2, 3, 4)] + [{"ts": "2026-10-05T01:00:00Z", "job": "j", "step": "no-reason-key"}])
        md = self.register()
        self.assertIn("mystery | unknown", md)
        self.assertIn("no-reason-key | unknown", md)

    def test_one_failure_with_two_reasons_is_one_row(self):
        two = "request refused after timeout (404 too)"
        self.write([rec(d, reason=two, text=two, step="multi") for d in (2, 3, 4)])
        md = self.register()
        self.assertEqual(len(self.rows(md)), 1)
        self.assertIn("http-404", md)  # fixed priority, same answer every run

    def test_classes(self):
        for reason, cls in (("got 404", "http-404"), ("it refused", "refused"), ("timed out", "timeout"),
                            ("data is stale", "stale"), ("skipped today", "skipped")):
            self.write([rec(d, reason=reason, step="c") for d in (2, 3, 4)])
            self.assertIn("c | " + cls, self.register(), reason)

    def test_control_chars_and_pipes_cannot_forge_rows(self):
        evil = "a | b\x01\r\n| rf-ffffff | forged | 99 |"
        self.write([rec(d, job=evil) for d in (2, 3, 4)])
        md = self.register()
        self.assertEqual(len(self.rows(md)), 1)
        self.assertNotIn("\x01", md)
        self.assertNotIn("| rf-ffffff", md)

    def test_ids_are_stable(self):
        self.write([rec(d) for d in (2, 3, 4)])
        a = self.rows(self.register())[0].split("|")[1]
        b = self.rows(self.register())[0].split("|")[1]
        self.assertEqual(a, b)
        self.assertTrue(a.strip().startswith("rf-"))


class Inputs(Base):
    def test_rotation_mid_day_counts_once(self):
        a, b, c = rec(2), rec(3), rec(4)
        self.write([b, c])
        with gzip.open(self.inp + ".1.gz", "wt") as fh:  # rotation copied the 10-03 line into both files
            fh.write(json.dumps(a) + "\n" + json.dumps(b) + "\n")
        row = self.rows(self.register())[0]
        self.assertIn("| 3 | 3 |", row)

    def test_only_rotated_file_present(self):
        with open(self.inp + ".1", "w") as fh:
            for d in (2, 3, 4):
                fh.write(json.dumps(rec(d)) + "\n")
        self.assertEqual(len(self.rows(self.register())), 1)

    def test_corrupt_lines_are_skipped_and_reported(self):
        raw = "not json\n[1,2]\nnull\n\"str\"\n{\"ts\":\"nope\",\"job\":\"j\"}\n{\"job\":\"no ts\"}\n\x00\x01\n"
        raw += "".join(json.dumps(rec(d)) + "\n" for d in (2, 3, 4)) + "{\"truncated\": \n"
        self.write([], raw=raw)
        md = self.register()
        self.assertEqual(len(self.rows(md)), 1)
        self.assertIn("unreadable line(s) skipped", md)

    def test_binary_garbage_does_not_crash(self):
        with open(self.inp, "wb") as fh:
            fh.write(b"\xff\xfe\x00\x80" * 50 + b"\n" + (json.dumps(rec(2)) + "\n").encode())
        p = self.run_cli()
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_empty_input_writes_register_and_heartbeat(self):
        self.write([], raw="")
        md = self.register()
        self.assertIn("| - | none |", md)
        with open(self.tick) as fh:
            self.assertEqual(fh.read().strip(), NOW)

    def test_missing_input_warns_and_still_beats(self):
        md = self.register()
        self.assertIn("source missing", md)
        self.assertTrue(os.path.exists(self.tick))

    def test_heartbeat_rewritten_every_run(self):
        self.write([rec(2)])
        self.register()
        os.remove(self.tick)
        self.run_cli()
        self.assertTrue(os.path.exists(self.tick))

    def test_epoch_timestamps_accepted(self):
        import calendar
        import time
        base = calendar.timegm(time.strptime("2026-10-03", "%Y-%m-%d"))
        self.write([{"ts": base + i * 86400, "job": "j", "step": "s", "reason": "x"} for i in range(3)])
        self.assertEqual(len(self.rows(self.register())), 1)


class ReadOnly(Base):
    def test_never_opens_a_ticket_or_touches_anything_else(self):
        self.write([rec(d) for d in (2, 3, 4, 5, 6)])
        with open(os.path.join(self.sd, "ledger.jsonl"), "w") as fh:
            fh.write('{"id":"L-0001","status":"open"}\n')
        with open(os.path.join(self.hk, "notes.txt"), "w") as fh:
            fh.write("x")
        before = tree(self.hk)
        real = mtimes(REAL_CLAUDE)
        self.assertEqual(self.run_cli().returncode, 0)
        after = tree(self.hk)
        new = {k for k in after if k not in before}
        changed = {k for k in before if after.get(k) != before[k]}
        self.assertTrue(new <= {"state/failure-register.md", "state/failure-register.tick", "state/events.jsonl"}, new)
        self.assertEqual(changed, set())
        self.assertEqual(real, mtimes(REAL_CLAUDE))

    def test_explain_is_read_only(self):
        self.write([rec(d) for d in (2, 3)])
        before = tree(self.hk)
        p = self.run_cli("--explain", "build")
        self.assertEqual(p.returncode, 0)
        self.assertIn("2 raw lines", p.stdout)
        self.assertEqual(tree(self.hk), before)

    def test_repeat_event_emitted_for_open_row(self):
        self.write([rec(d) for d in (2, 3, 4)])
        self.run_cli()
        with open(os.path.join(self.sd, "events.jsonl")) as fh:
            ev = [json.loads(l) for l in fh]
        self.assertEqual([e["event"] for e in ev], ["failure.repeat"])
        self.assertEqual(ev[0]["count"], 3)

    def test_no_event_for_below_threshold(self):
        self.write([rec(2)])
        self.run_cli()
        self.assertFalse(os.path.exists(os.path.join(self.sd, "events.jsonl")))


class Leaks(Base):
    def blob(self):
        return " ".join([SK, GHP, AKIA, JWT, BEARER, LONG, KV1, KV2,
                         UD + "/alice/work/app/x.py", "~/private/notes.txt", "C:\\" + UD[1:] + "\\bob\\x.txt",
                         "/srv/data/deep/file.log"])

    def check(self, text):
        for needle in (SK, GHP, AKIA, JWT, "abcdef0123456789ABCDEF", LONG, "hunter2hunter2", "abc123abc123",
                       "alice", UD, "private/notes", "bob", "/srv/data"):
            self.assertNotIn(needle, text, needle)

    def test_register_contains_no_credentials_or_paths(self):
        b = self.blob()
        self.write([rec(d, job="job " + b, step="step " + b, reason=b, text=b) for d in (2, 3, 4)])
        md = self.register()
        self.check(md)
        self.assertEqual(len(self.rows(md)), 1)

    def test_explain_output_is_scrubbed_too(self):
        b = self.blob()
        self.write([rec(d, reason=b, text=b) for d in (2, 3, 4)])
        p = self.run_cli("--explain", "nightly")
        self.check(p.stdout)
        self.assertIn("<" + "secret" + ">", p.stdout)

    def test_typed_cause_cannot_carry_credentials_into_the_register(self):
        self.write([rec(d) for d in (2, 3, 4)])
        md = self.register()
        row = self.rows(md)[0]
        cells = row.split("|")
        cells[-3] = " fixed in %s/alice/x/y with %s " % (UD, GHP)
        with open(self.md, "w") as fh:
            fh.write(md.replace(row, "|".join(cells)))
        md2 = self.register()
        self.assertNotIn(GHP, md2)
        self.assertNotIn("alice", md2)
        self.assertIn("caused", md2)

    def test_plain_words_and_short_numbers_survive(self):
        self.write([rec(d, step="compile frontend") for d in (2, 3, 4)])
        self.assertIn("compile frontend", self.register())


class Cause(Base):
    def seed(self, cause):
        self.write([rec(d) for d in (2, 3, 4)])
        md = self.register()
        row = self.rows(md)[0]
        cells = row.split("|")
        cells[-3] = " %s " % cause
        with open(self.md, "w") as fh:
            fh.write(md.replace(row, "|".join(cells)))
        return self.rows(self.register())[0]

    def test_cause_survives_rerun(self):
        row = self.seed("disk was full")
        self.assertIn("disk was full", row)
        self.assertIn("| caused |", row)

    def test_recurrence_after_fix_date_reopens(self):
        self.assertIn("| reopened |", self.seed("patched @2026-10-02"))

    def test_failure_before_fix_date_stays_caused(self):
        self.assertIn("| caused |", self.seed("patched @2026-10-06"))

    def test_by_design(self):
        self.assertIn("| by-design |", self.seed("by-design: expected on weekends"))

    def test_no_cause_is_open(self):
        self.write([rec(d) for d in (2, 3, 4)])
        self.assertIn("| open |", self.rows(self.register())[0])

    def test_second_run_without_a_typed_cause_stays_open(self):
        self.write([rec(d) for d in (2, 3, 4)])
        self.register()
        row = self.rows(self.register())[0]
        self.assertIn("| open |", row)
        self.assertNotIn("| caused |", row)


class Standing(Base):
    LAUNCH = "PID\tStatus\tLabel\n-\t1\tcom.example.weekly\n-\t0\tcom.example.nightly\n-\t-9\tcom.example.x\n12\t0\tcom.other.foo\n-\t1\tcom.other.bar\n"

    def test_prefix_nonzero_flagged_regardless_of_count(self):
        lf = os.path.join(self.tmp.name, "lc.txt")
        with open(lf, "w") as fh:
            fh.write(self.LAUNCH)
        self.write([])
        md = self.register("--launchctl-file", lf, "--prefix", "com.example.")
        self.assertIn("com.example.weekly", md)
        self.assertIn("com.example.x", md)
        self.assertNotIn("com.example.nightly", md)
        self.assertNotIn("com.other.bar", md)

    def test_without_prefix_nothing_flagged(self):
        lf = os.path.join(self.tmp.name, "lc.txt")
        with open(lf, "w") as fh:
            fh.write(self.LAUNCH)
        self.write([])
        self.assertNotIn("com.example", self.register("--launchctl-file", lf))


class Helper(Base):
    def rf(self, *args):
        env = dict(os.environ, HUBKIT_HOME=self.hk, PYTHONDONTWRITEBYTECODE="1")
        return subprocess.run([sys.executable, REC] + list(args), env=env, capture_output=True, text=True)

    def lines(self):
        with open(self.inp) as fh:
            return [json.loads(l) for l in fh]

    def test_appends_one_line_each(self):
        self.assertEqual(self.rf("--job", "j", "--step", "s", "--reason", "boom", "--exit", "2").returncode, 0)
        self.assertEqual(self.rf("--job", "j", "--step", "s", "--ok").returncode, 0)
        rows = self.lines()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["exit"], 2)
        self.assertTrue(rows[1]["ok"])

    def test_scrubs_credentials_paths_and_newlines(self):
        self.rf("--job", "j", "--reason", "a\nb " + GHP + " " + UD + "/alice/x/y", "--text", SK + "\r\nz")
        with open(self.inp) as fh:
            raw = fh.read()
        self.assertEqual(len(raw.splitlines()), 1)
        for n in (GHP, SK, "alice"):
            self.assertNotIn(n, raw)

    def test_text_is_capped(self):
        self.rf("--job", "j", "--reason", "x " * 5000)
        self.assertLessEqual(len(self.lines()[0]["reason"]), 300)

    def test_round_trip_into_register(self):
        for d in (2, 3, 4):
            self.rf("--job", "j", "--step", "s", "--reason", "timed out", "--exit", "1", "--ts", "2026-10-0%dT01:00:00Z" % d)
        self.assertEqual(len(self.rows(self.register())), 1)

    def test_unwritable_path_exits_1_quietly(self):
        blocker = os.path.join(self.tmp.name, "afile")
        with open(blocker, "w") as fh:
            fh.write("x")
        p = self.rf("--job", "j", "--file", os.path.join(blocker, "sub", "f.jsonl"))
        self.assertEqual(p.returncode, 1)
        self.assertNotIn("Traceback", p.stderr)

    def test_appends_never_truncates(self):
        self.write([rec(2)])
        self.rf("--job", "j")
        self.assertEqual(len(self.lines()), 2)


class Source(unittest.TestCase):
    def test_python39_syntax_only(self):
        for p in (REG, REC):
            with open(p) as fh:
                ast.parse(fh.read(), feature_version=(3, 9))

    def test_register_has_no_ticket_or_network_code(self):
        with open(REG) as fh:
            src = fh.read().lower()
        for word in ("ledger", "subprocess", "urllib", "socket"):
            self.assertNotIn(word, src)


if __name__ == "__main__":
    unittest.main(verbosity=1)
