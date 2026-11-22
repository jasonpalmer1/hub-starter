#!/usr/bin/env python3
"""Tests for turn-cap.py. The hook is run as a subprocess the way Claude Code runs it:
a JSON event on stdin, exit code and output back. All state lives in a scratch dir."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
HOOK = os.environ.get("HOOK", os.path.join(HERE, "turn-cap.py"))


def sh(args, env, stdin):
    return subprocess.run([sys.executable, HOOK], input=stdin, capture_output=True, text=True, env=env)


class Base(unittest.TestCase):
    cap_env = {}

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.state = os.path.join(self.tmp, "turns")
        self.reports = os.path.join(self.tmp, "reports")
        os.makedirs(self.reports)
        self.env = dict(os.environ, TURN_CAP_STATE_DIR=self.state, TURN_CAP_REPORTS_DIR=self.reports,
                        PYTHONDONTWRITEBYTECODE="1")
        for k in ("TURN_CAP_NUDGE", "TURN_CAP_BLOCK", "TURN_CAP_EXEMPT", "HUBKIT_HOME"):
            self.env.pop(k, None)
        self.env.update(self.cap_env)

    def tearDown(self):
        for root, dirs, files in os.walk(self.tmp, topdown=False):
            for f in files:
                p = os.path.join(root, f)
                try:
                    os.chmod(p, 0o644)
                except OSError:
                    pass
                os.remove(p)
            for d in dirs:
                os.rmdir(os.path.join(root, d))
        os.rmdir(self.tmp)

    def pre(self, aid, atype="builder", tool="Bash", fp=None, env=None):
        ti = {"file_path": fp} if fp else {}
        ev = {"hook_event_name": "PreToolUse", "agent_id": aid, "agent_type": atype,
              "tool_name": tool, "tool_input": ti}
        return sh(None, env or self.env, json.dumps(ev))

    def post(self, aid, atype="builder"):
        ev = {"hook_event_name": "PostToolUse", "agent_id": aid, "agent_type": atype,
              "tool_name": "Bash", "tool_input": {}}
        return sh(None, self.env, json.dumps(ev))

    def size(self, aid):
        return os.stat(os.path.join(self.state, aid)).st_size


class DefaultCaps(Base):
    def test_blocks_at_115_exactly(self):
        first = 0
        for i in range(1, 121):
            if self.pre("A1").returncode == 2 and not first:
                first = i
        self.assertEqual(first, 115)

    def test_block_message_goes_to_stderr_and_pass_path_is_silent(self):
        r = self.pre("A2")
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))
        for _ in range(114):
            self.pre("A2")
        r = self.pre("A2")
        self.assertEqual(r.returncode, 2)
        self.assertIn("turn cap reached", r.stderr)

    def test_nudge_threshold_is_90(self):
        for _ in range(89):
            self.pre("A3")
        self.assertNotIn("TURN CAP NUDGE", self.post("A3").stdout)
        self.pre("A3")
        self.assertIn("TURN CAP NUDGE", self.post("A3").stdout)


class SaveMessage(Base):
    def test_step_60_message_once_and_never_blocks(self):
        outs = []
        for _ in range(70):
            self.pre("S1")
            outs.append(self.post("S1").stdout)
        hits = [i for i, o in enumerate(outs) if "STEP-60 CHECK" in o]
        self.assertEqual(hits, [59])  # exactly at the 60th call, once
        self.assertTrue(all(o == "" for i, o in enumerate(outs) if i != 59))

    def test_exempt_types_get_no_step_60_message(self):
        self.env["TURN_CAP_EXEMPT"] = "bug-gate,playtester,planner"
        for _ in range(65):
            self.pre("S2", atype="bug-gate")
        self.assertEqual(self.post("S2", atype="bug-gate").stdout, "")


class PollCredit(Base):
    POLL = "until test -f /tmp/done; do sleep 5; done"

    def call(self, aid, tool="Bash", **ti):
        ev = {"hook_event_name": "PreToolUse", "agent_id": aid, "agent_type": "builder",
              "tool_name": tool, "tool_input": ti}
        return sh(None, self.env, json.dumps(ev))

    def credits(self, aid):
        try:
            return os.stat(os.path.join(self.state, aid + ".credits")).st_size
        except OSError:
            return 0

    def test_pure_poll_and_monitor_earn_a_credit(self):
        self.call("C1", command=self.POLL, timeout=250000)
        self.call("C1", tool="Monitor")
        self.assertEqual(self.credits("C1"), 2)

    def test_hostile_spellings_earn_nothing(self):
        for ti in ({"command": self.POLL},                                           # no timeout
                   {"command": self.POLL, "timeout": 600000},                        # too long
                   {"command": self.POLL, "timeout": True},                          # bool timeout
                   {"command": self.POLL, "timeout": 250000, "run_in_background": True},
                   {"command": "sleep 200", "timeout": 250000},                      # bare sleep
                   {"command": "until x; do sleep 5; done; rm -rf y", "timeout": 250000},
                   {"command": "echo hi && until x; do sleep 5; done", "timeout": 250000},
                   {"command": "until x; do sleep 5; done\nrm y", "timeout": 250000},
                   {"command": "until $(rm y); do sleep 5; done", "timeout": 250000},
                   {"command": 7, "timeout": 250000}):
            self.call("C2", **ti)
        self.assertEqual(self.credits("C2"), 0)
        self.assertEqual(self.size("C2"), 10)

    def test_credits_are_capped_at_40(self):
        for _ in range(45):
            self.call("C3", command=self.POLL, timeout=250000)
        self.assertEqual(self.credits("C3"), 40)

    def test_credits_defer_the_nudge_but_never_the_block(self):
        for _ in range(60):
            self.call("C4", command=self.POLL, timeout=250000)
        for _ in range(35):
            self.call("C4")
        self.assertEqual(self.size("C4"), 95)  # past 90 calls, 60 credits: no nudge yet
        self.assertEqual(self.post("C4").stdout, "")
        for _ in range(10):
            self.call("C4")                    # 105 calls: the safety floor nudges anyway
        self.assertIn("TURN CAP NUDGE", self.post("C4").stdout)
        rc = [self.call("C4").returncode for _ in range(15)]
        self.assertIn(2, rc)                   # block still lands at 115


class SmallCaps(Base):
    cap_env = {"TURN_CAP_NUDGE": "3", "TURN_CAP_BLOCK": "5"}

    def fill(self, aid, n):
        for _ in range(n):
            self.pre(aid)

    def test_handoff_and_report_writes_pass_past_the_cap(self):
        self.fill("B1", 9)
        h = os.path.join(self.reports, "L-0001", "handoff.md")
        self.assertEqual(self.pre("B1", tool="Write", fp=h).returncode, 0)
        self.assertEqual(self.pre("B1", tool="Edit", fp=h).returncode, 0)

    def test_other_tools_and_paths_blocked_past_the_cap(self):
        self.fill("B2", 9)
        self.assertEqual(self.pre("B2", tool="Bash").returncode, 2)
        self.assertEqual(self.pre("B2", tool="Read", fp=os.path.join(self.reports, "x.md")).returncode, 2)
        self.assertEqual(self.pre("B2", tool="Write", fp=os.path.join(self.tmp, "other.md")).returncode, 2)

    def test_traversal_out_of_reports_is_blocked(self):
        self.fill("B3", 9)
        sneaky = os.path.join(self.reports, "..", "settings.json")
        self.assertEqual(self.pre("B3", tool="Write", fp=sneaky).returncode, 2)

    def test_sibling_folder_with_same_prefix_is_blocked(self):
        self.fill("B4", 9)
        sib = self.reports + "-evil"
        os.makedirs(sib)
        try:
            self.assertEqual(self.pre("B4", tool="Write", fp=os.path.join(sib, "x.md")).returncode, 2)
        finally:
            os.rmdir(sib)

    def test_symlink_out_of_reports_is_blocked(self):
        self.fill("B5", 9)
        link = os.path.join(self.reports, "escape")
        os.symlink(self.tmp, link)
        try:
            self.assertEqual(self.pre("B5", tool="Write", fp=os.path.join(link, "x.md")).returncode, 2)
        finally:
            os.remove(link)

    def test_write_with_no_path_is_blocked(self):
        self.fill("B6", 9)
        self.assertEqual(self.pre("B6", tool="Write").returncode, 2)

    def test_under_the_cap_nothing_is_blocked(self):
        for _ in range(4):
            self.assertEqual(self.pre("B7").returncode, 0)

    def test_exempt_types_run_past_the_cap(self):
        self.env["TURN_CAP_EXEMPT"] = "bug-gate,playtester,planner"
        for t in ("bug-gate", "playtester", "planner"):
            for _ in range(12):
                self.assertEqual(self.pre("E-" + t, atype=t).returncode, 0)
            self.assertFalse(os.path.exists(os.path.join(self.state, "E-" + t)))

    def test_exempt_list_is_configurable(self):
        self.env["TURN_CAP_EXEMPT"] = "builder"
        for _ in range(12):
            self.assertEqual(self.pre("C1").returncode, 0)
        self.env["TURN_CAP_EXEMPT"] = "bug-gate"
        for _ in range(12):
            r = self.pre("C2")
        self.assertEqual(r.returncode, 2)

    def test_main_chat_without_agent_id_is_never_counted(self):
        for _ in range(12):
            r = sh(None, self.env, json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Bash"}))
            self.assertEqual(r.returncode, 0)
        self.assertFalse(os.path.exists(self.state))

    def test_workers_are_counted_separately(self):
        self.fill("D1", 4)
        self.assertEqual(self.pre("D2").returncode, 0)
        self.assertEqual(self.size("D1"), 4)
        self.assertEqual(self.size("D2"), 1)

    def test_agent_id_is_sanitised(self):
        self.pre("../../escape")
        self.assertIn("escape", os.listdir(self.state))
        self.assertTrue(all(os.sep not in n for n in os.listdir(self.state)))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "escape")))

    def test_agent_id_with_nothing_safe_is_ignored(self):
        self.assertEqual(self.pre("///").returncode, 0)

    def test_parallel_calls_do_not_lose_counts(self):
        procs = []
        for i in range(30):
            for aid in ("P1", "P2"):
                ev = json.dumps({"hook_event_name": "PreToolUse", "agent_id": aid, "agent_type": "builder",
                                 "tool_name": "Write", "tool_input": {"file_path": os.path.join(self.reports, "h.md")}})
                p = subprocess.Popen([sys.executable, HOOK], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, env=self.env, text=True)
                p.stdin.write(ev)
                p.stdin.close()
                procs.append(p)
        for p in procs:
            p.wait()
        self.assertEqual(self.size("P1"), 30)
        self.assertEqual(self.size("P2"), 30)

    # -- nudge
    def test_nudge_fires_once_at_the_threshold(self):
        self.fill("N1", 2)
        self.assertEqual(self.post("N1").stdout, "")
        self.fill("N1", 1)
        out = self.post("N1").stdout
        data = json.loads(out)
        self.assertEqual(data["hookSpecificOutput"]["hookEventName"], "PostToolUse")
        self.assertIn("handoff.md", data["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(self.post("N1").stdout, "")

    def test_per_type_caps_and_handback_paths(self):
        self.env.pop("TURN_CAP_BLOCK", None)
        self.env.pop("TURN_CAP_NUDGE", None)
        self.env["TURN_CAP_CAPS"] = "judge=3"
        for _ in range(3):
            r = self.pre("J1", atype="judge")
        self.assertEqual(r.returncode, 2)                                  # judge cap 3 reached
        self.assertEqual(self.pre("J1", atype="judge", tool="SubagentHandback").returncode, 0)
        self.assertEqual(self.pre("J1", atype="judge", tool="Bash").returncode, 2)
        for _ in range(5):                                                 # a type not in the table gets 115
            self.assertEqual(self.pre("J2", atype="other-worker").returncode, 0)

    def test_nudge_does_not_fire_for_exempt_or_main(self):
        self.env["TURN_CAP_EXEMPT"] = "bug-gate,playtester,planner"
        self.fill("N2", 6)
        self.assertEqual(self.post("N2", atype="planner").stdout, "")

    # -- fail open
    def test_corrupt_and_empty_input_fail_open(self):
        for raw in ("garbage{", "", "[]", "null"):
            self.assertEqual(sh(None, self.env, raw).returncode, 0, raw)

    def test_unusable_state_dir_fails_open(self):
        env = dict(self.env, TURN_CAP_STATE_DIR="/dev/null/x")
        for _ in range(8):
            self.assertEqual(self.pre("F1", env=env).returncode, 0)

    def test_unwritable_counter_fails_open(self):
        os.makedirs(self.state)
        p = os.path.join(self.state, "F2")
        open(p, "w").close()
        os.chmod(p, 0)
        if os.geteuid() == 0:
            self.skipTest("root ignores file modes")
        self.assertEqual(self.pre("F2").returncode, 0)

    def test_post_with_no_counter_file_fails_open(self):
        self.assertEqual(self.post("NOFILE").returncode, 0)

    def test_bad_cap_settings_use_defaults(self):
        self.env["TURN_CAP_BLOCK"] = "not-a-number"
        for _ in range(10):
            self.assertEqual(self.pre("G1").returncode, 0)

    # -- prune
    def test_old_state_is_pruned_and_fresh_kept(self):
        os.makedirs(self.state)
        old = os.path.join(self.state, "OLD")
        open(old, "w").close()
        os.utime(old, (1577836800, 1577836800))
        self.pre("H0")
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(os.path.join(self.state, "H0")))

    def test_prune_runs_at_most_once_an_hour(self):
        self.pre("I0")  # creates the marker
        os.makedirs(self.state, exist_ok=True)
        old = os.path.join(self.state, "OLD2")
        open(old, "w").close()
        os.utime(old, (1577836800, 1577836800))
        self.pre("I1")
        self.assertTrue(os.path.exists(old))


class AgentFile(unittest.TestCase):
    def test_builder_agent_wires_both_hooks_and_names_a_model(self):
        with open(os.path.join(HERE, "builder.md")) as f:
            text = f.read()
        head = text.split("---")[1]
        self.assertIn("model: sonnet", head)
        self.assertEqual(head.count("turn-cap.py"), 2)
        self.assertIn("PreToolUse:", head)
        self.assertIn("PostToolUse:", head)
        self.assertIn("handoff.md", text)
        self.assertIn("115", text)

    def test_readme_says_user_scope_is_required_for_frontmatter_hooks(self):
        with open(os.path.join(HERE, "README.md")) as f:
            text = f.read()
        self.assertIn("~/.claude/agents", text)
        self.assertIn("not yet been", text)       # project-scope agents are skipped until trusted
        self.assertIn("skipped", text)


if __name__ == "__main__":
    unittest.main(verbosity=1)
