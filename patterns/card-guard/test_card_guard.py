#!/usr/bin/env python3
"""Tests for card_guard.py and worker_card.py. Run: python3 test_card_guard.py"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import card_guard as G  # noqa: E402

TMP = tempfile.mkdtemp()
OPTS = [{"label": "Ship it", "description": "Puts the change live today"}, {"label": "Wait", "description": "Keeps the old page for now"}]
CTX = "Should the new page go live?\nProof: the page loads in a test run\nWhy: it saves a week of waiting because the review is done"


def card(*args, env=None):
    e = dict(os.environ, HUBKIT_HOME=TMP, WORKER_CARD_STATE=os.path.join(TMP, "wc"), WORKER_CARD_RUN="t1")
    e.update(env or {})
    return subprocess.run([sys.executable, os.path.join(HERE, "worker_card.py")] + list(args), capture_output=True, text=True, env=e)


def good(ticket="L-0001", header="Go live today?", extra=()):
    return ["--ticket", ticket, "--header", header, "--why", "it saves a week because the review is done",
            "--option", "Yes, ship|Puts the change live today", "--option", "Wait|Keeps the old page", "--recommended", "0", "--proof", "test run passed"] + list(extra)


class GuardTests(unittest.TestCase):
    def test_good_card_passes(self):
        self.assertIsNone(G.check("Go live today?", CTX, OPTS, require_why=True, require_proof=True))

    def test_private_looking_text_refused_in_each_place(self):
        bad = ["see L-0008", "commit abc1234def", "card q-abcd12", "the slice is done", "on main", "ALARM disk", "k=deadbeef12",
               "at 2030-01-05 10:30", "[REDACTED x]", "[" + "[marker]]", "cut off...", "run groom-check.py", "[my-script] ran", '{"a": 1}']
        for t in bad:
            self.assertIsNotNone(G.check("Go?", "Context here. " + t if not t.startswith(("ALARM", "{", "[[")) else t, OPTS), t)
            self.assertIsNotNone(G.check("Go? " + t if not t.startswith(("ALARM", "{")) else t, "Fine context", OPTS), "h " + t)
            o = [{"label": "A", "description": "does " + t}, OPTS[1]]
            if not t.startswith(("ALARM", "{")):
                self.assertIsNotNone(G.check("Go?", "Fine context", o), "o " + t)

    def test_empty_and_long_header_and_few_options(self):
        self.assertEqual(G.check("", "c", OPTS), "empty header")
        self.assertEqual(G.check("h", "", OPTS), "empty context")
        self.assertIn("100", G.check("x" * 101, "c", OPTS))
        self.assertEqual(G.check("h", "c", OPTS[:1]), "needs at least 2 options")
        self.assertIsNotNone(G.check("h", "c", None))

    def test_option_without_description_refused(self):
        self.assertIn("plain description", G.check("h", "c", [{"label": "A", "description": ""}, OPTS[1]]))

    def test_long_option_description_is_not_refused(self):
        o = [{"label": "A", "description": "word " * 200}, OPTS[1]]
        self.assertIsNone(G.check("h", "c", o))

    def test_generic_yes_no_refused(self):
        o = [{"label": "Yes, go ahead", "description": "d"}, {"label": "No, hold off", "description": "d"}]
        self.assertIn("generic", G.check("h", "c", o))

    def test_why_rules(self):
        self.assertIn("Why:", G.check("h", "Proof: x", OPTS, require_why=True))
        self.assertIn("empty", G.check("h", "Proof: x\nWhy:   ", OPTS, require_why=True))
        self.assertIn("consequence", G.check("h", "Proof: x\nWhy: speed", OPTS, require_why=True))
        self.assertIn("consequence", G.check("h", "Proof: x\nWhy: because", OPTS, require_why=True))   # nothing after the cue word
        self.assertIsNone(G.check("h", "Proof: x\nWhy: it prevents double work", OPTS, require_why=True))

    def test_proof_rules(self):
        self.assertIn("Proof:", G.check("h", "Why: it saves time", OPTS, require_proof=True))
        self.assertIn("Proof:", G.check("h", "Proof:   \nWhy: it saves time", OPTS, require_proof=True))
        self.assertIsNone(G.check("h", "Proof: a link to the run\nWhy: it saves time", OPTS, require_proof=True))
        self.assertIn("placeholder", G.check("h", "Proof: TBD\nWhy: it saves time", OPTS, require_proof=True))
        self.assertIn("short", G.check("h", "Proof: a link\nWhy: it saves time", OPTS, require_proof=True))
        self.assertIn("longer", G.check("h", "Proof: " + "x" * 601 + "\nWhy: it saves time", OPTS, require_proof=True))


class WorkerCardTests(unittest.TestCase):
    def setUp(self):
        for p in ("cards.jsonl", "answers.jsonl"):
            f = os.path.join(TMP, "state", p)
            if os.path.exists(f):
                os.remove(f)
        wc = os.path.join(TMP, "wc")
        if os.path.isdir(wc):
            for f in os.listdir(wc):
                os.remove(os.path.join(wc, f))

    def queue(self):
        p = os.path.join(TMP, "state", "cards.jsonl")
        if not os.path.exists(p):
            return []
        with open(p) as fh:
            return [json.loads(l) for l in fh]

    def test_files_a_card_with_recommended_first(self):
        a = good(extra=())
        a[a.index("--recommended") + 1] = "1"
        r = card(*a)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Card: wc-", r.stdout)
        q = self.queue()
        self.assertEqual(q[0]["options"][0]["label"], "Wait")
        self.assertIn("\nProof: test run passed\nWhy: ", q[0]["context"])

    def test_retry_is_free_and_posts_once(self):
        self.assertEqual(card(*good()).returncode, 0)
        r = card(*good())
        self.assertEqual(r.returncode, 0)
        self.assertIn("already filed", r.stdout)
        self.assertEqual(len(self.queue()), 1)

    def test_third_card_is_capped(self):
        self.assertEqual(card(*good(header="First question here?")).returncode, 0)
        self.assertEqual(card(*good(header="Second different ask?")).returncode, 0)
        r = card(*good(header="Third other thing?"))
        self.assertEqual(r.returncode, 1)
        self.assertIn("cap reached", r.stderr)
        self.assertEqual(len(self.queue()), 2)

    def test_answered_decision_not_reasked(self):
        os.makedirs(os.path.join(TMP, "state"), exist_ok=True)
        with open(os.path.join(TMP, "state", "answers.jsonl"), "w") as f:
            f.write("garbage line\n" + json.dumps({"header": "Go live today?", "answered_at": "2030-01-01"}) + "\n")
        r = card(*good(header="Go live now today?"))
        self.assertEqual(r.returncode, 1)
        self.assertIn("already answered", r.stderr)

    def test_hostile_inputs_refused_and_not_echoed(self):
        cases = [good(header="café time?"), good(header="line\nbreak?"), good(header="x" * 101),
                 good(ticket="not-a-ticket"), good(ticket="L-1; rm -rf /"),
                 ["--ticket", "L-0001", "--header", "Q?", "--why", "saves time", "--option", "only one|d", "--recommended", "0"],
                 ["--ticket", "L-0001", "--header", "Q?", "--why", "saves time", "--option", "A|d", "--option", "B|d"],            # no --recommended
                 ["--ticket", "L-0001", "--header", "Q?", "--why", "saves time", "--option", "A|d", "--option", "B|d", "--recommended", "5"],
                 ["--ticket", "L-0001", "--header", "Q?", "--why", "saves time", "--option", "A|d", "--option", "Bnodesc", "--recommended", "0"],
                 good(header="See L-0008 now?"), good(header="Ship q-abcd12?")]
        for args in cases:
            r = card(*args)
            self.assertEqual(r.returncode, 1, args)
            self.assertNotIn("rm -rf", r.stderr)
        self.assertEqual(self.queue(), [])

    def test_missing_recommended_is_named(self):
        r = card("--ticket", "L-0001", "--header", "Q?", "--why", "saves time", "--option", "A|d", "--option", "B|d",
                 "--proof", "test run passed")
        self.assertEqual(r.returncode, 1)
        self.assertIn("no recommended option", r.stderr)

    def test_proof_required_and_placeholder_refused(self):
        base = ["--ticket", "L-0001", "--header", "Q?", "--why", "saves time", "--option", "A|d", "--option", "B|d", "--recommended", "0"]
        for proof in ([], ["--proof", "tbd"], ["--proof", "see my report"], ["--proof", "ok"]):
            r = card(*(base + proof))
            self.assertEqual(r.returncode, 1, proof)
            self.assertIn("roof", r.stderr)

    def test_why_naming_other_option_refused(self):
        r = card("--ticket", "L-0001", "--header", "Q?", "--why", "I recommend Wait because it saves time", "--option",
                 "Go|puts it live", "--option", "Wait|keeps the old page", "--recommended", "0", "--proof", "test run passed")
        self.assertEqual(r.returncode, 1)
        self.assertIn("different option", r.stderr)

    def test_recommend_problem_rules(self):
        o = [{"label": "Go"}, {"label": "Wait"}]
        self.assertIsNone(G.recommend_problem("Recommended: Go\nWhy: x", o, 0, require_line=True))
        self.assertIn("different", G.recommend_problem("Recommended: Wait", o, 0))
        self.assertIsNone(G.recommend_problem("Not recommended: Wait", o, 0))
        self.assertIn("missing", G.recommend_problem("x", o, None))
        self.assertIn("Recommended:", G.recommend_problem("Why: it saves time", o, 0, require_line=True))

    def test_why_without_consequence_refused(self):
        a = good()
        a[a.index("--why") + 1] = "speed"
        r = card(*a)
        self.assertEqual(r.returncode, 1)
        self.assertIn("consequence", r.stderr)

    def ledger(self, text):
        os.makedirs(os.path.join(TMP, "wc"), exist_ok=True)
        with open(os.path.join(TMP, "wc", "t1.jsonl"), "w") as f:
            f.write(text)

    def test_corrupt_cap_ledger_fails_closed_never_resets_the_cap(self):
        # cap reached with two real cards, then every kind of damaged line must keep refusing (probe W2/W3 of the gate)
        self.assertEqual(card(*good(header="First question here?")).returncode, 0)
        self.assertEqual(card(*good(header="Second different ask?")).returncode, 0)
        path = os.path.join(TMP, "wc", "t1.jsonl")
        with open(path) as fh:
            good_rows = fh.read()
        for bad in ('{"id": "wc-par', "garbage line\n", "[1, 2]\n", "123\n", "null\n", '{"id": "x", "t": NaN}\n', '{"id": "x", "t": "soon"}\n', '{"t": 1}\n'):
            with open(path, "w") as fh:
                fh.write(good_rows + bad)
            r = card(*good(header="Third other thing?"))
            self.assertEqual(r.returncode, 1, repr(bad))
            self.assertIn("cap ledger", r.stderr, repr(bad))
            self.assertEqual(len(self.queue()), 2, repr(bad))

    def test_partial_line_alone_refuses_instead_of_starting_from_zero(self):
        self.ledger('{"id": "wc-par')
        r = card(*good())
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.queue(), [])

    def test_missing_ledger_is_a_fresh_run_and_blank_lines_are_fine(self):
        self.assertEqual(card(*good()).returncode, 0)
        with open(os.path.join(TMP, "wc", "t1.jsonl"), "a") as fh:
            fh.write("\n\n")
        self.assertEqual(card(*good(header="Second different ask?")).returncode, 0)

    def test_unwritable_ledger_posts_no_card(self):
        os.makedirs(os.path.join(TMP, "wc", "t1.jsonl"), exist_ok=True)       # a folder where the ledger file should be
        try:
            r = card(*good())
            self.assertEqual(r.returncode, 1)
            self.assertEqual(self.queue(), [])
        finally:
            os.rmdir(os.path.join(TMP, "wc", "t1.jsonl"))

    def test_read_only_ledger_posts_no_card(self):
        if os.geteuid() == 0:
            self.skipTest("root ignores file permissions")
        self.ledger("")
        path = os.path.join(TMP, "wc", "t1.jsonl")
        os.chmod(path, 0o400)                                   # readable (zero rows) but cannot be appended to
        try:
            r = card(*good())
            self.assertEqual(r.returncode, 1)
            self.assertIn("cannot write the cap ledger", r.stderr)
            self.assertEqual(self.queue(), [])
        finally:
            os.chmod(path, 0o600)

    def test_cards_older_than_the_window_stop_counting(self):
        import time
        old = time.time() - 13 * 3600
        self.ledger("".join(json.dumps({"id": "wc-old%d" % i, "t": old}) + "\n" for i in range(2)))
        self.assertEqual(card(*good()).returncode, 0)
        self.ledger("".join(json.dumps({"id": "wc-new%d" % i, "t": time.time() - 3600}) + "\n" for i in range(2)))
        self.assertEqual(card(*good(header="Another one entirely?")).returncode, 1)

    def test_answer_rows_need_answered_at_and_match_case_and_spacing(self):
        os.makedirs(os.path.join(TMP, "state"), exist_ok=True)
        p = os.path.join(TMP, "state", "answers.jsonl")
        with open(p, "w") as f:
            f.write(json.dumps({"header": "Go live today?"}) + "\n")          # asked, never answered: does not block
        self.assertEqual(card(*good(header="Go live today?")).returncode, 0)
        with open(p, "w") as f:
            f.write(json.dumps({"header": "Is it on?", "answered_at": "2030-01-01"}) + "\n")   # only stop words: exact text path
        r = card(*good(ticket="L-0002", header="IS   it  ON?"))
        self.assertEqual(r.returncode, 1)
        self.assertIn("already answered", r.stderr)

    def test_size_and_count_limits(self):
        def run_with(**kw):
            a = good()
            for k, v in kw.items():
                if k == "opts":
                    while "--option" in a:
                        i = a.index("--option")
                        del a[i:i + 2]
                    for o in v:
                        a += ["--option", o]
                else:
                    a[a.index("--" + k) + 1] = v
            return card(*a)
        r = run_with(opts=["A|d", "B|d", "C|d", "D|d"])
        self.assertIn("2-3", r.stderr)
        self.assertIn("too long", run_with(opts=["L" * 61 + "|d", "B|d"]).stderr)
        self.assertIn("too long", run_with(opts=["A|" + "d" * 201, "B|d"]).stderr)
        self.assertIn("longer than 400", run_with(why="it saves a week because " + "x" * 400).stderr)
        self.assertIn("longer than 400", run_with(proof="p" * 401).stderr)
        big = ["L" * 60 + "|" + "d" * 200] * 3
        self.assertIn("too long", run_with(opts=big, why="it saves a week because " + "x" * 300, proof="p" * 390).stderr)
        self.assertEqual(self.queue(), [])


if __name__ == "__main__":
    unittest.main()
