#!/usr/bin/env python3
"""Tests for trigger_check.py and base_read.py, on real throwaway git repos. Run: python3 test_diff_triggers.py
DT_DIR points at another folder holding the two scripts (the --mutate run uses that)."""
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.environ.get("DT_DIR") or HERE
TC = os.path.join(SRC, "trigger_check.py")
BR = os.path.join(SRC, "base_read.py")
ROWS = os.path.join(HERE, "triggers.example.txt")
ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
           GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com")


def sh(*a, cwd=None):
    return subprocess.run(a, cwd=cwd, env=ENV, capture_output=True, text=True)


def run(script, *a):
    return subprocess.run([sys.executable, script] + list(a), env=ENV, capture_output=True, text=True)


class Repo:
    def __init__(self, d):
        self.d = d
        sh("git", "init", "-q", "-b", "main", d)
        self.write("README.md", "hello\n")
        self.commit("base")
        self.base = self.rev()

    def write(self, p, text):
        full = os.path.join(self.d, p)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            f.write(text)

    def commit(self, msg):
        sh("git", "add", "-A", cwd=self.d)
        sh("git", "commit", "-q", "-m", msg, cwd=self.d)

    def rev(self):
        return sh("git", "rev-parse", "HEAD", cwd=self.d).stdout.strip()


class T(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.r = Repo(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def check(self, *extra):
        return run(TC, self.tmp.name, self.r.base, "HEAD", "--rows", ROWS, "--no-log", *extra)

    def test_doc_only_flags_nothing(self):
        self.r.write("docs/a.md", "x\n")
        self.r.commit("doc")
        p = self.check()
        self.assertEqual(p.returncode, 0)
        self.assertIn("0 rows matched", p.stdout)

    def test_sw_alone_flags_row_a_and_with_release_does_not(self):
        self.r.write("public/sw.js", "v1\n")
        self.r.commit("sw")
        self.assertIn("row a", self.check().stdout)
        self.r.write("src/release.json", "{}\n")
        self.r.commit("rel")
        self.assertNotIn("row a", self.check().stdout)

    def test_test_list_line_flags_row_b(self):
        self.r.write("scripts/test-list.txt", "t1\n")
        self.r.commit("list")
        self.assertIn("row b", self.check().stdout)

    def test_new_root_ui_file_flags_row_c_but_nested_does_not(self):
        self.r.write("src/deep/x.css", "a{}\n")
        self.r.commit("nested")
        self.assertNotIn("row c", self.check().stdout)
        self.r.write("page.html", "<p>\n")
        self.r.commit("root")
        self.assertIn("row c", self.check().stdout)

    def test_edit_of_existing_root_ui_file_is_not_row_c(self):
        self.r.write("old.html", "<p>\n")
        self.r.commit("old")
        self.r.base = self.r.rev()
        self.r.write("old.html", "<p>edited\n")
        self.r.commit("edit")
        self.assertNotIn("row c", self.check().stdout)

    def test_note_only_row_matches_nothing(self):
        self.r.write("anything.txt", "x\n")
        self.r.commit("x")
        self.assertNotIn("row d", self.check().stdout)

    def test_rules_edit_note(self):
        self.r.write("CLAUDE.md", "new rule\n")
        self.r.commit("rules")
        p = self.check()
        self.assertEqual(p.returncode, 0)
        self.assertIn("RULES-EDIT: this change edits CLAUDE.md", p.stdout)

    def test_row_deleted_in_the_diff_does_not_hide_it(self):
        # the table is read from the install root, never from the diff
        self.r.write("public/sw.js", "v1\n")
        self.r.write("triggers/triggers.tsv", "id\ttrigger\tcheck\tsource\treplaces\tmode\n")
        self.r.commit("sw and an empty table")
        self.assertIn("row a", self.check().stdout)

    def test_empty_replaces_refused(self):
        bad = os.path.join(self.tmp.name, "bad.tsv")
        with open(bad, "w") as f:
            f.write("id\ttrigger\tcheck\tsource\treplaces\tmode\nz\tchanged:*\tc\ts\t\tobserve\n")
        p = run(TC, self.tmp.name, self.r.base, "HEAD", "--rows", bad, "--no-log")
        self.assertEqual(p.returncode, 2)
        self.assertIn("empty `replaces`", p.stderr)

    def test_dash_replaces_only_for_none_rows(self):
        bad = os.path.join(self.tmp.name, "bad2.tsv")
        with open(bad, "w") as f:
            f.write("id\ttrigger\tcheck\tsource\treplaces\tmode\nz\tchanged:*\tc\ts\t-\tobserve\n")
        self.assertEqual(run(TC, self.tmp.name, self.r.base, "HEAD", "--rows", bad, "--no-log").returncode, 2)

    def test_bad_mode_and_bad_columns_refused(self):
        bad = os.path.join(self.tmp.name, "bad3.tsv")
        with open(bad, "w") as f:
            f.write("z\tchanged:*\tc\ts\tr\tenforce\n")
        self.assertEqual(run(TC, self.tmp.name, self.r.base, "HEAD", "--rows", bad, "--no-log").returncode, 2)
        with open(bad, "w") as f:
            f.write("z\tchanged:*\tc\n")
        self.assertEqual(run(TC, self.tmp.name, self.r.base, "HEAD", "--rows", bad, "--no-log").returncode, 2)

    def test_bad_ref_and_no_args_exit_2(self):
        self.assertEqual(run(TC, self.tmp.name, "no-such-ref", "HEAD", "--rows", ROWS, "--no-log").returncode, 2)
        self.assertEqual(run(TC).returncode, 2)

    def test_off_mode_row_is_silent(self):
        off = os.path.join(self.tmp.name, "off.tsv")
        with open(off, "w") as f:
            f.write("z\tchanged:*.md\tc\ts\tr\toff\n")
        self.r.write("n.md", "x\n")
        self.r.commit("n")
        p = run(TC, self.tmp.name, self.r.base, "HEAD", "--rows", off, "--no-log")
        self.assertIn("0 rows matched", p.stdout)

    def test_spaces_unicode_and_binary_paths_do_not_crash(self):
        self.r.write("dir with space/ünï.txt", "x\n")
        with open(os.path.join(self.tmp.name, "b.bin"), "wb") as f:
            f.write(b"\x00\x01\xff")
        self.r.commit("odd")
        self.assertEqual(self.check().returncode, 0)

    def test_writes_no_verdict_or_ledger_file(self):
        self.r.write("public/sw.js", "v1\n")
        self.r.commit("sw")
        before = sorted(os.listdir(self.tmp.name))
        self.check()
        self.assertEqual(before, sorted(os.listdir(self.tmp.name)))

    def test_log_written_when_asked(self):
        self.r.write("public/sw.js", "v1\n")
        self.r.commit("sw")
        log = os.path.join(self.tmp.name, "obs.log")
        run(TC, self.tmp.name, self.r.base, "HEAD", "--rows", ROWS, "--log", log)
        self.assertIn("trigger_row_would_flag row=a", open(log).read())

    # ---- base_read.py
    def test_base_read_prints_base_not_tip(self):
        self.r.write("CLAUDE.md", "base rule\n")
        self.r.commit("rule")
        base = self.r.rev()
        self.r.write("CLAUDE.md", "tip rule\n")
        self.r.commit("edit")
        p = run(BR, self.tmp.name, base, "CLAUDE.md")
        self.assertEqual((p.returncode, p.stdout), (0, "base rule\n"))

    def test_base_read_miss_never_falls_back_to_tip(self):
        self.r.write("REVIEW.md", "only on tip\n")
        self.r.commit("new rules file")
        p = run(BR, self.tmp.name, self.r.base, "REVIEW.md")
        self.assertEqual(p.returncode, 3)
        self.assertEqual(p.stdout, "")
        self.assertIn("MISS", p.stderr)

    def test_base_read_refuses_unsafe_paths_and_bad_base(self):
        for bad in ("/etc/passwd", "../x", "a/../../x", "a/..", "", "a\nb"):
            self.assertEqual(run(BR, self.tmp.name, self.r.base, bad).returncode, 2, repr(bad))
        self.assertEqual(run(BR, self.tmp.name, "no-such", "README.md").returncode, 2)
        self.assertEqual(run(BR, self.tmp.name).returncode, 2)

    def test_base_read_refuses_symlink_at_base(self):
        os.symlink("/etc/hosts", os.path.join(self.tmp.name, "CLAUDE.md"))
        self.r.commit("link")
        p = run(BR, self.tmp.name, self.r.rev(), "CLAUDE.md")
        self.assertEqual(p.returncode, 2)
        self.assertEqual(p.stdout, "")


if __name__ == "__main__":
    unittest.main(verbosity=1)
