#!/usr/bin/env python3
"""Tests for the ledger pattern. Run: bash test.sh  (or python3 test_ledger.py -v).

Pure-function tests use synthetic records; CLI and hook tests run in a scratch
directory with LEDGER_PATH pointed at it, so no real ledger is ever touched.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import ledger_lib  # noqa: E402


def make_rec(**overrides) -> dict:
    rec = {
        "id": "L-0001", "title": "a test item", "status": "open",
        "asked_on": None, "asked_by": "hub", "owner": "unowned",
        "trigger": "", "project": None, "proof": [], "last_surfaced": None,
        "operator_ack": False, "created_at": None, "updated_at": None,
        "notes": [], "schema_issues": [], "reviewed_upto": 0,
    }
    rec.update(overrides)
    return rec


def hours_ago(h: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ")


class NeedsYouTests(unittest.TestCase):
    """Finished work is never a decision: only needs-operator needs the human."""

    def test_needs_operator_is_needs_you(self):
        self.assertTrue(ledger_lib.needs_you(make_rec(status="needs-operator")))

    def test_done_operator_ask_without_ack_is_not_needs_you(self):
        self.assertFalse(ledger_lib.needs_you(make_rec(status="done", asked_by="operator")))

    def test_other_statuses_are_not_needs_you(self):
        for status in ("open", "active", "dropped", "done"):
            self.assertFalse(ledger_lib.needs_you(make_rec(status=status)), status)

    def test_banner_counts(self):
        records = {
            "L-0001": make_rec(id="L-0001", status="needs-operator"),
            "L-0002": make_rec(id="L-0002", status="done", asked_by="operator"),
            "L-0003": make_rec(id="L-0003", status="active"),
        }
        lines = ledger_lib.banner_lines(records)
        self.assertEqual(lines[0], "Open: 2 · Needs you: 1")
        self.assertEqual(lines[1], "WORKING: 1")

    def test_banner_idle_when_nothing_active(self):
        self.assertEqual(ledger_lib.banner_lines({})[1], "IDLE")


class SurfaceTests(unittest.TestCase):
    def test_done_item_never_surfaces(self):
        records = {"L-0001": make_rec(status="done", asked_by="operator")}
        self.assertEqual(ledger_lib.surface_lines(records), [])

    def test_needs_operator_surfaces_first(self):
        records = {
            "L-0002": make_rec(id="L-0002", status="open", updated_at=hours_ago(100)),
            "L-0001": make_rec(id="L-0001", status="needs-operator", title="pick a color"),
        }
        lines = ledger_lib.surface_lines(records)
        self.assertTrue(lines[0].startswith("NEEDS-OPERATOR L-0001"))
        self.assertTrue(lines[1].startswith("STALE 48h+ L-0002"))

    def test_fresh_open_item_does_not_surface(self):
        records = {"L-0001": make_rec(status="open", updated_at=ledger_lib.now_iso())}
        self.assertEqual(ledger_lib.surface_lines(records), [])

    def test_never_started_operator_ask_surfaces(self):
        records = {"L-0001": make_rec(asked_by="operator", asked_on="2026-01-01",
                                      updated_at=ledger_lib.now_iso())}
        self.assertTrue(ledger_lib.surface_lines(records)[0].startswith("NEVER-STARTED L-0001"))

    def test_limit_is_respected(self):
        records = {f"L-{i:04d}": make_rec(id=f"L-{i:04d}", status="needs-operator") for i in range(1, 9)}
        self.assertEqual(len(ledger_lib.surface_lines(records)), ledger_lib.MAX_SURFACE)

    def test_stale_boundary(self):
        self.assertTrue(ledger_lib.is_stale(make_rec(updated_at=hours_ago(49))))
        self.assertFalse(ledger_lib.is_stale(make_rec(updated_at=hours_ago(47))))
        self.assertFalse(ledger_lib.is_stale(make_rec(status="done", updated_at=hours_ago(500))))


class OwnerTests(unittest.TestCase):
    def test_owner_event_does_not_touch_status(self):
        events = [
            {"ts": "2026-01-01T00:00:00Z", "id": "L-0001", "event": "created", "title": "x", "owner": "unowned"},
            {"ts": "2026-01-01T00:05:00Z", "id": "L-0001", "event": "status", "status": "active"},
            {"ts": "2026-01-01T00:10:00Z", "id": "L-0001", "event": "owner", "owner": "worker-a"},
        ]
        rec = ledger_lib.fold(events)["L-0001"]
        self.assertEqual((rec["owner"], rec["status"]), ("worker-a", "active"))

    def test_owner_event_bumps_updated_at(self):
        events = [
            {"ts": "2026-01-01T00:00:00Z", "id": "L-0001", "event": "created", "title": "x"},
            {"ts": "2026-01-02T00:00:00Z", "id": "L-0001", "event": "owner", "owner": "someone"},
        ]
        self.assertEqual(ledger_lib.fold(events)["L-0001"]["updated_at"], "2026-01-02T00:00:00Z")

    def test_owner_event_requires_owner(self):
        ledger_lib.validate_event({"id": "L-0001", "event": "owner", "owner": "someone"})
        with self.assertRaises(ValueError):
            ledger_lib.validate_event({"id": "L-0001", "event": "owner", "owner": ""})


class DoneGateTests(unittest.TestCase):
    """What COUNTS as proof for the done gate."""

    def test_metric_claims_alone_do_not_count(self):
        for kind in ("metric-claim", "metric-claim-verdict"):
            self.assertFalse(ledger_lib.has_done_gate_proof(make_rec(proof=[{"type": kind, "ref": "r"}])), kind)

    def test_both_claim_types_together_do_not_count(self):
        rec = make_rec(proof=[{"type": "metric-claim", "ref": "a"}, {"type": "metric-claim-verdict", "ref": "b"}])
        self.assertFalse(ledger_lib.has_done_gate_proof(rec))

    def test_real_proof_counts(self):
        self.assertTrue(ledger_lib.has_done_gate_proof(make_rec(proof=[{"type": "commit", "ref": "abc123"}])))

    def test_real_proof_beside_a_claim_counts(self):
        rec = make_rec(proof=[{"type": "metric-claim", "ref": "a"}, {"type": "report", "ref": "r.md"}])
        self.assertTrue(ledger_lib.has_done_gate_proof(rec))

    def test_no_proof_does_not_count(self):
        self.assertFalse(ledger_lib.has_done_gate_proof(make_rec(proof=[])))

    def test_bare_string_proof_counts(self):
        self.assertTrue(ledger_lib.has_done_gate_proof(make_rec(proof=["a plain string"])))

    def test_fold_keeps_claims_visible(self):
        events = [
            {"ts": "2026-01-01T00:00:00Z", "id": "L-0001", "event": "created", "title": "x"},
            {"ts": "2026-01-01T00:01:00Z", "id": "L-0001", "event": "proof",
             "proof": {"type": "metric-claim", "ref": "r"}},
        ]
        self.assertEqual(len(ledger_lib.fold(events)["L-0001"]["proof"]), 1)


class FoldTests(unittest.TestCase):
    def test_reused_id_with_new_title_resets_and_flags(self):
        events = [
            {"ts": "2026-01-01T00:00:00Z", "id": "L-0001", "event": "created", "title": "first"},
            {"ts": "2026-01-01T00:01:00Z", "id": "L-0001", "event": "proof", "proof": {"type": "file", "ref": "a"}},
            {"ts": "2026-01-02T00:00:00Z", "id": "L-0001", "event": "created", "title": "second"},
        ]
        rec = ledger_lib.fold(events)["L-0001"]
        self.assertEqual(rec["title"], "second")
        self.assertEqual(rec["proof"], [], "old proof must not carry to the new task")
        self.assertEqual(len(rec["schema_issues"]), 1)

    def test_invalid_status_is_recorded_not_applied(self):
        events = [
            {"ts": "2026-01-01T00:00:00Z", "id": "L-0001", "event": "created", "title": "x"},
            {"ts": "2026-01-01T00:01:00Z", "id": "L-0001", "event": "status", "status": "bogus"},
        ]
        rec = ledger_lib.fold(events)["L-0001"]
        self.assertEqual(rec["status"], "open")
        self.assertEqual(rec["schema_issues"][0]["reason"], "invalid status")

    def test_status_aliases_fold(self):
        self.assertEqual(ledger_lib.normalize_status("in_progress"), "active")
        self.assertEqual(ledger_lib.normalize_status("WORKING"), "active")

    def test_unknown_event_is_flagged_and_does_not_advance_updated_at(self):
        events = [
            {"ts": "2026-01-01T00:00:00Z", "id": "L-0001", "event": "created", "title": "x"},
            {"ts": "2026-02-01T00:00:00Z", "id": "L-0001", "event": "mystery"},
        ]
        rec = ledger_lib.fold(events)["L-0001"]
        self.assertEqual(rec["updated_at"], "2026-01-01T00:00:00Z")
        self.assertEqual(rec["schema_issues"][0]["reason"], "unsupported event")

    def test_ack_default_and_explicit_false(self):
        base = {"ts": "2026-01-01T00:00:00Z", "id": "L-0001", "event": "created", "title": "x"}
        ack = {"ts": "2026-01-01T00:01:00Z", "id": "L-0001", "event": "ack"}
        self.assertTrue(ledger_lib.fold([base, ack])["L-0001"]["operator_ack"])
        self.assertFalse(ledger_lib.fold([base, {**ack, "operator_ack": False}])["L-0001"]["operator_ack"])

    def test_validate_event_rules(self):
        bad = [
            {"event": "created", "title": "x"},                       # no id
            {"id": "L-0001", "event": "created"},                     # no title
            {"id": "L-0001", "event": "status", "status": "nope"},
            {"id": "L-0001", "event": "proof"},
            {"id": "L-0001", "event": "note"},
            {"id": "L-0001", "event": "decision", "note": "x"},       # not a writable event
        ]
        for ev in bad:
            with self.assertRaises(ValueError, msg=str(ev)):
                ledger_lib.validate_event(ev)


class ReviewCountTests(unittest.TestCase):
    """The banner's "needs review" count ignores settled items and honours a
    `reviewed` event, while fold() keeps every issue on record."""

    def ev(self, day, **kw):
        return {"ts": f"2026-09-0{day}T00:00:00Z", "id": "L-0001", **kw}

    def history(self, *tail):
        # an unsupported event always records one schema issue
        return [self.ev(1, event="created", title="t", asked_by="hub"), self.ev(2, event="mystery"), *tail]

    def count(self, recs):
        return [x for x in ledger_lib.banner_lines(recs) if "needs review" in x]

    def test_open_item_with_issue_is_counted(self):
        recs = ledger_lib.fold(self.history())
        self.assertEqual(self.count(recs), ["Ledger history needs review: 1 IDs; counts are provisional"])

    def test_done_and_dropped_are_not_counted_but_issues_kept(self):
        for status in ("done", "dropped"):
            recs = ledger_lib.fold(self.history(self.ev(3, event="status", status=status)))
            self.assertEqual(len(recs["L-0001"]["schema_issues"]), 1)
            self.assertEqual(self.count(recs), [])

    def test_reviewed_clears_open_item(self):
        recs = ledger_lib.fold(self.history(self.ev(3, event="reviewed", note="settled")))
        rec = recs["L-0001"]
        self.assertEqual(len(rec["schema_issues"]), 1)
        self.assertEqual(self.count(recs), [])
        self.assertIn("settled", [n["text"] for n in rec["notes"]])

    def test_later_bad_event_reflags(self):
        recs = ledger_lib.fold(self.history(self.ev(3, event="reviewed", note="ok"), self.ev(4, event="mystery")))
        self.assertEqual(len(ledger_lib.unreviewed_issues(recs["L-0001"])), 1)

    def test_same_timestamp_bad_event_after_review_reflags(self):
        events = self.history(self.ev(3, event="reviewed", note="ok"))
        events.append(self.ev(3, event="mystery"))
        self.assertEqual(len(ledger_lib.unreviewed_issues(ledger_lib.fold(events)["L-0001"])), 1)

    def test_reviewed_does_not_advance_updated_at(self):
        rec = ledger_lib.fold(self.history(self.ev(5, event="reviewed", note="ok")))["L-0001"]
        self.assertEqual(rec["updated_at"], "2026-09-01T00:00:00Z")


class CliTests(unittest.TestCase):
    """End-to-end through the real command, in a scratch directory."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state" / "ledger.jsonl"
        env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_PID", "CLAUDE_CODE_SESSION_ID")}
        self.env = {**env, "LEDGER_PATH": str(self.path), "HOME": self.tmp.name}

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *a):
        return subprocess.run([sys.executable, str(HERE / "ledger"), *a],
                              env=self.env, capture_output=True, text=True)

    def test_beginner_flow_cannot_close_without_proof(self):
        r = self.run_cli("add", "make a hello file")
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "L-0001"))
        self.assertEqual(self.run_cli("status", "L-0001", "active").returncode, 0)
        refused = self.run_cli("status", "L-0001", "done")
        self.assertEqual(refused.returncode, 1)
        self.assertIn("no proof recorded yet", refused.stderr)
        self.assertIn("[active]", self.run_cli("list").stdout)
        self.assertEqual(self.run_cli("proof", "L-0001", "file", "./hello.txt").returncode, 0)
        self.assertEqual(self.run_cli("status", "L-0001", "done").returncode, 0)
        self.assertIn("[done] proof:1", self.run_cli("list").stdout)

    def test_metric_claim_alone_refuses_done(self):
        self.run_cli("add", "x")
        self.run_cli("proof", "L-0001", "metric-claim", "baseline")
        r = self.run_cli("status", "L-0001", "done")
        self.assertEqual(r.returncode, 1)
        self.assertIn("a claim is not proof", r.stderr)

    def test_unknown_id_fails_on_every_command(self):
        for args in (("status", "L-0010", "done"), ("proof", "L-0010", "file", "x"), ("ack", "L-0010"),
                     ("claim", "L-0010", "w"), ("release", "L-0010"), ("note", "L-0010", "t")):
            r = self.run_cli(*args)
            self.assertEqual(r.returncode, 1, args)
            self.assertIn("unknown id", r.stderr)

    def test_ids_are_not_reused_after_the_file_is_rewound(self):
        self.run_cli("add", "one")
        self.run_cli("add", "two")
        self.path.write_text("")  # a checkout that rewinds the file
        self.assertEqual(self.run_cli("add", "three").stdout.strip(), "L-0003")

    def test_long_title_is_shortened(self):
        self.run_cli("add", "x" * 100)
        title = json.loads(self.path.read_text().splitlines()[0])["title"]
        self.assertEqual((len(title), title[-3:]), (60, "..."))

    def test_owner_verbs_leave_status_alone(self):
        self.run_cli("add", "x")
        self.run_cli("claim", "L-0001", "worker-a")
        self.assertIn("owner: worker-a", self.run_cli("list").stdout)
        self.run_cli("release", "L-0001")
        self.assertIn("owner: unowned", self.run_cli("list").stdout)
        self.run_cli("reassign", "L-0001", "worker-b")
        out = self.run_cli("list", "--json").stdout
        rec = json.loads(out)[0]
        self.assertEqual((rec["owner"], rec["status"]), ("worker-b", "open"))

    def test_claim_records_who_inside_claude_code(self):
        self.run_cli("add", "x")
        self.env["CLAUDE_PID"] = "4242"
        self.env["CLAUDE_CODE_SESSION_ID"] = "sess-1"
        self.run_cli("status", "L-0001", "active")
        last = json.loads(self.path.read_text().splitlines()[-1])
        self.assertEqual(last["claimed_by"], {"pid": 4242, "session_id": "sess-1"})

    def test_claim_has_no_actor_outside_claude_code(self):
        self.run_cli("add", "x")
        self.run_cli("status", "L-0001", "active")
        self.assertNotIn("claimed_by", json.loads(self.path.read_text().splitlines()[-1]))

    def test_banner_surface_and_note(self):
        self.assertEqual(self.run_cli("surface").stdout.strip(), "(nothing to surface)")
        self.assertEqual(self.run_cli("list").stdout.strip(), "(no items)")
        self.run_cli("add", "pick a name")
        self.run_cli("status", "L-0001", "needs-operator")
        self.assertIn("Needs you: 1", self.run_cli("banner").stdout)
        self.assertIn("NEEDS-OPERATOR L-0001", self.run_cli("surface").stdout)
        self.assertEqual(self.run_cli("note", "L-0001", "asked in chat").returncode, 0)

    def test_corrupt_file_is_refused_not_miscounted(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("not json\n")
        r = self.run_cli("list")
        self.assertEqual(r.returncode, 1)
        self.assertIn("Malformed", r.stderr)

    def test_reviewed_round_trip(self):
        self.path.parent.mkdir(parents=True)
        events = [
            {"ts": "2026-09-01T00:00:00Z", "id": "L-0001", "event": "created", "title": "t", "asked_by": "hub"},
            {"ts": "2026-09-02T00:00:00Z", "id": "L-0001", "event": "mystery"},
        ]
        self.path.write_text("".join(json.dumps(e) + "\n" for e in events))
        self.assertNotEqual(self.run_cli("reviewed", "L-0001").returncode, 0, "--note is required")
        self.assertIn("needs review: 1", self.run_cli("banner").stdout)
        self.assertEqual(self.run_cli("reviewed", "L-0001", "--note", "settled").returncode, 0)
        self.assertNotIn("needs review", self.run_cli("banner").stdout)
        self.assertNotEqual(self.run_cli("reviewed", "L-0001", "--note", "again").returncode, 0)

    def test_new_item_records_who_filed_it(self):
        self.env["LEDGER_FILER"] = "nightly:sweep"
        self.env["LEDGER_FILER_WORKER"] = "builder"
        self.run_cli("add", "a")
        self.env["LEDGER_FILER"] = "re_" + "x" * 30        # key-shaped: redacted, never stored
        self.env.pop("LEDGER_FILER_WORKER")
        self.env.pop("CLAUDECODE", None)
        self.run_cli("add", "b")
        self.env["LEDGER_FILER"] = "a b;$(rm -rf x)"       # hostile spelling: cleaned to plain characters
        self.run_cli("add", "c")
        rows = [json.loads(x) for x in self.path.read_text().splitlines()]
        got = [r["filed_by"] for r in rows if r["event"] == "created"]
        self.assertEqual(got[0], {"script": "nightly:sweep", "worker": "builder"})
        self.assertEqual(got[1], {"script": "redacted", "worker": "script"})
        self.assertEqual(got[2]["script"], "abrm-rfx")
        recs = ledger_lib.fold(rows)
        self.assertEqual(recs["L-0001"]["filed_by"]["worker"], "builder")

    def test_help_is_beginner_readable(self):
        r = self.run_cli("--help")
        self.assertEqual(r.returncode, 0)
        for needle in ("YOUR FIRST FIVE MINUTES", "THE RULE THAT MATTERS", "REFUSED: no proof yet", "STATUSES"):
            self.assertIn(needle, r.stdout)
        self.assertEqual(self.run_cli().returncode, 0)  # bare `ledger` prints help


class ProjectEventTests(unittest.TestCase):
    def test_project_event_sets_project_without_touching_updated_at(self):
        events = [
            {"ts": "2026-01-01T00:00:00+00:00", "id": "L-0001", "event": "created", "title": "x"},
            {"ts": "2026-01-02T00:00:00+00:00", "id": "L-0001", "event": "project", "project": "site"},
        ]
        rec = ledger_lib.fold(events)["L-0001"]
        self.assertEqual(rec["project"], "site")
        self.assertEqual(rec["updated_at"], "2026-01-01T00:00:00+00:00")

    def test_project_event_needs_a_project(self):
        with self.assertRaises(ValueError):
            ledger_lib.validate_event({"id": "L-0001", "event": "project", "project": "  "})
        ledger_lib.validate_event({"id": "L-0001", "event": "project", "project": "site"})


class HookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "ledger.jsonl"
        self.env = {**os.environ, "LEDGER_PATH": str(self.path), "HUBKIT_NOW_ISO": "2026-09-23T17:22:00"}

    def tearDown(self):
        self.tmp.cleanup()

    def run_hook(self):
        return subprocess.run([sys.executable, str(HERE / "ledger-banner-hook.py")], input="{}",
                              env=self.env, capture_output=True, text=True)

    def test_clock_only_when_no_ledger(self):
        r = self.run_hook()
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip().splitlines(),
                         ["<ledger-status>", "Now: 5:22pm, Wed 9/23", "</ledger-status>"])

    def test_banner_and_surface_lines(self):
        events = [
            {"ts": ledger_lib.now_iso(), "id": "L-0001", "event": "created", "title": "pick a name"},
            {"ts": ledger_lib.now_iso(), "id": "L-0001", "event": "status", "status": "needs-operator"},
        ]
        self.path.write_text("".join(json.dumps(e) + "\n" for e in events))
        out = self.run_hook().stdout
        self.assertIn("Open: 1 · Needs you: 1", out)
        self.assertIn("  · NEEDS-OPERATOR L-0001: pick a name", out)

    def test_corrupt_ledger_fails_open(self):
        self.path.write_text("garbage\n")
        r = self.run_hook()
        self.assertEqual(r.returncode, 0)
        self.assertIn("Now:", r.stdout)
        self.assertNotIn("Open:", r.stdout)

    def _extras(self, body):
        p = Path(self.tmp.name) / "extras.py"
        p.write_text(body)
        self.env["HUBKIT_BANNER_EXTRAS"] = str(p)

    def test_extras_lines_print_with_payload_fields(self):
        self._extras("def prompt_lines(sid, cwd):\n    return ['IDLE FLOOR: ' + str(sid) + ' ' + str(cwd)]\n")
        r = subprocess.run([sys.executable, str(HERE / "ledger-banner-hook.py")],
                           input=json.dumps({"session_id": "s1", "cwd": "/w"}),
                           env=self.env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip().splitlines(),
                         ["<ledger-status>", "Now: 5:22pm, Wed 9/23", "IDLE FLOOR: s1 /w", "</ledger-status>"])

    def test_extras_failures_add_nothing(self):
        for body in ("raise RuntimeError('x')\n",
                     "def prompt_lines(sid, cwd):\n    raise ValueError('x')\n",
                     "import sys\ndef prompt_lines(sid, cwd):\n    sys.exit(2)\n",
                     "def prompt_lines(sid, cwd):\n    return 'not a list'\n",
                     "x = (\n"):
            self._extras(body)
            r = self.run_hook()
            self.assertEqual(r.returncode, 0, body)
            self.assertEqual(r.stdout.strip().splitlines(),
                             ["<ledger-status>", "Now: 5:22pm, Wed 9/23", "</ledger-status>"], body)

    def test_extras_default_file_next_to_hook(self):
        d = Path(self.tmp.name) / "kit"
        d.mkdir()
        for f in ("ledger-banner-hook.py", "ledger_lib.py"):
            (d / f).write_text((HERE / f).read_text())
        (d / "banner_extras.py").write_text("def prompt_lines(sid, cwd):\n    return ['beside the hook']\n")
        self.env.pop("HUBKIT_BANNER_EXTRAS", None)
        r = subprocess.run([sys.executable, str(d / "ledger-banner-hook.py")], input="{}",
                           env=self.env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip().splitlines(),
                         ["<ledger-status>", "Now: 5:22pm, Wed 9/23", "beside the hook", "</ledger-status>"])

    def test_extras_missing_file_and_bad_payload(self):
        self.env["HUBKIT_BANNER_EXTRAS"] = str(Path(self.tmp.name) / "nope.py")
        for stdin in ("", "not json", "[1, 2]", "{}"):
            r = subprocess.run([sys.executable, str(HERE / "ledger-banner-hook.py")], input=stdin,
                               env=self.env, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, stdin)
            self.assertIn("Now: 5:22pm", r.stdout, stdin)

    def test_midnight_and_noon_format(self):
        for iso, want in (("2026-01-05T00:05:00", "Now: 12:05am, Mon 1/5"), ("2026-01-05T12:00:00", "Now: 12:00pm, Mon 1/5")):
            self.env["HUBKIT_NOW_ISO"] = iso
            self.assertIn(want, self.run_hook().stdout)


class KeyShapedRefusalTests(unittest.TestCase):
    """Key-shaped text is refused at write time, in any field, without echoing it."""

    def _fake(self):
        return "re_" + "A1b2C3d4" * 4  # built at runtime so no key-shaped literal sits in the repo

    def test_title_note_and_proof_refused(self):
        for ev in ({"id": "L-9", "event": "created", "title": "fix " + self._fake()},
                   {"id": "L-9", "event": "note", "note": self._fake()},
                   {"id": "L-9", "event": "proof", "proof": "commit x " + self._fake()}):
            with self.assertRaises(ValueError) as cm:
                ledger_lib.validate_event(ev)
            self.assertNotIn(self._fake(), str(cm.exception))
            self.assertIn("looks like a secret", str(cm.exception))

    def test_other_key_families_refused(self):
        for v in ("sk-" + "a" * 24, "ghp_" + "a" * 36, "AKIA" + "A" * 16,
                  "-----BEGIN " + "RSA PRIVATE KEY-----"):
            with self.assertRaises(ValueError):
                ledger_lib.validate_event({"id": "L-9", "event": "note", "note": v})

    def test_normal_text_still_allowed(self):
        for v in ("resend_failed retry", "re_run the nightly", "redact the title", "sk-learn is fine"):
            ledger_lib.validate_event({"id": "L-9", "event": "note", "note": v})


FIXTURE_LINES = (
    '{"ts": "2026-01-01T00:00:01Z", "id": "L-0001", "event": "created", "title": "sample item one"}',
    '{"ts": "2026-01-01T00:00:02Z", "id": "L-0001", "event": "triage", "class": "urgent", "size": "S"}',
    '{"ts": "2026-01-01T00:00:03Z", "id": "L-0002", "event": "created", "title": "sample item two"}',
    '{"ts": "2026-01-01T00:00:04Z", "id": "L-0002", "event": "triage", "class": "Important", "size": "M"}',
    '{"ts": "2026-01-01T00:00:05Z", "id": "L-0003", "event": "created", "title": "sample item three"}',
    '{"ts": "2026-01-01T00:00:06Z", "id": "L-0003", "event": "triage", "class": "low_hanging"}',
    '{"ts": "2026-01-01T00:00:07Z", "id": "L-0004", "event": "created", "title": "sample item four"}',
    '{"ts": "2026-01-01T00:00:08Z", "id": "L-0004", "event": "triage", "class": "WAIT"}',
    '{"ts": "2026-01-01T00:00:09Z", "id": "L-0005", "event": "created", "title": "sample item five, never triaged"}',
    '{"ts": "2026-01-01T00:00:10Z", "id": "L-0006", "event": "created", "title": "sample item six"}',
    '{"ts": "2026-01-01T00:00:11Z", "id": "L-0006", "event": "triage", "class": "urgent"}',
    '{"ts": "2026-01-01T00:00:12Z", "id": "L-0006", "event": "proof", "proof": {"type": "commit", "ref": "abc1234"}}',
    '{"ts": "2026-01-01T00:00:13Z", "id": "L-0006", "event": "status", "status": "done", "lesson": "sample one-line lesson"}',
)
FIXTURE_TEXT = "\n".join(FIXTURE_LINES) + "\n"


def fixture_rows() -> list[dict]:
    return [json.loads(l) for l in FIXTURE_LINES]


class LawMixTests(unittest.TestCase):
    """Law-mix counts per class, kit-generic fields only."""

    def test_fixture_rows_are_kit_generic(self):
        rows = fixture_rows()
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(ledger_lib.non_generic_fields(row), [], row)

    def test_non_generic_field_is_detected(self):
        for bad in ("lane", "card_id", "site", "operator_name"):
            row = {"id": "L-0001", "event": "created", "title": "x", bad: "v"}
            self.assertEqual(ledger_lib.non_generic_fields(row), [bad])

    def test_mix_counts_from_fixture(self):
        mix = ledger_lib.mix_counts(ledger_lib.fold(fixture_rows()))
        self.assertEqual(mix, {"urgent": 2, "important": 1, "low-hanging": 1, "unclassified": 2})

    def test_fold_carries_size_and_lesson(self):
        recs = ledger_lib.fold(fixture_rows())
        self.assertEqual(recs["L-0001"]["size"], "S")
        self.assertEqual(recs["L-0006"]["lesson"], "sample one-line lesson")

    def test_empty_and_odd_input_do_not_crash(self):
        self.assertEqual(sum(ledger_lib.mix_counts({}).values()), 0)
        odd = {"L-1": make_rec(id="L-1", triage_class=None), "L-2": make_rec(id="L-2", triage_class=7),
               "L-3": make_rec(id="L-3", triage_class=["urgent"]), "L-4": make_rec(id="L-4", triage_class=""),
               "L-5": make_rec(id="L-5")}
        self.assertEqual(ledger_lib.mix_counts(odd)["unclassified"], 5)

    def test_cli_mix_empty_and_fixture(self):
        with tempfile.TemporaryDirectory() as d:
            env = dict(os.environ, LEDGER_PATH=str(Path(d) / "l.jsonl"))
            run = lambda *a: subprocess.run([sys.executable, str(HERE / "ledger"), *a], env=env, capture_output=True, text=True)
            r = run("mix")
            self.assertEqual((r.returncode, r.stdout.strip()), (0, "Mix: urgent 0 · important 0 · low-hanging 0 · unclassified 0"))
            Path(env["LEDGER_PATH"]).write_text(FIXTURE_TEXT)
            self.assertEqual(json.loads(run("mix", "--json").stdout)["mix"]["urgent"], 2)
            self.assertEqual(run("triage", "L-0005", "important", "--size", "L").returncode, 0)
            self.assertEqual(json.loads(run("mix", "--json").stdout)["mix"]["important"], 2)
            self.assertNotEqual(run("triage", "L-8", "urgent").returncode, 0)
            self.assertEqual(run("banner").returncode, 0)  # existing command unchanged

    def test_last_triage_wins_on_retriage(self):
        rows = fixture_rows() + [
            {"ts": "2026-01-01T00:01:00Z", "id": "L-0001", "event": "triage", "class": "important"}]
        recs = ledger_lib.fold(rows)
        self.assertEqual(recs["L-0001"]["triage_class"], "important")
        self.assertEqual(ledger_lib.mix_counts(recs),
                         {"urgent": 1, "important": 2, "low-hanging": 1, "unclassified": 2})

    def test_triage_event_requires_a_class(self):
        for bad in ({}, {"class": ""}, {"class": None}):
            with self.assertRaises(ValueError):
                ledger_lib.validate_event({"id": "L-8", "event": "triage", **bad})
        with tempfile.TemporaryDirectory() as d:
            env = dict(os.environ, LEDGER_PATH=str(Path(d) / "l.jsonl"))
            Path(env["LEDGER_PATH"]).write_text(FIXTURE_TEXT)
            r = subprocess.run([sys.executable, str(HERE / "ledger"), "triage", "L-0005", ""],
                               env=env, capture_output=True, text=True)
            self.assertNotEqual(r.returncode, 0)
            self.assertEqual(Path(env["LEDGER_PATH"]).read_text(), FIXTURE_TEXT)


if __name__ == "__main__":
    unittest.main()
