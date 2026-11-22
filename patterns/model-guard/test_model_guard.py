#!/usr/bin/env python3
"""Tests for model-guard.py: run the hook as Claude Code does (JSON on stdin)."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

# The older cases below carry no "Report:" line; the report-path rule has its own case (ReportPathCase) that turns it on.
os.environ["MODEL_GUARD_REPORT_CHECK"] = "0"
HERE = os.path.dirname(os.path.abspath(__file__))
HOOK = os.environ.get("HOOK", os.path.join(HERE, "model-guard.py"))


def slurp(path):
    with open(path) as f:
        return f.read()


class GuardCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.logp = os.path.join(self.tmp, "state", "mg.log")
        self.env = dict(os.environ, MODEL_GUARD_LOG=self.logp, PYTHONDONTWRITEBYTECODE="1",
                        GATE_SPAWNS_LOG=os.path.join(self.tmp, "gs.jsonl"))
        self.env.pop("MODEL_GUARD_ALLOWED", None)

    def tearDown(self):
        for root, dirs, files in os.walk(self.tmp, topdown=False):
            for f in files:
                os.remove(os.path.join(root, f))
            for d in dirs:
                os.rmdir(os.path.join(root, d))
        os.rmdir(self.tmp)

    def run_hook(self, payload, env=None):
        raw = payload if isinstance(payload, str) else json.dumps(payload)
        return subprocess.run([sys.executable, HOOK], input=raw, capture_output=True, text=True,
                              env=env or self.env)

    def agent(self, tool="Agent", **tin):
        return self.run_hook({"tool_name": tool, "tool_input": tin})

    # -- the rule
    def test_agent_without_model_is_blocked_with_a_reason(self):
        r = self.agent(description="look around", prompt="find files")
        self.assertEqual(r.returncode, 2)
        self.assertIn("model", r.stderr)
        self.assertEqual(r.stdout, "")

    def test_task_without_model_is_blocked(self):
        self.assertEqual(self.agent(tool="Task", prompt="x").returncode, 2)

    def test_blank_and_none_model_are_blocked(self):
        for m in ("", "   ", None):
            self.assertEqual(self.agent(model=m, prompt="x").returncode, 2, repr(m))

    def test_inherit_is_blocked_in_any_case(self):
        for m in ("inherit", "Inherit", " INHERIT "):
            self.assertEqual(self.agent(model=m, prompt="x").returncode, 2, m)

    def test_any_named_model_passes_including_new_ones(self):
        for m in ("haiku", "sonnet", "opus", "Sonnet", "some-future-model"):
            self.assertEqual(self.agent(model=m, prompt="x").returncode, 0, m)

    def test_no_tool_input_at_all_is_blocked(self):
        self.assertEqual(self.run_hook({"tool_name": "Agent"}).returncode, 2)

    # -- allow list
    def test_allow_list_blocks_models_outside_it(self):
        env = dict(self.env, MODEL_GUARD_ALLOWED="haiku, Sonnet")
        self.assertEqual(self.run_hook({"tool_name": "Agent", "tool_input": {"model": "opus"}}, env).returncode, 2)
        self.assertEqual(self.run_hook({"tool_name": "Agent", "tool_input": {"model": "sonnet"}}, env).returncode, 0)
        self.assertEqual(self.run_hook({"tool_name": "Agent", "tool_input": {"model": "HAIKU"}}, env).returncode, 0)

    def test_empty_allow_list_means_no_restriction(self):
        env = dict(self.env, MODEL_GUARD_ALLOWED=" , ")
        self.assertEqual(self.run_hook({"tool_name": "Agent", "tool_input": {"model": "opus"}}, env).returncode, 0)

    # -- other tools
    def test_other_tools_are_ignored(self):
        for t in ("Bash", "Read", "Write", ""):
            r = self.run_hook({"tool_name": t, "tool_input": {"command": "ls"}})
            self.assertEqual((r.returncode, r.stderr), (0, ""), t)

    def test_workflow_passes_untouched_even_with_a_missing_model_option(self):
        for script in ("agent('a', {model: 'haiku'}); agent('b'); agent('c')", "agent('a', {model: 'haiku'})"):
            r = self.run_hook({"tool_name": "Workflow", "tool_input": {"script": script}})
            self.assertEqual((r.returncode, r.stderr), (0, ""), script)

    def test_workflow_with_no_agents_is_quiet(self):
        r = self.run_hook({"tool_name": "Workflow", "tool_input": {"script": "log('hi')"}})
        self.assertEqual((r.returncode, r.stderr), (0, ""))

    # -- fail closed for a spawn, open for everything else
    def test_unreadable_or_wrong_shape_payloads_are_refused(self):
        for raw in ("not json {", "", "[]", "null", '"Agent"', "\xff\xfe", '{"tool_name": 5}', '{"tool_name": ["Agent"]}'):
            r = self.run_hook(raw)
            self.assertEqual(r.returncode, 2, repr(raw))
            self.assertIn("refused", r.stderr)
            self.assertIn("FAIL-CLOSED", slurp(self.logp))

    def test_a_spawn_with_a_non_object_tool_input_is_refused_but_other_tools_pass(self):
        for tin in ("x", ["model", "haiku"], 7, True):
            self.assertEqual(self.run_hook({"tool_name": "Agent", "tool_input": tin}).returncode, 2, repr(tin))
            self.assertEqual(self.run_hook({"tool_name": "Task", "tool_input": tin}).returncode, 2, repr(tin))
            self.assertEqual(self.run_hook({"tool_name": "Workflow", "tool_input": tin}).returncode, 0, repr(tin))
            self.assertEqual(self.run_hook({"tool_name": "Bash", "tool_input": tin}).returncode, 0, repr(tin))

    def test_missing_or_blank_tool_name_and_null_input_are_not_a_spawn(self):
        for raw in ('{}', '{"tool_name": null}', '{"tool_name": ""}', '{"tool_name": "Bash", "tool_input": null}'):
            self.assertEqual(self.run_hook(raw).returncode, 0, raw)

    def test_a_crash_inside_the_check_refuses_a_spawn_but_not_another_tool(self):
        d = os.path.join(self.tmp, "crash")
        os.makedirs(d)
        src = slurp(HOOK).replace("def decide(data):", "def decide(data):\n    raise RuntimeError('boom')", 1)
        hook = os.path.join(d, "model-guard.py")
        with open(hook, "w") as f:
            f.write(src)

        def run(tool):
            return subprocess.run([sys.executable, hook], input=json.dumps({"tool_name": tool, "tool_input": {"model": "haiku"}}),
                                  capture_output=True, text=True, env=self.env)
        self.assertEqual(run("Agent").returncode, 2)
        self.assertEqual(run("Task").returncode, 2)
        self.assertEqual(run("Workflow").returncode, 0)
        self.assertIn("FAIL-OPEN", slurp(self.logp))

    def test_a_block_is_still_exit_2_when_stderr_is_broken(self):
        for payload in ({"tool_name": "Agent", "tool_input": {"prompt": "x"}}, "not json"):
            rfd, wfd = os.pipe()
            os.close(rfd)  # nobody reads stderr: every write to it fails
            raw = payload if isinstance(payload, str) else json.dumps(payload)
            try:
                p = subprocess.run([sys.executable, HOOK], input=raw, stdout=subprocess.PIPE, stderr=wfd, text=True,
                                   env=self.env)
            finally:
                os.close(wfd)
            self.assertEqual(p.returncode, 2, repr(payload))

    def test_unwritable_log_does_not_change_the_decision(self):
        env = dict(self.env, MODEL_GUARD_LOG="/dev/null/x/log")
        self.assertEqual(self.run_hook({"tool_name": "Agent", "tool_input": {}}, env).returncode, 2)
        self.assertEqual(self.run_hook({"tool_name": "Agent", "tool_input": {"model": "haiku"}}, env).returncode, 0)

    def test_non_string_model_values_do_not_crash(self):
        r = self.agent(model=12345, prompt="x")
        self.assertEqual(r.returncode, 0)  # "12345" is a named (if odd) model; the guard only checks it is set
        self.assertEqual(self.agent(model=["opus"], prompt="x").returncode, 0)

    # -- log
    def test_decisions_are_logged(self):
        self.agent(description="first job", prompt="x")
        self.agent(model="haiku", description="second job", prompt="x")
        text = slurp(self.logp)
        self.assertIn("BLOCKED unset model: first job", text)
        self.assertIn("OK model=haiku: second job", text)

    def test_default_log_lives_under_hubkit_home(self):
        home = os.path.join(self.tmp, "hk")
        env = {k: v for k, v in self.env.items() if k != "MODEL_GUARD_LOG"}
        env["HUBKIT_HOME"] = home
        self.run_hook({"tool_name": "Agent", "tool_input": {"model": "haiku"}}, env)
        self.assertTrue(os.path.exists(os.path.join(home, "state", "model-guard.log")))
        os.remove(os.path.join(home, "state", "model-guard.log"))
        os.rmdir(os.path.join(home, "state"))
        os.rmdir(home)

    # -- ticket lock (patterns/ticket-lock): one owner per ticket, taken as the last gate
    def build(self, desc, prompt="do it", stype="builder", env=None):
        return self.run_hook({"tool_name": "Agent", "session_id": "s1", "tool_use_id": "t1", "tool_input": {
            "description": desc, "prompt": prompt, "subagent_type": stype, "model": "sonnet"}}, env=env)

    def test_second_builder_on_the_same_ticket_is_refused_with_the_handoff_hint(self):
        self.assertEqual(self.build("Build L-0001 widget").returncode, 0)
        r = self.build("Build L-0001 again")
        self.assertEqual(r.returncode, 2)
        self.assertIn("L-0001 already owned by Build L-0001 widget", r.stderr)
        self.assertIn("HANDOFF-FROM", r.stderr)
        self.assertIn("BLOCKED ticket lock", slurp(self.logp))

    def test_other_ticket_and_gate_agents_are_not_refused(self):
        self.assertEqual(self.build("Build L-0001").returncode, 0)
        self.assertEqual(self.build("Build L-0002").returncode, 0)
        self.assertEqual(self.build("Review L-0001", stype="bug-gate").returncode, 0)

    def test_handoff_line_and_dup_ok_get_through(self):
        self.assertEqual(self.build("Build L-0001 a").returncode, 0)
        self.assertEqual(self.build("Build L-0001 b", prompt="x\nHANDOFF-FROM: a").returncode, 0)
        self.assertEqual(self.build("Verify L-0001", prompt="DUP-OK second opinion").returncode, 0)

    def test_a_spawn_refused_by_the_model_rule_does_not_take_the_lock(self):
        r = self.run_hook({"tool_name": "Agent", "tool_input": {
            "description": "Build L-0003", "prompt": "x", "subagent_type": "builder"}})
        self.assertEqual(r.returncode, 2)
        self.assertEqual(self.build("Build L-0003").returncode, 0)

    def test_same_brief_sent_again_goes_through_as_a_handoff(self):
        self.assertEqual(self.build("Build L-0001 widget").returncode, 0)
        first = self.build("Build L-0001 again")
        self.assertEqual(first.returncode, 2)
        self.assertIn("Blocked ONCE", first.stderr)
        self.assertEqual(self.build("Build L-0001 again").returncode, 0)          # the retry takes the lock
        self.assertIn("OWNER-RETRY-ALLOW", slurp(self.logp))
        self.assertEqual(self.build("Build L-0001 third", prompt="a different brief").returncode, 2)   # a new brief is refused again

    def test_a_refusal_that_keeps_coming_back_is_stopped_after_three(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("mg_under_test", HOOK)
        mg = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mg)
        mg.owner_stop_path = lambda: os.path.join(self.tmp, "stop.json")
        tin = {"subagent_type": "builder", "description": "Build L-0001", "prompt": "do it"}
        self.assertEqual([mg.owner_refusals(tin, "do it") for _ in range(5)], [1, 2, 3, 4, 5])
        self.assertEqual(mg.owner_refusals(dict(tin, description="other"), "do it"), 1)   # another payload counts apart
        with open(os.path.join(self.tmp, "stop.json"), "w") as f:
            f.write("not json")
        self.assertEqual(mg.owner_refusals(tin, "do it"), 1)                             # garbage state fails open

    def test_every_allowed_spawn_leaves_a_type_and_model_line(self):
        spawns = os.path.join(self.tmp, "ms.jsonl")
        env = dict(self.env, MODEL_SPAWNS_LOG=spawns)
        self.run_hook({"tool_name": "Agent", "tool_input": {"model": "haiku", "subagent_type": "Explore",
                                                              "prompt": "SECRET-BRIEF-TEXT"}}, env)
        self.run_hook({"tool_name": "Agent", "tool_input": {"prompt": "x"}}, env)    # refused: no line
        rows = [json.loads(x) for x in slurp(spawns).splitlines()]
        self.assertEqual(rows, [{"ts": rows[0]["ts"], "type": "explore", "model": "haiku"}])
        self.assertNotIn("SECRET", slurp(spawns))

    def test_without_the_ticket_lock_library_spawns_still_work(self):
        import shutil as sh
        d = os.path.join(self.tmp, "solo")
        os.makedirs(d)
        sh.copy(HOOK, d)
        env = dict(self.env, HUBKIT_HOME=os.path.join(self.tmp, "empty-home"))
        for _ in range(2):  # no lock library beside the hook: nothing is held, so the repeat passes
            r = subprocess.run([sys.executable, os.path.join(d, "model-guard.py")], capture_output=True, text=True, env=env,
                               input=json.dumps({"tool_name": "Agent", "tool_input": {
                                   "description": "Build L-0004", "prompt": "x", "subagent_type": "builder", "model": "sonnet"}}))
            self.assertEqual(r.returncode, 0)


class GateIndexCase(unittest.TestCase):
    """The gate-spawns index is observability only: it records builder and bug-gate spawns and can
    never block or break a spawn. Every run uses a scratch folder; none touches the live index."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.idx = os.path.join(self.tmp, "state", "gs.jsonl")
        self.env = dict(os.environ, MODEL_GUARD_LOG=os.path.join(self.tmp, "mg.log"),
                        GATE_SPAWNS_LOG=self.idx, PYTHONDONTWRITEBYTECODE="1")
        self.env.pop("MODEL_GUARD_ALLOWED", None)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def spawn(self, env=None, model="sonnet", **tin):
        if model:
            tin["model"] = model
        return subprocess.run([sys.executable, HOOK], input=json.dumps(
            {"tool_name": "Agent", "session_id": "s1", "tool_input": tin}),
            capture_output=True, text=True, env=env or self.env)

    def lines(self):
        with open(self.idx) as f:
            return [json.loads(x) for x in f.read().splitlines()]

    def test_builder_and_bug_gate_spawns_are_indexed(self):
        self.assertEqual(self.spawn(subagent_type="builder", description="L-0004 S7 build", prompt="x").returncode, 0)
        self.assertEqual(self.spawn(subagent_type="Bug-Gate", description="L-0004 S7 gate", prompt="x").returncode, 0)
        a, b = self.lines()
        self.assertEqual((a["subagent_type"], a["tickets"], a["slice"], a["model"], a["session_id"]),
                         ("builder", ["L-0004"], "S7", "sonnet", "s1"))
        self.assertEqual(b["subagent_type"], "bug-gate")

    def test_every_other_spawn_type_is_allowed_and_not_indexed(self):
        for tin in ({"subagent_type": "fork"}, {"subagent_type": "planner"}, {"subagent_type": "general-purpose"},
                    {}, {"subagent_type": ""}, {"subagent_type": ["builder"]}):
            self.assertEqual(self.spawn(prompt="x", **tin).returncode, 0, tin)
        self.assertFalse(os.path.exists(self.idx))

    def test_refused_spawns_are_not_indexed(self):
        self.assertEqual(self.spawn(model="", subagent_type="builder", prompt="x").returncode, 2)
        env = dict(self.env, MODEL_GUARD_ALLOWED="haiku")
        self.assertEqual(self.spawn(env=env, subagent_type="builder", prompt="x").returncode, 2)
        self.assertFalse(os.path.exists(self.idx))

    def test_same_second_spawns_all_land_as_whole_lines(self):
        payload = json.dumps({"tool_name": "Agent", "tool_input": {
            "model": "sonnet", "subagent_type": "builder", "description": "L-0001 S1 " + "x" * 250, "prompt": "p DUP-OK (twelve spawns of one ticket on purpose)"}})
        procs = [subprocess.Popen([sys.executable, HOOK], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True, env=self.env) for _ in range(12)]
        for p in procs:
            p.communicate(payload)
            self.assertEqual(p.returncode, 0)
        self.assertEqual(len(self.lines()), 12)

    def test_unwritable_index_never_blocks_or_speaks(self):
        for bad in ("/dev/null/x/gs.jsonl", self.tmp):  # parent is a file; index path is a folder
            r = self.spawn(env=dict(self.env, GATE_SPAWNS_LOG=bad), subagent_type="builder", prompt="x")
            self.assertEqual((r.returncode, r.stderr, r.stdout), (0, "", ""), bad)
        self.assertIn("GATE-SPAWN index write failed", slurp(os.path.join(self.tmp, "mg.log")))

    def test_read_only_index_file_never_blocks(self):
        os.makedirs(os.path.dirname(self.idx))
        open(self.idx, "w").close()
        os.chmod(self.idx, 0o444)
        self.assertEqual(self.spawn(subagent_type="bug-gate", prompt="x").returncode, 0)

    def test_symlinked_index_is_not_written_through_and_still_allows(self):
        target = os.path.join(self.tmp, "victim.txt")
        with open(target, "w") as f:
            f.write("keep\n")
        os.makedirs(os.path.dirname(self.idx))
        os.symlink(target, self.idx)
        r = self.spawn(subagent_type="builder", description="L-0001", prompt="x")
        self.assertEqual((r.returncode, r.stderr), (0, ""))
        self.assertEqual(slurp(target), "keep\n")

    def test_corrupt_existing_index_still_allows_and_appends(self):
        os.makedirs(os.path.dirname(self.idx))
        with open(self.idx, "wb") as f:
            f.write(b"\xff\xfe{{not json\n\x00\x00")
        r = self.spawn(subagent_type="builder", description="L-0007 S2", prompt="x")
        self.assertEqual((r.returncode, r.stderr), (0, ""))
        with open(self.idx, "rb") as f:
            self.assertIn(b'"L-0007"', f.read())

    def test_ticket_only_in_prompt_head_is_found_and_description_wins(self):
        self.spawn(subagent_type="builder", description="build it", prompt="Ticket L-0009 S3. See also L-0001.")
        self.spawn(subagent_type="builder", description="L-0005 build", prompt="Ticket L-0009")
        self.spawn(subagent_type="builder", description="build it", prompt="y" * 500 + " L-0010")
        a, b, c = self.lines()
        self.assertEqual((a["tickets"], a["slice"]), (["L-0009"], "S3"))
        self.assertEqual(b["tickets"], ["L-0005"])
        self.assertEqual(c["tickets"], [])

    def test_two_tickets_in_one_description_are_both_kept_once(self):
        self.spawn(subagent_type="bug-gate", description="gate L-0002 and L-0001, then L-0002 again", prompt="x")
        self.assertEqual(self.lines()[0]["tickets"], ["L-0001", "L-0002"])

    def test_default_index_lives_in_the_scratch_home_never_the_real_one(self):
        env = {k: v for k, v in self.env.items() if k not in ("GATE_SPAWNS_LOG", "HUBKIT_HOME")}
        env["HOME"] = self.tmp
        self.assertEqual(self.spawn(env=env, subagent_type="builder", prompt="x").returncode, 0)
        self.assertTrue(os.path.exists(os.path.join(self.tmp, ".claude", "hubkit", "state", "gate-spawns.jsonl")))
        self.assertFalse(os.path.exists(self.idx))
        self.assertNotEqual(os.path.expanduser("~"), self.tmp)


class RoundCapCase(unittest.TestCase):
    spawn = GateIndexCase.spawn
    tearDown = GateIndexCase.tearDown

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.idx = os.path.join(self.tmp, "state", "gs.jsonl")
        self.env = dict(os.environ, MODEL_GUARD_LOG=os.path.join(self.tmp, "mg.log"),
                        GATE_SPAWNS_LOG=self.idx, PYTHONDONTWRITEBYTECODE="1")
        self.env.pop("MODEL_GUARD_ALLOWED", None)
        self.marker = os.path.join(self.tmp, "marker")
        self.env.update(GATE_CAP_MARKER=self.marker, GATE_CAP_LEDGER=os.path.join(self.tmp, "ledger.jsonl"))
        os.makedirs(os.path.dirname(self.idx), exist_ok=True)
        with open(self.idx, "w") as f:
            for i in range(3):
                f.write(json.dumps({"ts": "2026-01-01T00:00:0%d+00:00" % i, "subagent_type": "bug-gate",
                                    "primary": "L-0006", "tickets": ["L-0006"]}) + "\n")

    def gate(self, extra=""):
        return self.spawn(subagent_type="bug-gate", description="gate L-0006", prompt="Gate L-0006.\n" + extra)

    def age_marker(self, days):
        t = __import__("time").time() - days * 86400 - 5
        os.utime(self.marker, (t, t))

    def test_fourth_review_warns_first_then_blocks_after_7_days(self):
        r = self.gate()
        self.assertEqual(r.returncode, 0)
        self.assertIn("CUT-OR-CONTINUE", r.stdout)
        self.age_marker(6)
        self.assertEqual(self.gate().returncode, 0)
        self.age_marker(7)
        self.assertEqual(self.gate().returncode, 2)

    def test_no_marker_line_exempts_any_more(self):
        self.gate()
        self.age_marker(8)
        for extra in ("the judge reads this, second opinion", "REVIEW-MODE: for-and-against", "REVIEW-MODE: second-opinion",
                      "REVIEW-MODE: for-and-against\r\n", "```\nREVIEW-MODE: for-and-against\n```\n"):
            self.assertEqual(self.gate(extra).returncode, 2, repr(extra))

    def test_a_row_marked_exempt_still_counts_toward_the_cap(self):
        with open(self.idx, "w") as f:
            for i in range(3):
                f.write(json.dumps({"ts": "2026-01-01T00:00:0%d+00:00" % i, "subagent_type": "bug-gate",
                                    "primary": "L-0006", "tickets": ["L-0006"], "exempt": True}) + "\n")
        self.gate()
        self.age_marker(8)
        self.assertEqual(self.gate().returncode, 2)
        self.assertNotIn("GATE-ROUND-CAP-EXEMPT", open(os.path.join(self.tmp, "mg.log")).read())

    def test_lowercase_ticket_still_counts(self):
        self.gate()
        self.age_marker(8)
        r = self.spawn(subagent_type="bug-gate", description="gate it", prompt="Gate l-0006 now.\n")
        self.assertEqual(r.returncode, 2)

    def test_one_note_covers_three_reviews(self):
        self.gate()
        self.age_marker(8)
        with open(self.env["GATE_CAP_LEDGER"], "w") as f:
            f.write(json.dumps({"ts": "2026-06-01T00:00:00+00:00", "id": "L-0006", "event": "note",
                                "note": "CUT-OR-CONTINUE: continue"}) + "\n")
        # three earlier reviews are 2026-01-01, spawns the gate() above wrote are "now" (after the note)
        self.env["GATE_HARD_CEILING"] = "0"          # ceiling off: this test is about the note window alone
        self.assertEqual(self.gate().returncode, 0)

    def test_hard_ceiling_is_not_lifted_by_a_note(self):
        self.gate()                                    # 4th review, still in the 7-day warn-only window
        self.age_marker(8)
        with open(self.env["GATE_CAP_LEDGER"], "w") as f:
            f.write(json.dumps({"ts": "2026-06-01T00:00:00+00:00", "id": "L-0006", "event": "note",
                                "note": "CUT-OR-CONTINUE: continue"}) + "\n")
        r = self.gate()
        self.assertEqual(r.returncode, 2)              # 4 reviews already: the note no longer unlocks a 5th
        self.assertIn("ceiling 4 reached", r.stderr)
        for bad in ("abc", "-1", ""):                  # a bad value keeps the default of 4
            self.env["GATE_HARD_CEILING"] = bad
            self.assertEqual(self.gate().returncode, 2, bad)
        self.env["GATE_HARD_CEILING"] = "9"            # a raised ceiling lets the note work again
        self.assertEqual(self.gate().returncode, 0)

    def test_many_item_builder_brief_warns_at_7_not_6(self):
        def b(n):
            return self.spawn(subagent_type="builder", description="Build L-9%03d" % n,
                              prompt="\n".join("%d. thing" % i for i in range(1, n + 1)))
        self.assertNotIn("slices", b(6).stdout)
        self.assertIn("slices", b(7).stdout)


class ReportPathCase(unittest.TestCase):
    """The report-path rule: needs report-gate beside this folder (the repo layout) or under $HUBKIT_HOME."""
    GOOD = "Report: $HUBKIT_HOME/reports/job-1.md"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.env = dict(os.environ, MODEL_GUARD_LOG=os.path.join(self.tmp, "mg.log"), PYTHONDONTWRITEBYTECODE="1",
                        GATE_SPAWNS_LOG=os.path.join(self.tmp, "gs.jsonl"), MODEL_GUARD_REPORT_CHECK="1",
                        HUBKIT_HOME=os.path.join(self.tmp, "home"))
        self.env.pop("MODEL_GUARD_ALLOWED", None)
        # the hook under test with report-gate installed beside it, whatever HOOK points at
        self.base = os.path.join(self.tmp, "kit", "model-guard")
        os.makedirs(self.base)
        shutil.copy(HOOK, os.path.join(self.base, "model-guard.py"))
        self.rg_src = os.path.join(HERE, "..", "report-gate", "report-gate.py")
        self.rg_dst = os.path.join(self.tmp, "kit", "report-gate")
        os.makedirs(self.rg_dst)
        shutil.copy(self.rg_src, self.rg_dst)
        self.hook = os.path.join(self.base, "model-guard.py")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def spawn(self, stype, prompt, hook=None, env=None):
        tin = {"model": "sonnet", "description": "report probe", "prompt": prompt}
        if stype:
            tin["subagent_type"] = stype
        return subprocess.run([sys.executable, hook or self.hook], input=json.dumps({"tool_name": "Agent", "tool_input": tin}),
                              capture_output=True, text=True, env=env or self.env)

    def test_covered_types_without_a_path_are_refused_with_a_short_hint(self):
        for t in ("builder", "bug-gate", "playtester", "Builder", " bug-gate "):
            r = self.spawn(t, "do the thing")
            self.assertEqual(r.returncode, 2, t)
            self.assertIn("Report:", r.stderr)
            self.assertLess(len(r.stderr), 500)

    def test_a_good_path_or_report_none_passes(self):
        for t in ("builder", "bug-gate", "playtester"):
            self.assertEqual(self.spawn(t, "x\n" + self.GOOD).returncode, 0, t)
            self.assertEqual(self.spawn(t, "x\nReport: none").returncode, 0, t)
            self.assertEqual(self.spawn(t, "x\nReport: $HUBKIT_HOME/reports/a/b.md").returncode, 0, t)

    def test_other_types_and_a_missing_type_are_not_covered(self):
        for t in ("planner", "general-purpose", "Explore", None, ""):
            self.assertEqual(self.spawn(t, "no path here").returncode, 0, repr(t))

    def test_hostile_report_lines_are_refused_even_next_to_a_good_path(self):
        for bad in ("Report: $HUBKIT_HOME/reports/../../etc/x.md", "Report: /tmp/../reports/x.md",
                    "Report: $HUBKIT_HOME/reports/a.md\x07", "- Report: ../reports/x.md", "1. Report: /a/../reports/x.md",
                    "**Report:** ../reports/x.md"):
            r = self.spawn("builder", self.GOOD + "\n" + bad)
            self.assertEqual(r.returncode, 2, repr(bad))

    def test_empty_report_line_and_a_path_to_some_other_md_are_refused(self):
        self.assertEqual(self.spawn("builder", "Report:\nbuild it").returncode, 2)
        self.assertEqual(self.spawn("builder", "Report: notes.md\nbuild it").returncode, 2)
        self.assertEqual(self.spawn("builder", "see $HUBKIT_HOME/docs/readme.md").returncode, 2)

    def test_a_fenced_handoff_line_does_not_exempt(self):
        self.assertEqual(self.spawn("builder", "```\nHANDOFF-FROM: someone\n```\nbuild it").returncode, 2)

    def test_handoff_line_exempts(self):
        self.assertEqual(self.spawn("builder", "HANDOFF-FROM: earlier-owner\nbuild it").returncode, 0)

    def test_switch_off_and_the_model_rule_still_comes_first(self):
        for off in ("0", "false", "No", "OFF"):
            self.assertEqual(self.spawn("builder", "no path", env=dict(self.env, MODEL_GUARD_REPORT_CHECK=off)).returncode, 0, off)
        r = subprocess.run([sys.executable, self.hook], capture_output=True, text=True, env=self.env,
                           input=json.dumps({"tool_name": "Agent", "tool_input": {"subagent_type": "builder", "prompt": "x"}}))
        self.assertEqual(r.returncode, 2)
        self.assertIn("model", r.stderr)

    def test_a_refused_spawn_is_logged_and_not_indexed(self):
        self.spawn("builder", "no path")
        self.assertIn("BLOCKED report path builder", slurp(os.path.join(self.tmp, "mg.log")))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "gs.jsonl")))

    def test_without_report_gate_installed_nothing_changes(self):
        d = os.path.join(self.tmp, "solo", "model-guard")
        os.makedirs(d)
        shutil.copy(HOOK, d)
        self.assertEqual(self.spawn("builder", "no path at all", hook=os.path.join(d, "model-guard.py")).returncode, 0)

    def test_report_gate_under_hubkit_home_is_found(self):
        d = os.path.join(self.tmp, "solo2", "model-guard")
        os.makedirs(d)
        shutil.copy(HOOK, d)
        os.makedirs(os.path.join(self.tmp, "home", "report-gate"))
        shutil.copy(self.rg_src, os.path.join(self.tmp, "home", "report-gate"))
        self.assertEqual(self.spawn("builder", "no path", hook=os.path.join(d, "model-guard.py")).returncode, 2)

    def test_a_broken_report_gate_fails_open(self):
        with open(os.path.join(self.rg_dst, "report-gate.py"), "w") as f:
            f.write("raise RuntimeError('broken')\n")
        self.assertEqual(self.spawn("builder", "no path").returncode, 0)

    def test_a_report_gate_that_crashes_while_checking_fails_open(self):
        with open(os.path.join(self.rg_dst, "report-gate.py"), "w") as f:
            f.write("import re\nREPORT_PATH_RX = re.compile('x')\n\n\ndef _is_exempt(t):\n    raise ValueError('boom')\n")
        r = self.spawn("builder", "no path")
        self.assertEqual(r.returncode, 0)
        self.assertIn("REPORT fail-open", slurp(os.path.join(self.tmp, "mg.log")))


if __name__ == "__main__":
    unittest.main(verbosity=1)
