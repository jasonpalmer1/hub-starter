"""Tests for sweep.py. Run through test.sh (scratch HOME). Python 3.9, stdlib only."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.environ.get("SWEEP") or os.path.join(HERE, "sweep.py")


def iso(age_s):
    return (datetime.now(timezone.utc) - timedelta(seconds=age_s)).strftime("%Y-%m-%dT%H:%M:%SZ")


def created(tid, owner="unowned", age=3600, asked_by="hub", status=None):
    out = [{"event": "created", "id": tid, "title": "t " + tid, "ts": iso(age), "asked_by": asked_by, "owner": owner}]
    if status:
        out.append({"event": "status", "id": tid, "status": status, "ts": iso(age)})
    return out


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.hk = os.path.join(self.tmp.name, "hk")
        self.state = os.path.join(self.hk, "state")
        os.makedirs(self.state)
        self.ledger = os.path.join(self.state, "ledger.jsonl")
        self.live = os.path.join(self.state, "live-owners.txt")
        self.env = dict(os.environ, HUBKIT_HOME=self.hk, PYTHONDONTWRITEBYTECODE="1")
        for k in ("LEDGER_PATH", "SWEEP_LIVE_OWNERS", "SWEEP_NOW"):
            self.env.pop(k, None)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, *groups, raw=()):
        with open(self.ledger, "w") as f:
            for g in groups:
                for e in g:
                    f.write(json.dumps(e) + "\n")
            for r in raw:
                f.write(r + "\n")

    def set_live(self, *names, at=None, header=True, end=True):
        body = ""
        if header:
            body += "# live-owners %d\n" % (time.time() - 60 if at is None else at)
        body += "".join(n + "\n" for n in names)
        if end:
            body += "# end\n"
        with open(self.live, "w") as f:
            f.write(body)

    def run_tool(self, *args, env=None):
        return subprocess.run([sys.executable, TOOL] + list(args), capture_output=True, text=True, env=env or self.env)

    def folded(self):
        sys.path.insert(0, os.path.join(HERE, "..", "ledger"))
        import ledger_lib
        with open(self.ledger) as f:
            return ledger_lib.fold([json.loads(l) for l in f if l.strip()])

    def events(self):
        p = os.path.join(self.state, "events.jsonl")
        return [json.loads(l) for l in open(p)] if os.path.exists(p) else []

    def ledger_text(self):
        return open(self.ledger).read()


class Reassign(Base):
    def test_dead_owner_ticket_goes_back_to_the_queue(self):
        self.write(created("L-0001", "lane-dead", status="active"))
        self.set_live("lane-a")
        r = self.run_tool("orphans", "--reassign")
        self.assertEqual(r.returncode, 0, r.stderr)
        rec = self.folded()["L-0001"]
        self.assertEqual((rec["status"], rec["owner"]), ("open", "unowned"))
        self.assertIn("orphans.reassigned", [e["event"] for e in self.events()])

    def test_dead_owner_without_reassign_flag_changes_nothing(self):
        self.write(created("L-0001", "lane-dead", status="active"))
        self.set_live("lane-a")
        before = self.ledger_text()
        r = self.run_tool("orphans")
        self.assertIn("L-0001 dead owner", r.stdout)
        self.assertEqual(self.ledger_text(), before)

    def test_alive_owner_is_never_taken_from(self):
        self.write(created("L-0001", "lane-a", status="active"))
        self.set_live("lane-a", "lane-b")
        before = self.ledger_text()
        self.run_tool("orphans", "--reassign")
        self.assertEqual(self.ledger_text(), before)

    def test_alive_owner_name_case_and_space_insensitive(self):
        self.write(created("L-0001", " Lane-A ", status="active"))
        self.set_live("lane-a")
        before = self.ledger_text()
        self.run_tool("orphans", "--reassign")
        self.assertEqual(self.ledger_text(), before)

    def test_prefix_lane_1_dead_lane_10_alive_and_the_reverse(self):
        self.write(created("L-0001", "lane-1", status="active"), created("L-0002", "lane-10", status="active"))
        self.set_live("lane-10")
        self.run_tool("orphans", "--reassign")
        recs = self.folded()
        self.assertEqual(recs["L-0001"]["owner"], "unowned")   # lane-1 is dead
        self.assertEqual(recs["L-0002"]["owner"], "lane-10")   # lane-10 untouched
        self.write(created("L-0001", "lane-1", status="active"), created("L-0002", "lane-10", status="active"))
        self.set_live("lane-1")
        self.run_tool("orphans", "--reassign")
        recs = self.folded()
        self.assertEqual(recs["L-0001"]["owner"], "lane-1")
        self.assertEqual(recs["L-0002"]["owner"], "unowned")

    def test_protected_and_unowned_and_closed_tickets_are_left_alone(self):
        self.write(created("L-0001", "operator", status="active"), created("L-0002"),
                   created("L-0003", "lane-dead", status="needs-operator"),
                   created("L-0004", "lane-dead", status="dropped"),
                   created("L-0005", "lane-dead", status="open"))
        self.set_live("lane-a")
        self.run_tool("orphans", "--reassign")
        recs = self.folded()
        for t in ("L-0001", "L-0003", "L-0004"):
            self.assertNotEqual(recs[t]["owner"], "unowned", t)
        self.assertEqual(recs["L-0005"]["owner"], "unowned")   # an open ticket stuck on a dead owner goes back too

    def test_held_lane_keeps_its_tickets_until_the_hold_ends(self):
        self.write(created("L-0001", "lane-dead", status="active"))
        self.set_live("lane-a")
        self.assertEqual(self.run_tool("hold", "--lane", "lane-dead", "--minutes", "60").returncode, 0)
        before = self.ledger_text()
        self.run_tool("orphans", "--reassign")
        self.assertEqual(self.ledger_text(), before)
        self.run_tool("hold", "--lane", "lane-dead", "--clear")
        self.run_tool("orphans", "--reassign")
        self.assertEqual(self.folded()["L-0001"]["owner"], "unowned")

    def test_expired_hold_no_longer_protects(self):
        self.write(created("L-0001", "lane-dead", status="active"))
        self.set_live("lane-a")
        os.makedirs(os.path.join(self.state, "holds"))
        json.dump({"until": time.time() - 5}, open(os.path.join(self.state, "holds", "lane-dead.json"), "w"))
        self.run_tool("orphans", "--reassign")
        self.assertEqual(self.folded()["L-0001"]["owner"], "unowned")

    def test_hold_always_expires_and_rejects_bad_input(self):
        for args in (["--minutes", "0"], ["--minutes", "99999"], [], ["--minutes", "5", "--lane", "a b;"]):
            r = self.run_tool(*(["hold", "--lane", "x"] + args if "--lane" not in args else ["hold"] + args))
            self.assertEqual(r.returncode, 2, args)
        self.assertFalse(os.path.exists(os.path.join(self.state, "holds", "x.json")))

    def test_unreadable_hold_file_counts_as_a_hold(self):
        self.write(created("L-0001", "lane-dead", status="active"))
        self.set_live("lane-a")
        os.makedirs(os.path.join(self.state, "holds"))
        open(os.path.join(self.state, "holds", "lane-dead.json"), "w").write("{not json")
        before = self.ledger_text()
        self.run_tool("orphans", "--reassign")
        self.assertEqual(self.ledger_text(), before)

    def test_claim_made_after_the_live_list_was_written_is_not_taken(self):
        self.write(created("L-0001", "lane-new", age=700, status="active"))
        self.set_live("lane-a", at=time.time() - 1500)   # list older than the claim; lane-new may simply be missing
        before = self.ledger_text()
        self.run_tool("orphans", "--reassign")
        self.assertEqual(self.ledger_text(), before)

    def test_fresh_ticket_under_ten_minutes_is_not_taken(self):
        self.write(created("L-0001", "lane-dead", age=300, status="active"))
        self.set_live("lane-a", at=time.time() - 10)   # list newer than ticket, but ticket too young
        before = self.ledger_text()
        self.run_tool("orphans", "--reassign")
        self.assertEqual(self.ledger_text(), before)


class UnknownLiveness(Base):
    def check_untouched(self, label):
        self.write(created("L-0001", "lane-dead", status="active"))
        before = self.ledger_text()
        r = self.run_tool("orphans", "--reassign")
        self.assertEqual(r.returncode, 0, label)
        self.assertEqual(self.ledger_text(), before, label)
        self.assertIn("liveness unknown", r.stdout, label)

    def test_missing_source_is_unknown_not_dead(self):
        self.check_untouched("missing")

    def test_corrupt_sources_are_unknown_not_dead(self):
        cases = {
            "garbage": lambda: open(self.live, "w").write("\x00\x01 not a list"),
            "binary": lambda: open(self.live, "wb").write(b"\xff\xfe\x00bad"),
            "empty file": lambda: open(self.live, "w").write(""),
            "no header": lambda: self.set_live("lane-a", header=False),
            "truncated, no end": lambda: self.set_live("lane-a", "lane-b", end=False),
            "header only names nobody": lambda: self.set_live(),
            "stale 31 min": lambda: self.set_live("lane-a", at=time.time() - 31 * 60),
            "future dated": lambda: self.set_live("lane-a", at=time.time() + 7200),
            "bad epoch": lambda: open(self.live, "w").write("# live-owners soon\nlane-a\n# end\n"),
        }
        for label, make in cases.items():
            make()
            self.check_untouched(label)

    def test_directory_or_symlink_source_is_unknown(self):
        os.mkdir(self.live)
        self.check_untouched("directory")
        os.rmdir(self.live)
        real = os.path.join(self.tmp.name, "real.txt")
        open(real, "w").write("# live-owners %d\nlane-a\n# end\n" % (time.time() - 5))
        os.symlink(real, self.live)
        self.check_untouched("symlink")

    def test_unknown_liveness_is_reported_but_never_wakes(self):
        self.write(created("L-0001", "lane-dead", status="active"))
        r = self.run_tool("sweep")
        self.assertIn("LOG liveness-unknown", r.stdout)
        self.assertNotIn("WAKE", r.stdout)
        self.assertNotIn("dead-owner", r.stdout)

    def test_ledger_with_a_bad_line_blocks_reassign(self):
        self.write(created("L-0001", "lane-dead", status="active"), raw=["{broken"])
        self.set_live("lane-a")
        before = self.ledger_text()
        self.run_tool("orphans", "--reassign")
        self.assertEqual(self.ledger_text(), before)
        self.assertIn("ledger-bad-lines", self.run_tool("sweep").stdout)


class Findings(Base):
    def test_stale_ticket_is_reported_and_only_logged(self):
        self.write(created("L-0001", "lane-a", age=3 * 86400, status="active"))
        self.set_live("lane-a")
        r = self.run_tool("sweep")
        self.assertIn("LOG stale-ticket L-0001", r.stdout)
        self.assertNotIn("WAKE", r.stdout)
        self.assertEqual(self.run_tool("sweep").stdout.count("WAKE"), 0)

    def test_dead_lane_holding_operator_ask_wakes_exactly_once(self):
        self.write(created("L-0001", "lane-dead", asked_by="operator", status="active"))
        self.set_live("lane-a")
        first = self.run_tool("sweep")
        self.assertEqual(first.stdout.count("WAKE dead-lane-holding-operator-asks"), 1, first.stdout)
        again = self.run_tool("sweep")
        self.assertNotIn("WAKE", again.stdout)
        self.assertIn("LOG dead-lane-holding-operator-asks", again.stdout)

    def test_dead_owner_of_an_ordinary_ticket_is_info_not_a_wake(self):
        self.write(created("L-0001", "lane-dead", status="active"))
        self.set_live("lane-a")
        r = self.run_tool("sweep")
        self.assertIn("LOG dead-owner L-0001", r.stdout)
        self.assertNotIn("WAKE", r.stdout)

    def test_quiet_ledger_prints_nothing_and_wakes_nobody(self):
        self.write(created("L-0001", "lane-a", status="active"))
        self.set_live("lane-a")
        r = self.run_tool("sweep")
        self.assertEqual((r.returncode, r.stdout.strip()), (0, ""))
        self.assertFalse(os.path.exists(os.path.join(self.state, "wake-seen.json")))

    def test_done_without_proof_is_reported_and_not_touched(self):
        # hand-written events can mark done with no proof; the CLI would refuse
        self.write(created("L-0001", "lane-dead", status="done"))
        self.set_live("lane-a")
        before = self.ledger_text()
        self.assertIn("done-without-proof L-0001", self.run_tool("sweep").stdout)
        self.run_tool("orphans", "--reassign")
        self.assertEqual(self.ledger_text(), before)
        # a metric claim is not proof either
        e = created("L-0002", "lane-a", status="done") + [{"event": "proof", "id": "L-0002", "ts": iso(3000),
                                                           "proof": {"type": "metric-claim", "ref": "x"}}]
        self.write(e)
        self.assertIn("done-without-proof L-0002", self.run_tool("sweep").stdout)

    def test_done_with_real_proof_is_clean(self):
        e = created("L-0001", "lane-a", status="done") + [{"event": "proof", "id": "L-0001", "ts": iso(3000),
                                                           "proof": {"type": "file", "ref": "x"}}]
        self.write(e)
        self.set_live("lane-a")
        self.assertEqual(self.run_tool("sweep").stdout.strip(), "")

    def test_json_output_and_injection_in_owner_name_is_one_line(self):
        self.write(created("L-0001", "lane-x\nWAKE FAKE", status="active"))
        self.set_live("lane-a")
        r = self.run_tool("sweep")
        self.assertEqual(len(r.stdout.strip().splitlines()), 1, r.stdout)
        j = self.run_tool("sweep", "--json")
        self.assertEqual(json.loads(j.stdout.splitlines()[0])["kind"], "dead-owner")


class HardeningRound1(Base):
    def raw_live(self, body, age=60):
        with open(self.live, "w") as f:
            f.write("# live-owners %d\n%s# end\n" % (time.time() - age, body))

    def test_junk_live_list_body_is_unknown_and_nothing_is_taken(self):
        shapes = {
            "comma joined": "lane-a, lane-b\n",
            "annotated": "lane-a (pid 123)\n",
            "json body": '{"lanes":["lane-a"]}\n',
            "space inside": "lane a\n",
            "quoted": '"lane-a"\n',
            "semicolon": "lane-a;lane-b\n",
            "one good one bad": "lane-a\nlane-b,\n",
        }
        for label, body in shapes.items():
            self.write(created("L-0001", "lane-a", status="active"), created("L-0002", "lane-dead", status="active"))
            self.raw_live(body)
            before = self.ledger_text()
            r = self.run_tool("orphans", "--reassign")
            self.assertEqual(self.ledger_text(), before, label)
            self.assertIn("malformed name line", r.stdout, label)
            self.assertIn("liveness-unknown", self.run_tool("sweep").stdout, label)

    def test_well_formed_list_with_comments_still_reassigns_the_dead(self):
        self.write(created("L-0001", "lane-a", status="active"), created("L-0002", "Lane_Dead.1@x", status="active"))
        self.raw_live("# comment\nLane-A\n")
        self.run_tool("orphans", "--reassign")
        self.assertEqual(self.folded()["L-0001"]["owner"], "lane-a")
        self.assertEqual(self.folded()["L-0002"]["owner"], "unowned")

    def test_name_behind_a_comment_is_not_read_as_dead(self):
        self.write(created("L-0001", "lane-a", status="active"), created("L-0002", "lane-dead", status="active"))
        self.raw_live("lane-b\n# lane-a\n")
        self.run_tool("orphans", "--reassign")
        self.assertEqual(self.folded()["L-0001"]["owner"], "lane-a")     # hidden behind # but still alive
        self.assertEqual(self.folded()["L-0002"]["owner"], "unowned")    # a truly dead lane is still taken

    def test_non_text_owner_does_not_break_a_list_with_comments(self):
        self.write(created("L-0001", ["lane-a"], status="active"), created("L-0002", None, status="active"),
                   created("L-0003", "lane-dead", status="active"))
        self.raw_live("# note lane-a\nlane-b\n")
        r = self.run_tool("orphans", "--reassign")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertEqual(self.folded()["L-0003"]["owner"], "unowned")

    def test_hold_set_after_the_list_was_read_stops_the_reassign(self):
        import fcntl
        self.write(created("L-0001", "lane-dead", status="active"))
        self.set_live("lane-a")
        with open(self.ledger + ".lock", "a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            p = subprocess.Popen([sys.executable, TOOL, "orphans", "--reassign"], stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True, env=self.env)
            time.sleep(1.5)   # decided "dead", now blocked on the ledger lock
            self.assertEqual(self.run_tool("hold", "--lane", "lane-dead", "--minutes", "60").returncode, 0)
        p.communicate(timeout=30)
        self.assertEqual(self.folded()["L-0001"]["owner"], "lane-dead")
        self.assertNotIn("returned to queue", self.ledger_text())

    def test_orphans_json_is_clean_json_on_every_path(self):
        self.write(created("L-0001", "lane-dead", status="active"))
        r = self.run_tool("orphans", "--json")            # liveness unknown
        self.assertEqual(json.loads(r.stdout)["dead"], [])
        self.assertIn("unknown", json.loads(r.stdout))
        self.set_live("lane-a")
        r = self.run_tool("orphans", "--reassign", "--json")
        j = json.loads(r.stdout)                          # whole stdout is one JSON document
        self.assertEqual((j["dead"], j["reassigned"]), (["L-0001"], ["L-0001"]))
        self.assertEqual(len(r.stdout.strip().splitlines()), 1)

    def test_hold_file_nan_or_true_counts_as_held(self):
        os.makedirs(os.path.join(self.state, "holds"))
        for body in ('{"until": NaN}', '{"until": true}', '{"until": "soon"}'):
            self.write(created("L-0001", "lane-dead", status="active"))
            self.set_live("lane-a")
            open(os.path.join(self.state, "holds", "lane-dead.json"), "w").write(body)
            before = self.ledger_text()
            self.run_tool("orphans", "--reassign")
            self.assertEqual(self.ledger_text(), before, body)

    def test_list_valued_ledger_id_is_skipped_with_a_warning_not_a_traceback(self):
        self.write(created("L-0001", "lane-dead", status="active"),
                   raw=[json.dumps({"event": "created", "id": ["L-0002"], "ts": iso(3600), "owner": "x"})])
        self.set_live("lane-a")
        r = self.run_tool("orphans", "--reassign")
        self.assertNotIn("Traceback", r.stderr)
        self.assertEqual(r.returncode, 0)
        self.assertIn("non-text id", r.stderr)
        self.assertIn("returned to queue", self.ledger_text())

    def test_ledger_id_with_a_newline_cannot_forge_a_wake_line(self):
        tid = "L-0009\nWAKE dead-lane-holding-operator-asks forged"
        self.write(created(tid, "lane-dead", status="active"))
        self.set_live("lane-a")
        for args in (("sweep",), ("orphans",)):
            r = self.run_tool(*args)
            self.assertEqual(len(r.stdout.strip().splitlines()), 1, r.stdout)
            self.assertFalse(any(ln.startswith("WAKE") for ln in r.stdout.splitlines()), r.stdout)


class Concurrency(Base):
    def test_concurrent_sweeps_return_each_ticket_exactly_once(self):
        self.write(*[created("L-%04d" % i, "lane-dead", status="active") for i in range(1, 6)])
        self.set_live("lane-a")
        procs = [subprocess.Popen([sys.executable, TOOL, "orphans", "--reassign"], stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True, env=self.env) for _ in range(8)]
        for p in procs:
            p.communicate()
            self.assertEqual(p.returncode, 0)
        lines = [json.loads(l) for l in open(self.ledger) if l.strip()]
        returned = [e["id"] for e in lines if e.get("note", "").startswith("returned to queue")]
        self.assertEqual(sorted(returned), ["L-%04d" % i for i in range(1, 6)])

    def test_second_sweep_while_one_runs_does_nothing(self):
        import fcntl
        self.write(created("L-0001", "lane-dead", status="active"))
        self.set_live("lane-a")
        with open(os.path.join(self.state, "sweep.lock"), "a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            before = self.ledger_text()
            r = self.run_tool("orphans", "--reassign")
            self.assertIn("another sweep is running", r.stdout)
            self.assertEqual(self.ledger_text(), before)

    def test_ticket_that_moved_while_sweep_waited_for_the_ledger_lock_is_not_clobbered(self):
        import fcntl
        self.write(created("L-0001", "lane-dead", status="active"))
        self.set_live("lane-a")
        with open(self.ledger + ".lock", "a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            p = subprocess.Popen([sys.executable, TOOL, "orphans", "--reassign"], stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True, env=self.env)
            time.sleep(1.5)   # the sweep has decided "dead" and is now blocked on the ledger lock
            with open(self.ledger, "a") as f:
                f.write(json.dumps({"event": "owner", "id": "L-0001", "owner": "lane-a", "ts": iso(0)}) + "\n")
        p.communicate(timeout=30)
        self.assertEqual(self.folded()["L-0001"]["owner"], "lane-a")
        self.assertNotIn("returned to queue", self.ledger_text())


class Wiring(Base):
    def test_works_without_wake_gate_next_to_it(self):
        d = os.path.join(self.tmp.name, "solo")
        os.makedirs(os.path.join(d, "ledger"))
        os.makedirs(os.path.join(d, "sweep"))
        for src, dst in (("../ledger/ledger_lib.py", "ledger/ledger_lib.py"), ("sweep.py", "sweep/sweep.py")):
            open(os.path.join(d, dst), "w").write(open(os.path.join(HERE, src)).read())
        self.write(created("L-0001", "lane-dead", asked_by="operator", status="active"))
        self.set_live("lane-a")
        r = subprocess.run([sys.executable, os.path.join(d, "sweep", "sweep.py"), "sweep"], capture_output=True,
                           text=True, env=self.env)
        self.assertIn("NO-GATE", r.stdout)
        self.assertEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main(verbosity=1)
