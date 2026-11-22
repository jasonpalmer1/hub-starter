#!/usr/bin/env python3
"""Checks that the docs agree with the repo: every pattern is in the matrix with the right
files, and every path or pattern name the docs mention exists."""
import glob
import json
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = ["README.md"] + sorted(glob.glob(os.path.join(ROOT, "docs", "*.md")))


def read(p):
    p = p if os.path.isabs(p) else os.path.join(ROOT, p)
    with open(p, encoding="utf-8") as fh:
        return fh.read()


def patterns():
    out = {}
    for f in sorted(glob.glob(os.path.join(ROOT, "patterns", "*", "pattern.json"))):
        with open(f, encoding="utf-8") as fh:
            d = json.load(fh)
        out[d["name"]] = d
    return out


class DocsTests(unittest.TestCase):
    def test_matrix_lists_every_pattern_with_its_files_and_test(self):
        m = read("docs/PATTERN-MATRIX.md")
        for name, d in patterns().items():
            rows = [ln for ln in m.splitlines() if ln.startswith("| " + name + " |")]
            self.assertEqual(len(rows), 1, name)
            for fn in d["files"]:
                self.assertIn("`" + fn + "`", rows[0], name + " " + fn)
            self.assertIn(d["test"], rows[0], name)
            self.assertEqual(("| yes |" in rows[0]), bool(d["core"]), name)

    def test_matrix_core_flag_and_test_counts_match_manifest(self):
        man = read("MANIFEST.txt")
        for name in patterns():
            tests = re.findall(r"patterns/%s/test_\S+\t\d+\t(\d+) tests" % re.escape(name), man)
            if tests:
                row = [ln for ln in read("docs/PATTERN-MATRIX.md").splitlines() if ln.startswith("| " + name + " |")][0]
                self.assertIn("(%s)" % tests[0], row, name)

    def test_every_path_in_docs_exists(self):
        for doc in DOCS:
            for p in re.findall(r"`((?:docs|patterns|kit|tools)/[A-Za-z0-9_./-]+)`", read(doc)):
                if "<" in p or p.endswith("/") or "*" in p:
                    continue
                self.assertTrue(os.path.exists(os.path.join(ROOT, p)), "%s mentions missing %s" % (doc, p))

    def test_install_only_names_are_real_patterns(self):
        names = set(patterns())
        for doc in DOCS:
            for group in re.findall(r"install\.sh --only ([a-z0-9,-]+)", read(doc)):
                for n in group.split(","):
                    self.assertIn(n, names, doc)

    def test_every_pattern_has_a_section_in_adopt_one_piece(self):
        a = read("docs/ADOPT-ONE-PIECE.md")
        for name in patterns():
            self.assertIn("`" + name + "`", a, name)

    def test_glossary_defines_the_jargon(self):
        g = read("docs/GLOSSARY.md").lower()
        for term in ("lane", "hub", "lead chat", "operator", "brief", "breaker", "heartbeat"):
            self.assertIn(term, g, term)
            self.assertRegex(g, r"\*\*[^*\n]*" + re.escape(term), term)
        self.assertIn("docs/GLOSSARY.md", read("README.md"))

    def test_lane_lock_gate_values_documented(self):
        for doc in ("patterns/lane-lock/README.md", "docs/ADOPT-ONE-PIECE.md"):
            t = read(doc)
            for v in ("bug-gate", "playtest", "other"):
                self.assertIn(v, t, doc)
        self.assertIn("--gate", read("docs/ADOPT-ONE-PIECE.md"))

    def test_uninstall_and_written_paths_documented(self):
        r = read("README.md")
        for s in ("--uninstall", "~/.claude/agents", "~/.claude/commands", "~/.claude/hubkit", "settings.json"):
            self.assertIn(s, r, s)
        self.assertIn("--uninstall", read("docs/TROUBLESHOOTING.md"))

    def test_no_machine_paths(self):
        for doc in DOCS:
            self.assertNotIn("/Users/", read(doc), doc)
            self.assertNotIn("/home/", read(doc), doc)

    def test_no_private_hub_path_strings(self):
        # the kit lives in the hubkit folder; any other folder starting with hub, or hub/bin, is the author's private setup
        for doc in DOCS + sorted(glob.glob(os.path.join(ROOT, "patterns", "*", "README.md"))):
            t = read(doc)
            self.assertIsNone(re.search(r"\.claude/hub(?!kit)", t), doc)
            for s in ("hub/bin", "hub/state", "hub/reports", "hub/briefs", "hub/strategy"):
                self.assertNotIn(s, t, doc + " " + s)

    def test_no_real_ticket_numbers(self):
        # examples may use L-0001 to L-0010 only; a bigger number is a real ticket from somewhere else
        for doc in DOCS + sorted(glob.glob(os.path.join(ROOT, "patterns", "*", "README.md"))):
            for n in re.findall(r"\bL-(\d{4,5})\b", read(doc)):
                self.assertLessEqual(int(n), 10, "%s mentions ticket L-%s" % (doc, n))

    def test_loop_docs_exist_are_linked_and_listed(self):
        r = read("README.md")
        man = read("MANIFEST.txt")
        for d in ("docs/ALWAYS-RUNNING.md", "docs/IMPROVEMENT-LOOP.md"):
            self.assertTrue(os.path.isfile(os.path.join(ROOT, d)), d)
            self.assertIn("`" + d + "`", r, d)
            self.assertIn("\n" + d + "\t", man, d)
        self.assertIn("docs/ALWAYS-RUNNING.md", r.split("## 5-minute install")[0])   # the headline is above the fold
        self.assertIn("docs/ALWAYS-RUNNING.md", read("docs/FIRST-TICKET.md"))

    def test_manifest_lists_every_doc_with_its_true_line_count(self):
        rows = {}
        for ln in read("MANIFEST.txt").splitlines():
            if ln.strip() and not ln.startswith("#"):
                c = ln.split("\t")
                rows[c[0]] = c
        for doc in DOCS:
            rel = os.path.relpath(doc if os.path.isabs(doc) else os.path.join(ROOT, doc), ROOT)
            self.assertIn(rel, rows, rel + " is not in MANIFEST.txt")
            with open(os.path.join(ROOT, rel), "rb") as fh:
                n = fh.read().count(b"\n")
            self.assertEqual(rows[rel][1], str(n), rel + " line count in MANIFEST.txt")

    def test_windows_guide_is_linked_and_honest(self):
        r = read("README.md")
        self.assertIn("docs/WINDOWS.md", r.split("## 5-minute install")[1].split("\n## ")[0])
        self.assertIn("docs/WINDOWS.md", read("docs/START-HERE.md"))
        self.assertIn("docs/WINDOWS.md", read("docs/TROUBLESHOOTING.md"))
        w = read("docs/WINDOWS.md")
        top = "\n".join(w.splitlines()[:5])
        self.assertIn("Windows 11 only", top)
        self.assertIn("Last verified on a real Windows 11 PC: NOT YET", top)
        for q in ("‘", "’", "“", "”"):
            self.assertNotIn(q, w)
        self.assertIsNone(re.search(r"^\s*\$ ", w, re.M))

    def test_commands_in_the_loop_docs_are_real(self):
        # every  $HK/<pattern>/<script>  or  .../hubkit/<pattern>/<script>  must be a shipped file of that pattern,
        # and every sub-command typed after it must be one the script defines
        for doc in ("docs/ALWAYS-RUNNING.md", "docs/IMPROVEMENT-LOOP.md", "docs/FIRST-TICKET.md"):
            text = read(doc)
            seen = 0
            for pat, script, rest in re.findall(r"python3 (?:\$HK|\$HOME/\.claude/hubkit|~/\.claude/hubkit)/([a-z-]+)/([A-Za-z_.-]+?)(?:[ \t]+([^\n]*))?$", text, re.M):
                seen += 1
                d = patterns().get(pat)
                self.assertIsNotNone(d, "%s: no pattern %s" % (doc, pat))
                self.assertIn(script, d["files"], "%s: %s is not a file of %s" % (doc, script, pat))
                src = read(os.path.join("patterns", pat, script))
                words = (rest or "").split()
                if script.endswith(".py") and words and not words[0].startswith("-") and words[0].islower() \
                        and words[0] in ("next", "tick", "touch", "hold", "lane", "lanes", "sweep", "orphans", "mine",
                                         "close", "status", "ask", "propose", "run", "projects", "check", "beat"):
                    self.assertIn('"%s"' % words[0], src, "%s: %s has no sub-command %s" % (doc, script, words[0]))
            self.assertGreater(seen, 0, doc)

    def test_doctor_rows_named_in_the_docs_exist_in_doctor(self):
        src = read("patterns/doctor/doctor.py")
        for doc in ("docs/ALWAYS-RUNNING.md", "patterns/doctor/README.md"):
            for name in re.findall(r"`(?:PASS |FAIL |WARN |SKIP )?(loop-[a-z]+|stop-arm|failure-register)(?: / [^`]*)?`", read(doc)):
                self.assertIn('"%s"' % name, src, doc + " " + name)


if __name__ == "__main__":
    unittest.main(verbosity=1)
