#!/usr/bin/env python3
"""Tests for ticket_lock.py and ticket-lock.py (the library and its command line).
All state lives in a scratch folder; the real lock folder is never touched."""
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
LIB = os.environ.get("LIB_DIR") or HERE
sys.path.insert(0, LIB)
import ticket_lock as tl  # noqa: E402

CLI = os.path.join(HERE, "ticket-lock.py")


class LockCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.saved = {k: os.environ.get(k) for k in ("TICKET_LOCK_DIR", "TICKET_LOCK_ALARM_LOG", "TICKET_LOCK_NOW",
                                                      "TICKET_LOCK_TTL_S", "TICKET_LOCK_ID_RE", "MODEL_GUARD_LOG",
                                                      "TICKET_LOCK_PROJECTS_GLOB", "TICKET_LOCK_SESSION_GLOB")}
        os.environ.pop("MODEL_GUARD_LOG", None)
        os.environ.pop("TICKET_LOCK_NOW", None)
        os.environ.pop("TICKET_LOCK_TTL_S", None)
        os.environ.pop("TICKET_LOCK_ID_RE", None)
        os.environ["TICKET_LOCK_DIR"] = os.path.join(self.tmp, "locks")
        os.environ["TICKET_LOCK_ALARM_LOG"] = os.path.join(self.tmp, "alarms.log")
        os.environ["TICKET_LOCK_PROJECTS_GLOB"] = os.path.join(self.tmp, "projects", "{sid}", "subagents", "agent-*.jsonl")
        os.environ["TICKET_LOCK_SESSION_GLOB"] = os.path.join(self.tmp, "sess", "{sid}.jsonl")
        self.t = 1_000_000.0
        self.nw = 0

    def tearDown(self):
        for k, v in self.saved.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        for root, dirs, files in os.walk(self.tmp, topdown=False):
            for f in files:
                os.remove(os.path.join(root, f))
            for d in dirs:
                os.rmdir(os.path.join(root, d))
        os.rmdir(self.tmp)

    def at(self, minutes):
        os.environ["TICKET_LOCK_NOW"] = str(self.t + minutes * 60)

    def start_worker(self, prompt, session="s1", mtime=None):
        """What the platform does once an allowed spawn really starts: a subagent transcript whose
        first line is the brief. Without it a claim stays provisional."""
        self.nw += 1
        d = os.path.join(self.tmp, "projects", session, "subagents")
        os.makedirs(d, exist_ok=True)
        fp = os.path.join(d, "agent-w%d.jsonl" % self.nw)
        with open(fp, "w") as f:
            f.write(json.dumps({"message": {"content": prompt}}) + "\n")
        # the lock reads file mtimes, so the file must carry the SAME fake clock the claim was stamped with
        ts = mtime if mtime is not None else tl.now()
        os.utime(fp, (ts, ts))
        return fp

    def spawn(self, desc, prompt="do it", stype="builder", session="s1", key="k", started=True):
        r = tl.acquire({"subagent_type": stype, "description": desc, "prompt": prompt}, session, key)
        if started and r is None and stype in tl.LOCK_TYPES and "DUP-OK" not in prompt:
            self.start_worker(prompt, session)
        return r

    def age_claim(self, t, secs):
        with open(self.lockfile(t)) as f:
            lk = json.load(f)
        lk["started"] -= secs
        with open(self.lockfile(t), "w") as f:
            json.dump(lk, f)

    def lockfile(self, t):
        return os.path.join(os.environ["TICKET_LOCK_DIR"], t + ".json")

    def alarms(self):
        p = os.environ["TICKET_LOCK_ALARM_LOG"]
        return open(p).read() if os.path.exists(p) else ""

    def test_second_builder_on_a_held_ticket_is_refused(self):
        self.at(0)
        self.assertIsNone(self.spawn("Build L-0001 widget"))
        msg = self.spawn("Build L-0001 again")
        self.assertIn("L-0001 already owned by Build L-0001 widget", msg)

    def test_different_ticket_is_never_refused(self):
        self.at(0)
        self.assertIsNone(self.spawn("Build L-0001"))
        self.assertIsNone(self.spawn("Build L-0002"))

    def test_ticket_cited_only_in_the_prompt_is_not_locked(self):
        self.at(0)
        self.assertIsNone(self.spawn("Build L-0001", prompt="follows up L-0002"))
        self.assertIsNone(self.spawn("Build L-0002"))

    def test_gate_agents_never_lock_and_are_never_refused(self):
        self.at(0)
        self.assertIsNone(self.spawn("Build L-0001"))
        for g in ("bug-gate", "playtester", "planner", "Explore"):
            self.assertIsNone(self.spawn("check L-0001", stype=g), g)

    def test_dup_ok_skips_lock_and_check(self):
        self.at(0)
        self.assertIsNone(self.spawn("Build L-0001"))
        self.assertIsNone(self.spawn("Verify L-0001", prompt="second opinion\nDUP-OK"))
        self.assertIn("Build L-0001", json.load(open(self.lockfile("L-0001")))["owner"])

    def test_handoff_line_transfers_and_is_logged(self):
        self.at(0)
        self.assertIsNone(self.spawn("Build L-0001 a"))
        self.assertIsNone(self.spawn("Build L-0001 b", prompt="intro\nHANDOFF-FROM: Build L-0001 a\nrest"))
        self.assertEqual(json.load(open(self.lockfile("L-0001")))["owner"], "Build L-0001 b")
        self.assertIn("TICKET-HANDOFF L-0001", self.alarms())

    def test_handoff_must_start_a_line(self):
        self.at(0)
        self.assertIsNone(self.spawn("Build L-0001 a"))
        self.assertTrue(self.spawn("Build L-0001 b", prompt="we never write HANDOFF-FROM: x mid-line"))
        self.assertTrue(self.spawn("Build L-0001 b", prompt="> HANDOFF-FROM: x"))

    def test_lock_expires_after_the_ttl(self):
        self.at(0)
        self.assertIsNone(self.spawn("Build L-0001"))
        self.at(59)
        self.assertTrue(self.spawn("Build L-0001 again"))
        self.at(61)
        self.assertIsNone(self.spawn("Build L-0001 again"))

    def test_redispatch_from_the_same_session_after_ten_minutes(self):
        self.at(0)
        self.assertIsNone(self.spawn("Build L-0001"))
        self.at(5)
        self.assertTrue(self.spawn("Build L-0001 fix round", session="s1"))
        self.at(11)
        self.assertTrue(self.spawn("Build L-0001 fix round", session="other"))
        self.assertTrue(self.spawn("Build L-0001 fix round", session=""))
        self.assertIsNone(self.spawn("Build L-0001 fix round", session="s1"))

    def test_release_frees_the_ticket(self):
        self.at(0)
        self.assertIsNone(self.spawn("Build L-0001"))
        self.assertTrue(tl.release("L-0001"))
        self.assertFalse(tl.release("L-0001"))
        self.assertIsNone(self.spawn("Build L-0001"))

    def test_corrupt_lock_files_fail_open(self):
        self.at(0)
        os.makedirs(os.environ["TICKET_LOCK_DIR"])
        for junk in ("[]", "null", "garbage", '{"started": "x"}'):
            with open(self.lockfile("L-0001"), "w") as f:
                f.write(junk)
            self.assertIsNone(self.spawn("Build L-0001"), junk)

    def test_release_and_audit_frees_only_the_workers_own_lock(self):
        self.at(0)
        mine = "Build it.\nReport to x.md"
        self.assertIsNone(self.spawn("Build L-0001", prompt=mine))
        self.assertIsNone(self.spawn("Build L-0002", prompt="Another job entirely"))
        # the stop transcript's prompt may carry extra text appended after the brief
        tl.release_and_audit(mine + "\n\nextra lessons appended by a harness", None, "a1")
        self.assertFalse(os.path.exists(self.lockfile("L-0001")))
        self.assertTrue(os.path.exists(self.lockfile("L-0002")))

    def test_release_and_audit_ignores_a_prompt_that_only_shares_a_prefix(self):
        self.at(0)
        self.assertIsNone(self.spawn("Build L-0001", prompt="Build it. Long brief with details"))
        tl.release_and_audit("Build it.", None, "a1")
        self.assertTrue(os.path.exists(self.lockfile("L-0001")))

    def test_closing_an_unbriefed_ticket_alarms(self):
        self.at(0)
        brief = "Build it"
        self.assertIsNone(self.spawn("Build L-0001", prompt=brief))
        tp = os.path.join(self.tmp, "t.jsonl")
        with open(tp, "w") as f:
            for cmd in ("ledger status L-0001 done", "FOO=1 /x/bin/ledger status L-0009 closed",
                        "echo ledger status L-0008 done", "ledger status L-0006 open", "grep 'ledger status L-0007 done' f"):
                f.write(json.dumps({"message": {"content": [{"type": "tool_use", "input": {"command": cmd}}]}}) + "\n")
        tl.release_and_audit(brief, tp, "a1")
        a = self.alarms()
        self.assertIn("TICKET-MISMATCH briefed=L-0001 closed=L-0009", a)
        self.assertNotIn("L-0008", a)
        self.assertNotIn("L-0007", a)
        self.assertNotIn("L-0006", a)

    def test_ticket_id_shape_can_be_changed(self):
        os.environ["TICKET_LOCK_ID_RE"] = r"TASK-\d+"
        import importlib
        importlib.reload(tl)
        try:
            self.at(0)
            self.assertIsNone(self.spawn("Build TASK-7"))
            self.assertTrue(self.spawn("Build TASK-7 again"))
            self.assertIsNone(self.spawn("Build L-0001"))
            self.assertIsNone(self.spawn("Build L-0001 again"))  # not a ticket in this shape
        finally:
            os.environ.pop("TICKET_LOCK_ID_RE", None)
            importlib.reload(tl)

    def test_lock_dir_defaults_inside_the_hubkit_state_folder(self):
        os.environ.pop("TICKET_LOCK_DIR")
        env_home = os.environ.get("HUBKIT_HOME")
        os.environ["HUBKIT_HOME"] = self.tmp
        try:
            self.assertEqual(tl.lock_dir(), os.path.join(self.tmp, "state", "ticket-locks"))
        finally:
            os.environ.pop("HUBKIT_HOME") if env_home is None else os.environ.__setitem__("HUBKIT_HOME", env_home)

    # ---- a claim is provisional until the worker starts ----
    def test_unstarted_claim_same_session_same_brief_refreshes_after_min_age(self):
        self.at(0)
        self.assertIsNone(self.spawn("Build L-0001", prompt="brief A", started=False))
        self.assertTrue(self.spawn("Build L-0001", prompt="brief A", started=False))  # under 15 s: duplicate
        self.at(0.5)  # 30 s
        self.assertIsNone(self.spawn("Build L-0001", prompt="brief A", started=False))  # retry refreshes

    def test_unstarted_claim_blocks_other_session_until_it_lapses(self):
        self.at(0)
        self.assertIsNone(self.spawn("Build L-0001", prompt="brief A", started=False))
        self.at(1)
        self.assertTrue(self.spawn("Build L-0001", prompt="brief A", session="s2"))
        self.at(2.5)  # 150 s, past the 120 s provisional window
        self.assertIsNone(self.spawn("Build L-0001", prompt="brief A", session="s2"))

    def test_started_worker_keeps_the_claim_past_the_provisional_window(self):
        self.at(0)
        self.assertIsNone(self.spawn("Build L-0001", prompt="brief A"))
        self.at(10)
        self.assertTrue(self.spawn("Build L-0001", prompt="brief A", session="s2"))
        self.assertTrue(self.spawn("Build L-0001", prompt="brief A", session="s1"))

    def test_different_brief_same_session_stays_blocked_while_provisional(self):
        self.at(0)
        self.assertIsNone(self.spawn("Build L-0001", prompt="brief A", started=False))
        self.at(0.5)
        self.assertTrue(self.spawn("Build L-0001", prompt="brief B", started=False))

    def test_released_lock_can_be_claimed(self):
        self.at(0)
        self.assertIsNone(self.spawn("Build L-0001"))
        tl.release("L-0001")
        self.assertIsNone(self.spawn("Build L-0001", session="s2"))

    def test_missing_lock_dir_and_garbage_registry_fail_open(self):
        self.at(0)
        self.assertFalse(os.path.exists(os.environ["TICKET_LOCK_DIR"]))
        self.assertIsNone(self.spawn("Build L-0001"))
        with open(self.lockfile("L-0002"), "w") as f:
            f.write("{not json")
        self.assertIsNone(self.spawn("Build L-0002"))

    def test_future_stamped_claim_does_not_hold_the_ticket(self):
        self.at(0)
        self.assertIsNone(self.spawn("Build L-0001", started=False))
        self.age_claim("L-0001", -7200)
        self.assertIsNone(self.spawn("Build L-0001", session="s2"))

    def test_stored_agent_id_counts_as_started(self):
        self.at(0)
        self.assertIsNone(self.spawn("Build L-0001", started=False))
        with open(self.lockfile("L-0001")) as f:
            lk = json.load(f)
        lk["agent_id"] = "a1"
        with open(self.lockfile("L-0001"), "w") as f:
            json.dump(lk, f)
        self.at(10)
        self.assertTrue(self.spawn("Build L-0001", session="s2"))

    def test_refresh_keeps_original_claim_time_as_evidence_floor(self):
        self.at(0)
        self.assertIsNone(self.spawn("Build L-0001", prompt="brief A", started=False))
        self.at(0.5)
        self.assertIsNone(self.spawn("Build L-0001", prompt="brief A", started=False))
        with open(self.lockfile("L-0001")) as f:
            lk = json.load(f)
        self.assertLess(lk["since"], lk["started"])

    # ---- what counts as proof the worker started (each test pins one guard) ----
    def test_transcript_with_a_different_brief_is_not_proof_of_start(self):
        self.at(0)
        self.assertIsNone(self.spawn("Build L-0001", prompt="brief A", started=False))
        self.start_worker("brief B")  # same session, same length, other brief
        self.start_worker("a much longer other brief that is not brief A at all")
        self.at(5)
        self.assertIsNone(self.spawn("Build L-0001", prompt="brief A", session="s2", started=False))

    def test_transcript_older_than_the_claim_is_not_proof_of_start(self):
        self.at(10)
        self.assertIsNone(self.spawn("Build L-0001", prompt="brief A", started=False))
        self.start_worker("brief A", mtime=self.t + 9 * 60)  # an earlier worker of the same brief
        self.at(15)
        self.assertIsNone(self.spawn("Build L-0001", prompt="brief A", session="s2", started=False))

    def test_transcript_written_at_or_after_the_claim_is_proof_of_start(self):
        self.at(10)
        self.assertIsNone(self.spawn("Build L-0001", prompt="brief A", started=False))
        self.start_worker("brief A", mtime=self.t + 10 * 60)  # exactly the claim time still counts
        self.at(15)
        self.assertTrue(self.spawn("Build L-0001", prompt="brief A", session="s2", started=False))

    def test_transcript_of_another_session_is_not_proof_of_start(self):
        self.at(0)
        self.assertIsNone(self.spawn("Build L-0001", prompt="brief A", started=False))
        self.start_worker("brief A", session="s9")
        self.at(5)
        self.assertIsNone(self.spawn("Build L-0001", prompt="brief A", session="s2", started=False))

    def test_claim_without_a_session_can_never_be_proven_started(self):
        # a glob with no {sid} would match every transcript, so only the session gate stops a sessionless claim
        os.environ["TICKET_LOCK_PROJECTS_GLOB"] = os.path.join(self.tmp, "projects", "*", "subagents", "agent-*.jsonl")
        self.at(0)
        self.assertIsNone(self.spawn("Build L-0001", prompt="brief A", session="", started=False))
        self.start_worker("brief A", session="s1")
        self.at(5)
        self.assertIsNone(self.spawn("Build L-0001", prompt="brief A", session="s2", started=False))

    def test_empty_brief_claim_is_never_proven_started_by_an_unrelated_transcript(self):
        self.at(0)
        self.assertIsNone(self.spawn("Build L-0001", prompt="", started=False))
        self.start_worker("an unrelated brief")
        self.at(5)
        self.assertIsNone(self.spawn("Build L-0001", prompt="", session="s2", started=False))

    def test_transcript_shorter_than_the_brief_is_not_proof_of_start(self):
        self.at(0)
        self.assertIsNone(self.spawn("Build L-0001", prompt="brief A with a long tail", started=False))
        self.start_worker("brief A")  # only a prefix of the brief
        self.at(5)
        self.assertIsNone(self.spawn("Build L-0001", prompt="brief A with a long tail", session="s2", started=False))

    def test_transcript_with_extra_text_after_the_brief_is_still_proof_of_start(self):
        # the platform may append text (lessons, reminders) after the brief; only the brief-length prefix is hashed
        self.at(0)
        self.assertIsNone(self.spawn("Build L-0001", prompt="brief A", started=False))
        self.start_worker("brief A\n\n<lessons>appended by the platform, not part of the brief</lessons>")
        self.at(5)
        self.assertTrue(self.spawn("Build L-0001", prompt="brief A", session="s2", started=False))

    # ---- a claim the session's own transcript shows was blocked by a hook is never an owner ----
    HOOK_ERR = "PreToolUse:Agent hook error: [python3 x.py]: Blocked"

    def session_log(self, lines, session="s1"):
        d = os.path.join(self.tmp, "sess")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, session + ".jsonl"), "w") as f:
            for l in lines:
                f.write((l if isinstance(l, str) else json.dumps(l)) + "\n")

    def result(self, tid, is_error=True, text=None):
        return {"message": {"content": [{"type": "tool_result", "tool_use_id": tid, "is_error": is_error,
                                         "content": self.HOOK_ERR if text is None else text}]}}

    def handoff_alarms(self):
        p = os.environ["TICKET_LOCK_ALARM_LOG"]
        return open(p).read() if os.path.exists(p) else ""

    HO = "HANDOFF-FROM: me (earlier dispatch was blocked)"

    def test_hook_blocked_first_claim_is_not_an_owner(self):
        self.assertIsNone(self.spawn("Build L-0001", key="toolu_A", started=False))
        self.session_log([self.result("toolu_A")])
        self.assertIsNone(self.spawn("Build L-0001 r2", prompt=self.HO, key="toolu_B", started=False))
        self.assertNotIn("TICKET-HANDOFF", self.handoff_alarms())

    def test_hook_blocked_first_claim_lets_a_plain_redispatch_through(self):
        self.assertIsNone(self.spawn("Build L-0001", prompt="brief A", key="toolu_A", started=False))
        self.session_log([self.result("toolu_A")])
        self.assertIsNone(self.spawn("Build L-0001 again", prompt="brief B", session="s2", key="toolu_B", started=False))

    def test_real_first_claim_without_a_block_record_still_blocks_and_alarms(self):
        self.assertIsNone(self.spawn("Build L-0001", key="toolu_A", started=False))
        self.assertTrue(self.spawn("Build L-0001 b", prompt="brief B", session="s2", key="toolu_B", started=False))
        self.assertIsNone(self.spawn("Build L-0001 c", prompt=self.HO, session="s2", key="toolu_C", started=False))
        self.assertIn("TICKET-HANDOFF L-0001", self.handoff_alarms())

    def test_started_worker_is_never_treated_as_blocked(self):
        self.assertIsNone(self.spawn("Build L-0001", key="toolu_A"))  # a transcript of the brief exists
        self.session_log([self.result("toolu_A")])  # a block-looking record must not outweigh a real start
        self.assertTrue(self.spawn("Build L-0001 b", prompt="brief B", session="s2", key="toolu_B", started=False))

    def test_block_proof_needs_a_real_hook_error_for_this_claim(self):
        for name, lines in (
            ("missing transcript", None),
            ("garbage lines", ["{not json", "toolu_A tool_result", "\x00\xff"]),
            ("no is_error", [self.result("toolu_A", is_error=False)]),
            ("error that is not a hook block", [self.result("toolu_A", text="Error: worker crashed")]),
            ("hook phrase mid-text", [self.result("toolu_A", text="quoting: " + self.HOOK_ERR)]),
            ("other tool_use_id", [self.result("toolu_ZZZ")]),
            ("two results on one line, the claim's launched", [{"message": {"content": [
                self.result("toolu_ZZZ")["message"]["content"][0],
                self.result("toolu_A", is_error=False, text="launched")["message"]["content"][0]]}}]),
        ):
            with self.subTest(name):
                for f in os.listdir(os.environ["TICKET_LOCK_DIR"]) if os.path.isdir(os.environ["TICKET_LOCK_DIR"]) else []:
                    os.remove(os.path.join(os.environ["TICKET_LOCK_DIR"], f))
                self.assertIsNone(self.spawn("Build L-0001", prompt="brief A", key="toolu_A", started=False))
                if lines is not None:
                    self.session_log(lines)
                self.assertTrue(self.spawn("Build L-0001 b", prompt="brief B", session="s2", key="toolu_B", started=False))

    # ---- hostile records and session ids: was_blocked answers False, never raises, never reads outside ----
    def lock(self, key="toolu_A", sid="s1"):
        return {"key": key, "session": sid, "started": 1.0, "phash": "h", "plen": 3}

    def raw(self, content, tid="toolu_A"):
        return {"message": {"content": [{"type": "tool_result", "tool_use_id": tid, "is_error": True, "content": content}]}}

    def test_hostile_record_returns_false_and_keeps_the_claim(self):
        # acquire() fails open on any exception, so an error escaping was_blocked would free a live lock
        self.session_log([self.raw([{"text": 5}])])
        self.assertIs(tl.was_blocked(self.lock()), False)
        self.assertIsNone(self.spawn("Build L-0001", key="toolu_A", started=False))
        self.assertTrue(self.spawn("Build L-0001 b", prompt="brief B", session="s2", key="toolu_B", started=False))

    def test_directory_where_the_transcript_should_be_returns_false(self):
        os.makedirs(os.path.join(self.tmp, "sess", "s1.jsonl"))
        self.assertIs(tl.was_blocked(self.lock()), False)

    def test_block_text_patterns_each_matter(self):
        for name, content, want in (
            ("plain", self.HOOK_ERR, True),
            ("list form", [{"type": "text", "text": self.HOOK_ERR}], True),
            ("parts joined with a space", [{"text": "PreToolUse:Agent"}, {"text": "hook error: x"}], True),
            ("leading whitespace", "\n  " + self.HOOK_ERR, True),
            ("empty hook name", "PreToolUse: hook error: x", False),
        ):
            with self.subTest(name):
                self.session_log([self.raw(content)])
                self.assertIs(tl.was_blocked(self.lock()), want)

    def test_key_without_toolu_prefix_is_never_blocked_proof(self):
        self.session_log([self.raw(self.HOOK_ERR, tid="abc")])
        self.assertIs(tl.was_blocked(self.lock(key="abc")), False)

    def test_wildcard_session_ids_do_not_match_other_sessions(self):
        self.session_log([self.raw(self.HOOK_ERR)], session="other")
        self.session_log([self.raw(self.HOOK_ERR)], session="s1")
        for sid in ("*", "s?", "[s]1"):
            with self.subTest(sid):
                self.assertIs(tl.was_blocked(self.lock(sid=sid)), False)
        d = os.path.join(self.tmp, "projects", "other", "subagents")
        os.makedirs(d)
        with open(os.path.join(d, "agent-x.jsonl"), "w") as f:
            f.write(json.dumps({"message": {"content": "brief A"}}) + "\n")
        ph, pl = tl.phash("brief A")
        self.assertIs(tl.has_started({"session": "*", "phash": ph, "plen": pl, "started": 1.0}), False)
        self.assertIs(tl.has_started({"session": "other", "phash": ph, "plen": pl, "started": 1.0}), True)

    def test_path_shaped_session_ids_never_free_a_lock(self):
        # real block records sit where each spelling would resolve, so each check must stand on its own
        os.makedirs(os.path.join(self.tmp, "sess", "sub"))
        for rel in ("sub/b.jsonl", "x..y.jsonl", "../outside.jsonl"):
            with open(os.path.join(self.tmp, "sess", rel), "w") as f:
                f.write(json.dumps(self.raw(self.HOOK_ERR)) + "\n")
        for sid in ("sub/b", "x..y", "../outside", "../../x", "..", "a\\b", "/abs", "a\0b"):
            with self.subTest(sid):
                self.assertIs(tl.was_blocked(self.lock(sid=sid)), False)
        self.assertIsNone(self.spawn("Build L-0001", key="toolu_A", session="../outside", started=False))
        self.assertTrue(self.spawn("Build L-0001 b", prompt="brief B", session="../outside", key="toolu_B", started=False))
        self.assertIs(tl.was_blocked(self.lock(sid="s1")), False)  # no s1 transcript: control for the real id
        self.session_log([self.raw(self.HOOK_ERR)])
        self.assertIs(tl.was_blocked(self.lock(sid="s1")), True)  # control: a real id still frees

    def test_refresh_boundary_is_exactly_fifteen_seconds(self):
        self.at(0)
        self.assertIsNone(self.spawn("Build L-0001", prompt="brief A", started=False))
        os.environ["TICKET_LOCK_NOW"] = str(self.t + 14.9)
        self.assertTrue(self.spawn("Build L-0001", prompt="brief A", started=False))
        os.environ["TICKET_LOCK_NOW"] = str(self.t + 15.0)
        self.assertIsNone(self.spawn("Build L-0001", prompt="brief A", started=False))

    def test_provisional_window_boundary_is_exactly_120_seconds(self):
        self.at(0)
        self.assertIsNone(self.spawn("Build L-0001", prompt="brief A", started=False))
        os.environ["TICKET_LOCK_NOW"] = str(self.t + 120.0)
        self.assertTrue(self.spawn("Build L-0001", prompt="brief A", session="s2", started=False))
        os.environ["TICKET_LOCK_NOW"] = str(self.t + 120.1)
        self.assertIsNone(self.spawn("Build L-0001", prompt="brief A", session="s2", started=False))

    def test_parallel_same_session_claims_exactly_one_wins(self):
        self.at(0)
        code = ("import sys,json;sys.path.insert(0,%r);import ticket_lock as t;"
                "r=t.acquire({'subagent_type':'builder','description':'Build L-0001','prompt':'brief A'},'s1','k');"
                "sys.exit(0 if r is None else 2)" % LIB)
        procs = [subprocess.Popen([sys.executable, "-c", code], env=dict(os.environ)) for _ in range(4)]
        self.assertEqual(sorted(p.wait() for p in procs), [0, 2, 2, 2])

    def cli(self, *args):
        r = subprocess.run([sys.executable, CLI, *args], capture_output=True, text=True, env=dict(os.environ))
        return r.returncode, r.stdout, r.stderr

    def test_cli_release_and_list(self):
        self.at(0)
        self.assertIsNone(self.spawn("Build L-0001"))
        rc, out, _ = self.cli("list")
        self.assertEqual(rc, 0)
        self.assertIn("L-0001", out)
        self.assertEqual(self.cli("release", "L-0001")[1].strip(), "released L-0001")
        self.assertEqual(self.cli("release", "L-0001")[1].strip(), "no lock for L-0001")

    def test_cli_rejects_odd_arguments(self):
        for a in (("release", "../x"), ("release", "L-0001", "x"), ("release",), ("bogus",), ()):
            self.assertEqual(self.cli(*a)[0], 2, a)


if __name__ == "__main__":
    unittest.main(verbosity=1)
