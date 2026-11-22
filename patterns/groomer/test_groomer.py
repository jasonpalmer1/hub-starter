#!/usr/bin/env python3
"""Tests for groomer.py. Run: python3 test_groomer.py"""
import importlib.util
import json
import os
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp()
STATE = os.path.join(TMP, "state")
os.makedirs(STATE)
os.environ["GROOM_STATE_DIR"] = STATE
os.environ["HUBKIT_HOME"] = os.path.join(TMP, "none")
spec = importlib.util.spec_from_file_location("groomer", os.path.join(HERE, "groomer.py"))
G = importlib.util.module_from_spec(spec)
spec.loader.exec_module(G)


def flag(name, on=True):
    p = os.path.join(STATE, name)
    if on:
        open(p, "w").close()
    elif os.path.exists(p):
        os.remove(p)


def reset():
    for n in ("groom-off", "groom-shadow", G.EVENTS_NAME):
        flag(n, False)


ITEMS = [
    {"id": "L-0001", "title": "ALARM disk full on build host nightly", "status": "open", "source": "alarm"},
    {"id": "L-0002", "title": "ALARM disk full on build host nightly", "status": "done", "source": "alarm"},
    {"id": "L-0003", "title": "ALARM disk full on build host nightly", "status": "open", "source": "operator-chat"},
]


class T(unittest.TestCase):
    def setUp(self):
        reset()

    def test_mode_files(self):
        self.assertEqual(G.mode(), "live")
        flag("groom-shadow")
        self.assertEqual(G.mode(), "shadow")
        flag("groom-off")
        self.assertEqual(G.mode(), "off")

    def test_missing_state_dir_is_shadow_never_live(self):
        self.assertEqual(G.mode(G.Path(TMP) / "nope"), "shadow")

    def test_kind_and_title_shaped_finish_line(self):
        self.assertEqual(G.suggest_kind("the login page is broken"), "bug")
        self.assertEqual(G.suggest_kind("fix the typo in the header"), "polish")
        self.assertEqual(G.suggest_kind("think about next quarter"), "process")
        self.assertIn("answer is written", G.done_line("operator-chat", "bug", "Count the open items"))
        self.assertIn("broken behavior", G.done_line("operator-chat", "bug", "login page broken"))
        self.assertIn("alarm line", G.done_line("alarm", None, "Check the alarm"))
        self.assertEqual(G.done_line("nonsense", None, "zzz"), G.DONE_TEMPLATES["other"])

    def test_groom_fills_empty_finish_line_only(self):
        r = G.groom("login page is broken", "operator-chat")
        self.assertEqual(r["mode"], "live")
        self.assertIn("broken behavior", r["done_means"])
        self.assertEqual(r["kind"], "bug")
        r2 = G.groom("login page is broken", "operator-chat", done_means="Already written")
        self.assertNotIn("done_means", r2)

    def test_shadow_changes_nothing_but_counts(self):
        flag("groom-shadow")
        r = G.groom("login page is broken", "operator-chat")
        self.assertEqual(r, {"mode": "shadow"})
        with open(os.path.join(STATE, G.EVENTS_NAME)) as fh:
            line = fh.read()
        self.assertIn("groom_fill", line)
        self.assertNotIn("login", line)         # counts only, never titles

    def test_off_does_nothing_at_all(self):
        flag("groom-off")
        self.assertEqual(G.groom("login page is broken", "operator-chat"), {"mode": "off"})
        self.assertFalse(os.path.exists(os.path.join(STATE, G.EVENTS_NAME)))

    def test_machine_repeat_folds_only_into_open_machine_items(self):
        r = G.groom("ALARM disk full on build host nightly", "alarm", items=ITEMS)
        self.assertEqual(r["fold_to"], "L-0001")        # not the done item, not the person's item

    def test_person_item_is_never_folded(self):
        r = G.groom("ALARM disk full on build host nightly", "operator-chat", items=ITEMS)
        self.assertNotIn("fold_to", r)

    def test_protected_source_wins_even_if_also_listed_as_machine(self):
        os.environ["MACHINE_SOURCES"] = "alarm,operator-chat"
        try:
            items = [{"id": "L-0001", "title": "disk full on build host", "status": "open", "source": "alarm"}]
            self.assertNotIn("fold_to", G.groom("disk full on build host", "operator-chat", items=items))
        finally:
            del os.environ["MACHINE_SOURCES"]

    def test_closed_items_never_match(self):
        items = [{"id": "L-0001", "title": "disk full on build host", "status": s, "source": "alarm"} for s in ("done", "dropped")]
        self.assertEqual(G.find_similar(items, "disk full on build host"), [])

    def test_different_ids_are_different_things(self):
        items = [{"id": "L-0001", "title": "JUDGE NEEDED L-0007 three gate fails", "status": "open", "source": "alarm"}]
        self.assertEqual(G.find_similar(items, "JUDGE NEEDED L-0003 three gate fails", only_machine=True), [])

    def test_short_titles_never_duplicates(self):
        items = [{"id": "L-0001", "title": "disk full", "status": "open", "source": "alarm"}]
        self.assertEqual(G.find_similar(items, "disk full"), [])

    def test_hostile_titles_do_not_raise(self):
        for t in (None, "", "x" * 20000, "\u0000‮", "ALARM " * 5000, 12345):
            r = G.groom(t, "alarm", items=ITEMS)
            self.assertIn("mode", r)

    def test_env_machine_sources_override(self):
        os.environ["MACHINE_SOURCES"] = "robot"
        try:
            items = [{"id": "L-0001", "title": "disk full on build host", "status": "open", "source": "robot"}]
            self.assertEqual(G.groom("disk full on build host", "robot", items=items)["fold_to"], "L-0001")
        finally:
            del os.environ["MACHINE_SOURCES"]

    def test_verify_suggestion_only_with_sibling_pattern(self):
        hosts = os.path.join(TMP, "hosts.txt")
        with open(hosts, "w") as f:
            f.write("example.com\n")
        os.environ["VERIFY_HOSTS"] = hosts
        title = 'see https://example.com/a shows "Free plan"'
        try:
            # sibling PRESENT (patterns/verify-watch/verify_watch.py is next to this folder) and the host is allowed: a line comes back
            self.assertTrue(os.path.isfile(os.path.join(HERE, "..", "verify-watch", "verify_watch.py")))
            self.assertEqual(G.suggest_verify(title, "", "other"), 'VERIFY: url=https://example.com/a has="Free plan" days=3')
            # a person-sourced stranger mail never gets one, even with the sibling present
            self.assertIsNone(G.suggest_verify(title, "", "inbound-mail"))
            # sibling ABSENT (same allowed host, so the only difference is the missing module): nothing
            real_here = G.HERE
            G.HERE = G.Path(TMP) / "lonely" / "groomer"
            try:
                self.assertIsNone(G._verify_module())
                self.assertIsNone(G.suggest_verify(title, "", "other"))
            finally:
                G.HERE = real_here
            # host NOT allowed: sibling present but no line
            with open(hosts, "w") as f:
                f.write("other.example\n")
            self.assertIsNone(G.suggest_verify(title, "", "other"))
        finally:
            del os.environ["VERIFY_HOSTS"]

    def test_never_fold_guards(self):
        t = "ALARM disk full on build host nightly"
        for kw in ({"grade": 9}, {"grade": 10}, {"grade": "9"}, {"grade": "high"}, {"own_ticket": True}, {"gate_note": True},
                   {"origin": "L-0005"}, {"asked_by": "operator"}, {"asked_by": "Operator"}):
            self.assertNotIn("fold_to", G.groom(t, "alarm", items=ITEMS, **kw), kw)
        for kw in ({}, {"grade": None}, {"grade": 8}, {"grade": "5"}, {"asked_by": "script"}):
            self.assertEqual(G.groom(t, "alarm", items=ITEMS, **kw)["fold_to"], "L-0001", kw)

    def test_candidate_asked_by_a_person_is_never_a_fold_target(self):
        items = [{"id": "L-0001", "title": "ALARM disk full on build host nightly", "status": "open", "source": "alarm", "asked_by": "operator"}]
        self.assertEqual(G.find_similar(items, "ALARM disk full on build host nightly", only_machine=True), [])
        self.assertNotIn("fold_to", G.groom("ALARM disk full on build host nightly", "alarm", items=items))

    def test_cli_guard_flags(self):
        import subprocess, sys
        env = dict(os.environ, GROOM_STATE_DIR=STATE, HUBKIT_HOME=os.path.join(TMP, "none"))
        r = subprocess.run([sys.executable, "-I", os.path.join(HERE, "groomer.py"), "groom", "--title", "x y z", "--source", "alarm",
                            "--grade", "9", "--gate-note", "--origin", "L-0001", "--asked-by", "operator", "--own-ticket"],
                           capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_similarity_threshold_and_title_overlap(self):
        items = [{"id": "L-0001", "title": "disk full on build host nightly", "status": "open", "source": "alarm"}]
        self.assertEqual(G.find_similar(items, "disk full on build server weekly"), [])          # 4 of 8 words: not a repeat
        self.assertEqual(G.find_similar(items, "disk full on build host nightly")[0][0], "L-0001")
        self.assertEqual(G.FOLD_THRESHOLD, 0.7)

    def test_shadow_counts_a_fold_but_changes_nothing(self):
        flag("groom-shadow")
        r = G.groom("ALARM disk full on build host nightly", "alarm", items=ITEMS)
        self.assertEqual(r, {"mode": "shadow"})
        with open(os.path.join(STATE, G.EVENTS_NAME)) as fh:
            self.assertIn("groom_fold", fh.read())

    def test_suggested_grade_table(self):
        self.assertEqual([G.groom("a b c", s)["grade"] for s in ("alarm", "operator-chat", "worker-followup", "nonsense")], [8, 7, 5, 5])

    def test_similar_cli_malformed_items_is_a_clean_error(self):
        import subprocess, sys
        bad = os.path.join(TMP, "bad.json")
        for body in ('{"id": "L-1", "ti', "not json at all", "", "5", '[1, 2]', '"text"'):
            with open(bad, "w") as f:
                f.write(body)
            r = subprocess.run([sys.executable, "-I", os.path.join(HERE, "groomer.py"), "similar", "--title", "disk full on build host", "--items", bad],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 2, body)
            self.assertNotIn("Traceback", r.stderr, body)
            self.assertIn("groomer:", r.stderr, body)
        r = subprocess.run([sys.executable, "-I", os.path.join(HERE, "groomer.py"), "similar", "--title", "disk full", "--items", os.path.join(TMP, "nope.json")],
                           capture_output=True, text=True)
        self.assertEqual((r.returncode, "Traceback" in r.stderr), (2, False))


if __name__ == "__main__":
    unittest.main()
