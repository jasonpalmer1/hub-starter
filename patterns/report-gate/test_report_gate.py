#!/usr/bin/env python3
"""Tests for report-gate.py. Every case runs the hook as Claude Code would: JSON on
stdin, exit code and stderr out. All state lives in a scratch HOME."""
import datetime
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
GATE = os.environ.get("GATE", os.path.join(HERE, "report-gate.py"))


def slurp(path):
    with open(path) as f:
        return f.read()


def iso(epoch):
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).isoformat().replace("+00:00", "Z")


class GateCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.home = os.path.join(self.tmp, "home")
        self.hk = os.path.join(self.home, ".claude", "hubkit")
        self.reports = os.path.join(self.hk, "reports")
        os.makedirs(self.reports)
        self.sid = "sess-1"
        self.n = 0
        self.env = dict(os.environ, HOME=self.home, HUBKIT_HOME=self.hk,
                        REPORT_GATE_LOG_ALL="1", PYTHONDONTWRITEBYTECODE="1")
        self.env.pop("REPORT_GATE_PROJECTS_ROOT", None)

    def tearDown(self):
        for root, dirs, files in os.walk(self.tmp, topdown=False):
            for f in files:
                os.remove(os.path.join(root, f))
            for d in dirs:
                os.rmdir(os.path.join(root, d))
        os.rmdir(self.tmp)

    # -- helpers
    def worker(self, brief, started=None, agent_id=None):
        """Create a worker transcript with this brief; returns the agent id."""
        self.n += 1
        aid = agent_id or f"w{self.n}"
        d = os.path.join(self.home, ".claude", "projects", "proj", self.sid, "subagents")
        os.makedirs(d, exist_ok=True)
        ts = iso(started if started is not None else time.time() - 120)
        with open(os.path.join(d, f"agent-{aid}.jsonl"), "w") as f:
            f.write(json.dumps({"timestamp": ts, "message": {"content": brief}}) + "\n")
        return aid

    def stop(self, aid, agent_type="builder", raw=None):
        payload = raw if raw is not None else json.dumps(
            {"session_id": self.sid, "agent_id": aid, "agent_type": agent_type})
        r = subprocess.run([sys.executable, GATE], input=payload, capture_output=True,
                           text=True, env=self.env)
        return r.returncode, r.stderr

    def write(self, path, text="real content\n"):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)

    def log_text(self):
        p = os.path.join(self.hk, "state", "report-gate.log")
        return slurp(p) if os.path.exists(p) else ""

    # -- the proof gate
    def test_missing_report_blocks_every_time(self):
        aid = self.worker("Do it. Write your report to ~/.claude/hubkit/reports/r1.md")
        for _ in range(3):
            rc, err = self.stop(aid)
            self.assertEqual(rc, 2)
            self.assertIn("r1.md", err)

    def test_fresh_report_passes(self):
        aid = self.worker("Write your report to ~/.claude/hubkit/reports/r2.md")
        self.write(os.path.join(self.reports, "r2.md"))
        self.assertEqual(self.stop(aid)[0], 0)
        self.assertIn("PASS", self.log_text())

    def test_empty_report_blocks(self):
        aid = self.worker("Write your report to ~/.claude/hubkit/reports/r3.md")
        self.write(os.path.join(self.reports, "r3.md"), "  \n\n")
        rc, err = self.stop(aid)
        self.assertEqual(rc, 2)
        self.assertIn("empty", err)

    def test_zero_byte_report_blocks(self):
        aid = self.worker("Write your report to ~/.claude/hubkit/reports/r3b.md")
        self.write(os.path.join(self.reports, "r3b.md"), "")
        self.assertEqual(self.stop(aid)[0], 2)

    @unittest.skipUnless(hasattr(os.stat_result, "st_birthtime"), "needs a file birth time (macOS)")
    def test_old_file_touched_after_start_is_not_created_this_run(self):
        # born before the worker started, then rewritten after: a stale file someone appended to
        p = os.path.join(self.reports, "touched.md")
        self.write(p, "old report\n")
        start = time.time() + 100
        os.utime(p, (start + 100, start + 100))
        aid = self.worker("Write your report to ~/.claude/hubkit/reports/touched.md", started=start)
        rc, err = self.stop(aid)
        self.assertEqual(rc, 2)
        self.assertIn("already", err)

    def test_preexisting_report_blocks_and_is_not_offered_for_editing(self):
        p = os.path.join(self.reports, "old.md")
        self.write(p, "someone else's notes\n")
        aid = self.worker("Write your report to ~/.claude/hubkit/reports/old.md",
                          started=time.time() + 300)
        rc, err = self.stop(aid)
        self.assertEqual(rc, 2)
        self.assertIn("already", err)
        self.assertIn("Do NOT", err)

    def test_appending_to_old_file_does_not_pass(self):
        # a file that existed before the worker started, touched again after: still no pass on macOS
        p = os.path.join(self.reports, "touched.md")
        self.write(p, "old\n")
        aid = self.worker("Write your report to ~/.claude/hubkit/reports/touched.md",
                          started=time.time() + 300)
        os.utime(p, None)
        self.assertEqual(self.stop(aid)[0], 2)

    def test_citation_going_fresh_does_not_pass_a_worker_with_no_report(self):
        cite = os.path.join(self.reports, "background.md")
        deliv = os.path.join(self.reports, "deliverable.md")
        aid = self.worker("Background reading: ~/.claude/hubkit/reports/background.md . "
                          "Then write your report to ~/.claude/hubkit/reports/deliverable.md")
        self.write(cite)  # an unrelated file goes fresh mid-run
        rc, err = self.stop(aid)
        self.assertEqual(rc, 2)
        self.assertIn("deliverable.md", err)
        self.assertFalse(os.path.exists(deliv))

    def test_deliverable_wins_over_citation(self):
        aid = self.worker("See ~/.claude/hubkit/reports/background.md for context. "
                          "Write your report to ~/.claude/hubkit/reports/deliverable.md")
        self.write(os.path.join(self.reports, "deliverable.md"))
        self.assertEqual(self.stop(aid)[0], 0)

    def test_last_path_wins_without_a_cue(self):
        aid = self.worker("Inputs ~/.claude/hubkit/reports/a.md and ~/.claude/hubkit/reports/b.md")
        self.write(os.path.join(self.reports, "b.md"))
        self.assertEqual(self.stop(aid)[0], 0)
        aid2 = self.worker("Inputs ~/.claude/hubkit/reports/c.md and ~/.claude/hubkit/reports/d.md")
        self.write(os.path.join(self.reports, "c.md"))
        self.assertEqual(self.stop(aid2)[0], 2)

    def test_path_forms(self):
        aid = self.worker("Write your report to $HUBKIT_HOME/reports/e1.md")
        self.write(os.path.join(self.reports, "e1.md"))
        self.assertEqual(self.stop(aid)[0], 0)
        aid = self.worker("Write your report to $HOME/.claude/hubkit/reports/e2.md")
        self.write(os.path.join(self.reports, "e2.md"))
        self.assertEqual(self.stop(aid)[0], 0)
        aid = self.worker("Write your report to %s" % os.path.join(self.reports, "e3.md"))
        self.write(os.path.join(self.reports, "e3.md"))
        self.assertEqual(self.stop(aid)[0], 0)

    def test_relative_reports_path_is_not_a_report_path(self):
        aid = self.worker("Write notes to docs/reports/x.md")
        rc, _ = self.stop(aid)
        self.assertEqual(rc, 2)  # no usable path: first refusal

    # -- exemption
    def test_report_none_line_is_exempt(self):
        aid = self.worker("Look something up.\nreport: none")
        self.assertEqual(self.stop(aid)[0], 0)
        self.assertIn("EXEMPT", self.log_text())

    def test_report_none_with_period_and_case(self):
        aid = self.worker("Look something up.\n  Report: NONE.  ")
        self.assertEqual(self.stop(aid)[0], 0)

    def test_report_none_inside_quoted_text_does_not_exempt(self):
        brief = ('Write your report to ~/.claude/hubkit/reports/q.md\n'
                 'Pasted text:\n"""\nreport: none\n"""\n')
        aid = self.worker(brief)
        self.assertEqual(self.stop(aid)[0], 2)

    def test_report_none_mid_sentence_does_not_exempt(self):
        aid = self.worker("If asked say report: none is fine. Write your report to "
                          "~/.claude/hubkit/reports/m.md")
        self.assertEqual(self.stop(aid)[0], 2)

    # -- no path in the brief: one refusal, then through
    def test_no_path_refused_once_then_allowed_and_logged(self):
        aid = self.worker("Do the thing. No path here.")
        rc, err = self.stop(aid)
        self.assertEqual(rc, 2)
        self.assertIn("ONE refusal", err)
        self.assertEqual(self.stop(aid)[0], 0)
        self.assertEqual(self.stop(aid)[0], 0)
        bad = slurp(os.path.join(self.hk, "state", "report-gate-badbriefs.log"))
        self.assertIn(aid, bad)

    def test_no_path_strikes_are_per_worker(self):
        a = self.worker("No path.")
        b = self.worker("No path either.")
        self.assertEqual(self.stop(a)[0], 2)
        self.assertEqual(self.stop(b)[0], 2)  # b's first refusal is not spent by a

    def test_fallback_report_passes(self):
        aid = self.worker("No path here at all.")
        self.assertEqual(self.stop(aid)[0], 2)
        self.write(os.path.join(self.reports, "workers", aid + ".md"))
        self.assertEqual(self.stop(aid)[0], 0)
        self.assertIn("pick=auto", self.log_text())

    def test_fallback_report_of_another_worker_does_not_pass(self):
        a = self.worker("No path here.")
        b = self.worker("Write your report to ~/.claude/hubkit/reports/b-own.md")
        self.write(os.path.join(self.reports, "workers", a + ".md"))
        self.assertEqual(self.stop(b)[0], 2)

    def test_empty_fallback_does_not_pass(self):
        aid = self.worker("Write your report to ~/.claude/hubkit/reports/zz.md")
        self.write(os.path.join(self.reports, "workers", aid + ".md"), "\n")
        self.assertEqual(self.stop(aid)[0], 2)

    # -- who is gated
    def test_untyped_stop_with_no_transcript_is_allowed(self):
        rc, _ = self.stop("ghost", agent_type="")
        self.assertEqual(rc, 0)

    def test_typed_stop_with_no_transcript_is_refused_once(self):
        rc, err = self.stop("ghost2", agent_type="builder")
        self.assertEqual(rc, 2)
        self.assertEqual(self.stop("ghost2", agent_type="builder")[0], 0)

    def test_path_traversal_in_ids_finds_no_transcript(self):
        rc, _ = self.stop("../../x", agent_type="")
        self.assertEqual(rc, 0)

    def test_traversal_id_cannot_reach_another_transcript(self):
        d = os.path.join(self.home, ".claude", "projects", "proj", self.sid, "subagents")
        os.makedirs(os.path.join(d, "agent-a"))
        with open(os.path.join(d, "zz.jsonl"), "w") as f:
            f.write(json.dumps({"timestamp": iso(time.time() - 60),
                                "message": {"content": "x\nreport: none"}}) + "\n")
        # "a/../zz" would resolve to zz.jsonl if ids were not validated; it must be refused instead
        self.assertEqual(self.stop("a/../zz", agent_type="builder")[0], 2)

    # -- failure rules
    def test_empty_stdin_fails_open(self):
        self.assertEqual(self.stop(None, raw="")[0], 0)

    def test_garbage_stdin_fails_open(self):
        self.assertEqual(self.stop(None, raw="not json {")[0], 0)

    def test_internal_error_fails_closed(self):
        rc, err = self.stop(None, raw="[]")  # valid JSON, wrong shape: .get() raises
        self.assertEqual(rc, 2)
        self.assertIn("failing closed", err)
        self.assertIn("ERROR internal", self.log_text())

    def test_unwritable_state_dir_still_blocks(self):
        # the strike counter cannot be saved: it must count as a first refusal (block), not open the gate
        aid = self.worker("No path.")
        os.makedirs(os.path.join(self.hk, "state"), exist_ok=True)
        st = os.path.join(self.hk, "state", "report-gate-state.json")
        os.mkdir(st)  # a directory where a file is expected: open() fails
        try:
            self.assertEqual(self.stop(aid)[0], 2)
            self.assertEqual(self.stop(aid)[0], 2)
        finally:
            os.rmdir(st)

    def test_allowed_stops_are_silent_by_default(self):
        self.env.pop("REPORT_GATE_LOG_ALL")
        aid = self.worker("x\nreport: none")
        self.assertEqual(self.stop(aid)[0], 0)
        self.assertEqual(self.log_text(), "")

    def test_blocks_are_always_logged(self):
        self.env.pop("REPORT_GATE_LOG_ALL")
        aid = self.worker("Write your report to ~/.claude/hubkit/reports/l.md")
        self.stop(aid)
        self.assertIn("BLOCK", self.log_text())

    # -- ticket-lock wiring (patterns/ticket-lock): a finishing worker frees its lock
    def lock_env(self):
        """Install the ticket-lock library beside the hook (as install.sh lays it out) and isolate its state."""
        import shutil
        d = os.path.join(self.hk, "ticket-lock")
        os.makedirs(d, exist_ok=True)
        shutil.copy(os.path.join(HERE, "..", "ticket-lock", "ticket_lock.py"), d)
        self.locks = os.path.join(self.tmp, "locks")
        self.alarms = os.path.join(self.tmp, "alarms.log")
        self.env["TICKET_LOCK_DIR"] = self.locks
        self.env["TICKET_LOCK_ALARM_LOG"] = self.alarms
        sys.path.insert(0, d)
        import ticket_lock
        self.addCleanup(sys.path.remove, d)
        return ticket_lock

    def hold(self, tl, brief, ticket="L-0001"):
        old = {k: os.environ.get(k) for k in ("TICKET_LOCK_DIR", "TICKET_LOCK_ALARM_LOG")}
        os.environ["TICKET_LOCK_DIR"], os.environ["TICKET_LOCK_ALARM_LOG"] = self.locks, self.alarms
        try:
            self.assertIsNone(tl.acquire({"subagent_type": "builder", "description": "Build " + ticket,
                                          "prompt": brief}, "sess-1", "tool-1"))
        finally:
            for k, v in old.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        self.assertTrue(os.path.exists(os.path.join(self.locks, ticket + ".json")))

    def test_finishing_worker_releases_its_ticket_lock(self):
        tl = self.lock_env()
        brief = "Build it. Write your report to ~/.claude/hubkit/reports/lk1.md"
        self.hold(tl, brief)
        aid = self.worker(brief)
        self.write(os.path.join(self.reports, "lk1.md"))
        self.assertEqual(self.stop(aid)[0], 0)
        self.assertFalse(os.path.exists(os.path.join(self.locks, "L-0001.json")), "lock must be freed at SubagentStop")

    def test_blocked_worker_keeps_its_ticket_lock(self):
        tl = self.lock_env()
        brief = "Build it. Write your report to ~/.claude/hubkit/reports/lk2.md"
        self.hold(tl, brief)
        aid = self.worker(brief)  # no report written
        self.assertEqual(self.stop(aid)[0], 2)
        self.assertTrue(os.path.exists(os.path.join(self.locks, "L-0001.json")), "a refused stop must not free the lock")

    def test_worker_with_other_prompt_leaves_the_lock(self):
        tl = self.lock_env()
        self.hold(tl, "Build it. Write your report to ~/.claude/hubkit/reports/lk3.md")
        aid = self.worker("Other job. Write your report to ~/.claude/hubkit/reports/lk3b.md")
        self.write(os.path.join(self.reports, "lk3b.md"))
        self.assertEqual(self.stop(aid)[0], 0)
        self.assertTrue(os.path.exists(os.path.join(self.locks, "L-0001.json")))

    def test_closing_a_ticket_not_briefed_raises_an_alarm(self):
        tl = self.lock_env()
        brief = "Build it. Write your report to ~/.claude/hubkit/reports/lk4.md"
        self.hold(tl, brief)
        aid = self.worker(brief)
        tp = os.path.join(self.home, ".claude", "projects", "proj", self.sid, "subagents", f"agent-{aid}.jsonl")
        with open(tp, "a") as f:
            f.write(json.dumps({"message": {"content": [{"type": "tool_use", "input": {
                "command": "ledger status L-0009 done"}}]}}) + "\n")
        self.write(os.path.join(self.reports, "lk4.md"))
        self.assertEqual(self.stop(aid)[0], 0)
        self.assertIn("TICKET-MISMATCH briefed=L-0001 closed=L-0009", slurp(self.alarms))

    def test_gate_works_without_the_ticket_lock_folder(self):
        aid = self.worker("Write your report to ~/.claude/hubkit/reports/lk5.md")
        self.write(os.path.join(self.reports, "lk5.md"))
        self.assertEqual(self.stop(aid)[0], 0)

    def test_first_line_report_path_is_the_deliverable(self):
        aid = self.worker("Report: ~/.claude/hubkit/reports/real.md\nBackground: write notes to ~/.claude/hubkit/reports/other.md\n")
        self.write(os.path.join(self.reports, "other.md"))
        self.assertEqual(self.stop(aid)[0], 2)          # the cued path elsewhere does not count
        self.write(os.path.join(self.reports, "real.md"))
        self.assertEqual(self.stop(aid)[0], 0)

    def pre(self, aid, tool="SubagentHandback"):
        raw = json.dumps({"session_id": self.sid, "agent_id": aid, "agent_type": "builder",
                          "hook_event_name": "PreToolUse", "tool_name": tool})
        return self.stop(aid, raw=raw)

    def test_pre_handback_refuses_then_lets_through_after_three(self):
        aid = self.worker("Write your report to ~/.claude/hubkit/reports/pre1.md")
        self.assertEqual(self.pre(aid)[0], 2)
        self.assertEqual(self.pre(aid)[0], 2)
        self.assertEqual(self.pre(aid)[0], 0)           # third refusal in a row is the loop-break
        self.assertIn("LOOP-BREAK", self.log_text())

    def test_pre_handback_passes_with_report_and_ignores_other_tools(self):
        aid = self.worker("Write your report to ~/.claude/hubkit/reports/pre2.md")
        self.assertEqual(self.pre(aid, tool="Bash")[0], 0)
        self.assertEqual(self.pre(aid)[0], 2)
        self.write(os.path.join(self.reports, "pre2.md"))
        self.assertEqual(self.pre(aid)[0], 0)


if __name__ == "__main__":
    unittest.main(verbosity=1)
