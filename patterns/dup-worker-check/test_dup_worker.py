#!/usr/bin/env python3
"""Tests for dup-worker-check.py and dup-worker-hook.py. Fake worker transcripts live in a
scratch folder; nothing on the real machine is read."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
CHECK = os.environ.get("CHECK", os.path.join(HERE, "dup-worker-check.py"))
HOOK = os.environ.get("HOOK", os.path.join(HERE, "dup-worker-hook.py"))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "projects")
        self.env = dict(os.environ, DUP_PROJECTS_ROOT=self.root, PYTHONDONTWRITEBYTECODE="1")
        for k in ("DUP_LIVE_MIN", "DUP_TICKET_RE", "DUP_CHECK_BIN"):
            self.env.pop(k, None)

    def tearDown(self):
        for root, dirs, files in os.walk(self.tmp, topdown=False):
            for f in files:
                os.remove(os.path.join(root, f))
            for d in dirs:
                os.rmdir(os.path.join(root, d))
        os.rmdir(self.tmp)

    def mk(self, n, agent, desc, brief, last_stop, age_min=0.0, meta=True):
        """A fake worker transcript: a brief, then one assistant message with this stop_reason."""
        d = os.path.join(self.root, "proj", "sess%s" % n, "subagents")
        os.makedirs(d, exist_ok=True)
        if meta:
            with open(os.path.join(d, "agent-%s.meta.json" % agent), "w") as f:
                json.dump({"description": desc}, f)
        p = os.path.join(d, "agent-%s.jsonl" % agent)
        with open(p, "w") as f:
            f.write(json.dumps({"type": "user", "message": {"role": "user", "content": brief}}) + "\n")
            f.write(json.dumps({"type": "assistant", "message": {"role": "assistant",
                    "stop_reason": last_stop, "content": [{"type": "text", "text": "x"}]}}) + "\n")
        if age_min:
            t = time.time() - age_min * 60
            os.utime(p, (t, t))
        return p

    def check(self, description, prompt, env=None):
        r = subprocess.run([sys.executable, CHECK], input=json.dumps({"description": description, "prompt": prompt}),
                           capture_output=True, text=True, env=env or self.env)
        return r

    def hook(self, tin, env=None, tool="Agent"):
        return subprocess.run([sys.executable, HOOK], input=json.dumps({"tool_name": tool, "tool_input": tin}),
                              capture_output=True, text=True, env=env or self.env)


class Checker(Base):
    def test_live_holder_clashes(self):
        self.mk(1, "aaa", "L-0001 plan", "Ticket L-0001: do it", "tool_use")
        r = self.check("x", "work L-0001 now")
        self.assertEqual(r.returncode, 3)
        self.assertIn("L-0001", r.stdout)
        self.assertIn("aaa", r.stdout)
        self.assertIn("L-0001 plan", r.stdout)

    def test_finished_worker_does_not_hold(self):
        self.mk(2, "bbb", "L-0002 done", "Ticket L-0002", "end_turn")
        self.assertEqual(self.check("x", "work L-0002 now").returncode, 0)

    def test_stale_worker_does_not_hold(self):
        self.mk(3, "ccc", "L-0003 old", "Ticket L-0003", "tool_use", age_min=60)
        self.assertEqual(self.check("x", "work L-0003 now").returncode, 0)

    def test_live_window_is_configurable(self):
        self.mk(3, "ccc", "L-0003 old", "Ticket L-0003", "tool_use", age_min=60)
        env = dict(self.env, DUP_LIVE_MIN="120")
        self.assertEqual(self.check("x", "work L-0003 now", env).returncode, 3)
        env = dict(self.env, DUP_LIVE_MIN="not-a-number")
        self.assertEqual(self.check("x", "work L-0003 now", env).returncode, 0)  # falls back to 15

    def test_deep_mention_in_a_long_brief_is_not_primary(self):
        self.mk(1, "aaa", "L-0001 plan", "Ticket L-0001: do it", "tool_use")
        padding = "z" * 320
        self.assertEqual(self.check("x", padding + " L-0001").returncode, 0)

    def test_holders_deep_mentions_are_not_primary_either(self):
        padding = "z" * 320
        self.mk(1, "aaa", "plain description", padding + " L-0004", "tool_use")
        self.assertEqual(self.check("x", "work L-0004").returncode, 0)

    def test_ids_on_dup_ok_and_exclusion_lines_are_context_not_claims(self):
        self.mk(1, "aaa", "L-0001 plan", "Ticket L-0001: do it", "tool_use")
        for prompt in ("work on the docs\nDUP-OK: second opinion on L-0001",
                       "work on the docs\nExclusions: L-0001 and L-0002",
                       "work on the docs\n> Exclude: L-0001",
                       "work on the docs\n- excluded: L-0001"):
            self.assertEqual(self.check("x", prompt).returncode, 0, prompt)
        # a line that merely contains the word later in the sentence is still a claim
        self.assertEqual(self.check("x", "work on L-0001, not an exclusion: L-0009").returncode, 3)
        self.assertEqual(self.check("x", "work\nL-0001 is mine").returncode, 3)

    def test_holders_exclusion_lines_do_not_hold_either(self):
        self.mk(1, "aaa", "plain description", "work on the docs\nExclusions: L-0004", "tool_use")
        self.assertEqual(self.check("x", "work L-0004").returncode, 0)

    def test_description_counts_as_primary(self):
        self.mk(1, "aaa", "L-0005 work", "no id in the brief itself", "tool_use")
        self.assertEqual(self.check("L-0005 again", "something else").returncode, 3)

    def test_brief_with_no_ticket_never_clashes(self):
        self.mk(1, "aaa", "L-0001 plan", "Ticket L-0001", "tool_use")
        self.assertEqual(self.check("tidy up", "no ticket here").returncode, 0)

    def test_different_tickets_do_not_clash(self):
        self.mk(1, "aaa", "L-0001 plan", "Ticket L-0001", "tool_use")
        self.assertEqual(self.check("x", "work L-0002").returncode, 0)

    def test_one_line_per_clash(self):
        self.mk(1, "aaa", "L-0001 and L-0002", "Tickets L-0001 and L-0002", "tool_use")
        self.mk(2, "bbb", "L-0002", "Ticket L-0002", "tool_use")
        r = self.check("x", "do L-0001 and L-0002")
        self.assertEqual(r.returncode, 3)
        self.assertEqual(len(r.stdout.strip().splitlines()), 3)

    def test_workers_in_other_sessions_are_seen(self):
        self.mk(7, "zzz", "L-0006", "Ticket L-0006", "tool_use")
        r = self.check("x", "work L-0006")
        self.assertEqual(r.returncode, 3)
        self.assertIn("sess7", r.stdout)

    def test_missing_meta_file_still_works(self):
        self.mk(1, "aaa", "unused", "Ticket L-0001", "tool_use", meta=False)
        self.assertEqual(self.check("x", "work L-0001").returncode, 3)

    def test_worker_that_is_waiting_on_a_tool_result_is_live(self):
        # last message is a user (tool result) turn: not finished
        p = self.mk(1, "aaa", "L-0001", "Ticket L-0001", "end_turn")
        with open(p, "a") as f:
            f.write(json.dumps({"type": "user", "message": {"role": "user", "content": "tool result"}}) + "\n")
        self.assertEqual(self.check("x", "work L-0001").returncode, 3)

    def test_custom_ticket_shape(self):
        self.mk(1, "aaa", "TASK-77", "Please do TASK-77", "tool_use")
        env = dict(self.env, DUP_TICKET_RE=r"\bTASK-\d+\b")
        self.assertEqual(self.check("x", "do TASK-77", env).returncode, 3)
        self.assertEqual(self.check("x", "do TASK-78", env).returncode, 0)

    def test_broken_custom_shape_falls_back_to_default(self):
        self.mk(1, "aaa", "L-0001", "Ticket L-0001", "tool_use")
        env = dict(self.env, DUP_TICKET_RE="([unclosed")
        self.assertEqual(self.check("x", "work L-0001", env).returncode, 3)

    def test_garbage_and_empty_input_exit_zero(self):
        for raw in ("", "not json", "[]", "null"):
            r = subprocess.run([sys.executable, CHECK], input=raw, capture_output=True, text=True, env=self.env)
            self.assertEqual(r.returncode, 0, repr(raw))

    def test_no_projects_folder_is_fine(self):
        self.assertEqual(self.check("x", "work L-0001").returncode, 0)

    def test_corrupt_transcript_is_skipped_not_fatal(self):
        d = os.path.join(self.root, "proj", "sess9", "subagents")
        os.makedirs(d)
        with open(os.path.join(d, "agent-bad.jsonl"), "w") as f:
            f.write("{{{ not json\n\x00\x01\n")
        self.mk(1, "aaa", "L-0001", "Ticket L-0001", "tool_use")
        self.assertEqual(self.check("x", "work L-0001").returncode, 3)


class Hook(Base):
    def setUp(self):
        super().setUp()
        self.mk(1, "aaa", "L-0001 plan", "Ticket L-0001: do it", "tool_use")

    def test_blocks_a_clash_and_says_how_to_override(self):
        r = self.hook({"description": "L-0001 again", "prompt": "build L-0001", "model": "sonnet"})
        self.assertEqual(r.returncode, 2)
        self.assertIn("L-0001", r.stderr)
        self.assertIn("DUP-OK", r.stderr)

    def test_task_tool_is_also_checked(self):
        r = self.hook({"description": "x", "prompt": "build L-0001"}, tool="Task")
        self.assertEqual(r.returncode, 2)

    def test_dup_ok_line_with_reason_overrides(self):
        r = self.hook({"description": "L-0001 verify",
                       "prompt": "build L-0001\nDUP-OK: independent check of the first worker\nthanks"})
        self.assertEqual(r.returncode, 0)

    def test_dup_ok_mentioned_in_passing_does_not_override(self):
        for prompt in ("build L-0001 DUP-OK",
                       "build L-0001. Never use the DUP-OK escape here",
                       "build L-0001\nDUP-OK:",
                       "build L-0001\nnote DUP-OK: because"):
            r = self.hook({"description": "L-0001 verify", "prompt": prompt})
            self.assertEqual(r.returncode, 2, prompt)

    def test_dup_ok_must_start_the_line_outside_a_code_fence_with_a_real_reason(self):
        for prompt in ("build L-0001\n  DUP-OK: indented reason",
                       "build L-0001\n\tDUP-OK: tabbed reason",
                       "build L-0001\n```\nDUP-OK: pasted brief\n```\n",
                       "build L-0001\n  ```\nDUP-OK: pasted brief\n  ```\n",
                       "build L-0001\n~~~\nDUP-OK: pasted brief\n~~~",
                       "build L-0001\n```\nDUP-OK: fence never closed",
                       "build L-0001\nDUP-OK: <reason>"):
            r = self.hook({"description": "L-0001 verify", "prompt": prompt})
            self.assertEqual(r.returncode, 2, prompt)

    def test_a_fence_closes_only_on_the_same_character_and_at_least_the_same_length(self):
        # each of these is ONE fence that is still open at the DUP-OK line, so the override must not count
        for prompt in ("build L-0001\n````\n```\nDUP-OK: pasted brief\n````",          # shorter fence inside a longer one
                       "build L-0001\n~~~~~\n~~~\nDUP-OK: pasted brief\n~~~~~",
                       "build L-0001\n```\n~~~\nDUP-OK: pasted brief\n```",              # other character is content
                       "build L-0001\n~~~\n```\nDUP-OK: pasted brief\n~~~",
                       "build L-0001\n```\n```python\nDUP-OK: pasted brief\n```",        # a closing fence has nothing after it
                       "build L-0001\n```\r\n```x\r\nDUP-OK: pasted brief\r\n```\r\n"):
            r = self.hook({"description": "L-0001 verify", "prompt": prompt})
            self.assertEqual(r.returncode, 2, prompt)

    def test_a_longer_or_padded_closing_fence_still_closes(self):
        for prompt in ("build L-0001\n```\ncode\n````\nDUP-OK: a second opinion",
                       "build L-0001\n````\ncode\n````   \nDUP-OK: a second opinion",
                       "build L-0001\n~~~\ncode\n  ~~~\nDUP-OK: a second opinion",
                       "build L-0001\r\n```\r\ncode\r\n```\r\nDUP-OK: a second opinion"):
            r = self.hook({"description": "L-0001 verify", "prompt": prompt})
            self.assertEqual(r.returncode, 0, prompt)

    def test_two_backticks_are_not_a_fence(self):
        r = self.hook({"description": "L-0001 verify",
                       "prompt": "build L-0001\n``inline``\nDUP-OK: a second opinion"})
        self.assertEqual(r.returncode, 0)

    def test_dup_ok_after_a_closed_fence_still_works(self):
        r = self.hook({"description": "L-0001 verify",
                       "prompt": "build L-0001\n```\ncode\n```\nDUP-OK: a second opinion"})
        self.assertEqual(r.returncode, 0)

    def test_the_refusal_text_does_not_contain_a_working_override(self):
        r = self.hook({"description": "L-0001 again", "prompt": "build L-0001"})
        self.assertIn("DUP-OK:", r.stderr)
        # pasting the refusal back into a brief must not disable the guard
        r2 = self.hook({"description": "L-0001 again", "prompt": "build L-0001\n" + r.stderr})
        self.assertEqual(r2.returncode, 2)

    def test_no_clash_passes(self):
        self.assertEqual(self.hook({"description": "x", "prompt": "build L-0002"}).returncode, 0)

    def test_other_tools_are_ignored(self):
        r = self.hook({"description": "x", "prompt": "L-0001"}, tool="Bash")
        self.assertEqual(r.returncode, 0)

    def test_missing_checker_fails_open(self):
        env = dict(self.env, DUP_CHECK_BIN=os.path.join(self.tmp, "nope.py"))
        self.assertEqual(self.hook({"description": "x", "prompt": "build L-0001"}, env).returncode, 0)

    def test_crashing_checker_fails_open(self):
        bad = os.path.join(self.tmp, "bad.py")
        with open(bad, "w") as f:
            f.write("import sys\nsys.exit(1)\n")
        env = dict(self.env, DUP_CHECK_BIN=bad)
        self.assertEqual(self.hook({"description": "x", "prompt": "build L-0001"}, env).returncode, 0)

    def test_slow_checker_fails_open(self):
        slow = os.path.join(self.tmp, "slow.py")
        with open(slow, "w") as f:
            f.write("import time\ntime.sleep(10)\n")
        env = dict(self.env, DUP_CHECK_BIN=slow)
        t0 = time.time()
        self.assertEqual(self.hook({"description": "x", "prompt": "build L-0001"}, env).returncode, 0)
        self.assertLess(time.time() - t0, 8)

    def test_garbage_input_fails_open(self):
        for raw in ("", "nope", "[]"):
            r = subprocess.run([sys.executable, HOOK], input=raw, capture_output=True, text=True, env=self.env)
            self.assertEqual(r.returncode, 0, repr(raw))

    def test_exit_3_with_no_output_does_not_block(self):
        odd = os.path.join(self.tmp, "odd.py")
        with open(odd, "w") as f:
            f.write("import sys\nsys.exit(3)\n")
        env = dict(self.env, DUP_CHECK_BIN=odd)
        self.assertEqual(self.hook({"description": "x", "prompt": "build L-0001"}, env).returncode, 0)


if __name__ == "__main__":
    unittest.main(verbosity=1)
