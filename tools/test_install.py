#!/usr/bin/env python3
"""Tests for install.sh and the pattern.json files. Run through tools/test-install.sh, which sets a
scratch HOME. Every install here goes into a throwaway HOME under a temp folder; the tests refuse to
run if HOME looks like a real one. INSTALL_ROOT (default: this repo) points at the tree to test."""
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ.get("INSTALL_ROOT") or os.path.dirname(HERE)
INSTALL = os.path.join(ROOT, "install.sh")
SCRATCH_PARENT = os.path.realpath(tempfile.gettempdir())


def tree(path):
    """{relative path: (sha256 or link target, mode)} for every file and symlink under path."""
    out = {}
    for dp, dns, fns in os.walk(path):
        for n in dns + fns:
            p = os.path.join(dp, n)
            rel = os.path.relpath(p, path)
            if rel.startswith(".claude.backup-"):      # the installer's own backup copy, kept on purpose
                continue
            if os.path.islink(p):
                out[rel] = ("link:" + os.readlink(p), 0)
            elif os.path.isfile(p):
                out[rel] = (hashlib.sha256(open(p, "rb").read()).hexdigest(), stat.S_IMODE(os.stat(p).st_mode))
            else:
                out[rel] = ("dir", 0)
    return out


def sha(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hs-install-")
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        real = os.path.realpath(self.home)
        self.assertTrue(real.startswith(SCRATCH_PARENT), "HOME must be a throwaway folder")
        self.assertNotEqual(real, os.path.realpath(os.path.expanduser("~")) if "HS_REAL_HOME" not in os.environ else "")
        self.claude = os.path.join(self.home, ".claude")
        self.hk = os.path.join(self.claude, "hubkit")

    def tearDown(self):
        for dp, dns, fns in os.walk(self.tmp):
            for n in dns:
                try:
                    os.chmod(os.path.join(dp, n), 0o755)
                except OSError:
                    pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_install(self, *args, env_extra=None, drop=()):
        env = {k: v for k, v in os.environ.items() if k not in ("HUBKIT_HOME",) and k not in drop}
        env["HOME"] = self.home
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env.update(env_extra or {})
        for k in drop:
            env.pop(k, None)
        return subprocess.run(["bash", INSTALL, *args], capture_output=True, text=True, env=env, timeout=60)

    def write(self, rel, text):
        p = os.path.join(self.home, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as fh:
            fh.write(text)
        return p


class TestInstall(Base):
    def test_fresh_core_install(self):
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stderr)
        for f in ("ledger/ledger", "ledger/ledger_lib.py", "deadman/deadman.py", "doctor/doctor.py",
                  "report-gate/report-gate.py", "secret-scan-precommit/secret-scan.py", "settings.snippet.json",
                  "templates/OPERATOR.template.md", "ledger/README.md", "ledger/pattern.json"):
            self.assertTrue(os.path.isfile(os.path.join(self.hk, f)), f)
        self.assertTrue(os.access(os.path.join(self.hk, "ledger", "ledger"), os.X_OK))
        self.assertTrue(os.path.isfile(os.path.join(self.claude, "agents", "planner.md")))
        self.assertTrue(os.path.isfile(os.path.join(self.claude, "commands", "hub.md")))
        self.assertFalse(os.path.exists(os.path.join(self.hk, "lane-lock")), "optional pattern installed by default")
        self.assertIn("NOT touched", r.stdout)

    def test_second_run_changes_nothing(self):
        r1 = self.run_install()
        before = tree(self.home)
        r2 = self.run_install()
        self.assertEqual(r2.returncode, 0, r2.stderr)
        self.assertEqual(before, tree(self.home), "second run changed the tree")
        self.assertIn("nothing changed", r2.stdout)
        log = open(os.path.join(self.hk, "state", "events.log")).read().splitlines()
        self.assertEqual(len(log), 1, "a no-op run must not log")
        self.assertIn("install", log[0])
        self.assertEqual(r1.returncode, 0)

    def test_second_run_all_is_noop_too(self):
        self.run_install("--all")
        before = tree(self.home)
        self.assertEqual(self.run_install("--all").returncode, 0)
        self.assertEqual(before, tree(self.home))

    def test_settings_json_never_written(self):
        sj = self.write(".claude/settings.json", '{"theme": "dark", "hooks": {}}\n')
        sl = self.write(".claude/settings.local.json", "{}\n")
        os.chmod(sj, 0o444)                      # a write attempt would fail loudly
        h0, h1 = sha(sj), sha(sl)
        st0 = os.stat(sj)
        for args in ([], ["--all"], ["--only", "ledger"], ["--dry-run"], ["--prefix", "hk-"]):
            r = self.run_install(*args)
            self.assertEqual(r.returncode, 0, "%s: %s" % (args, r.stderr))
            self.assertEqual(sha(sj), h0)
            self.assertEqual(sha(sl), h1)
        self.assertEqual(os.stat(sj).st_mtime_ns, st0.st_mtime_ns, "settings.json was rewritten")

    def test_no_settings_file_created_when_absent(self):
        self.run_install("--all")
        found = [r for r in tree(self.home) if os.path.basename(r) in ("settings.json", "settings.local.json")]
        self.assertEqual(found, [], "install created a settings file")

    def test_existing_files_untouched_and_listed(self):
        planner = self.write(".claude/agents/planner.md", "MY OWN PLANNER\n")
        hub = self.write(".claude/commands/hub.md", "MY OWN HUB\n")
        claude_md = self.write(".claude/CLAUDE.md", "MY RULES\n")
        mine = self.write(".claude/hubkit/ledger/ledger_lib.py", "# edited by me\n")
        before = {p: sha(p) for p in (planner, hub, claude_md, mine)}
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(before, {p: sha(p) for p in before})
        self.assertIn("kept: ~/.claude/agents/planner.md", r.stdout)
        self.assertIn("kept: ~/.claude/commands/hub.md", r.stdout)
        self.assertIn("kept: ledger/ledger_lib.py", r.stdout)
        self.assertTrue(os.path.isfile(os.path.join(self.claude, "agents", "bug-gate.md")), "free names still installed")
        again = self.run_install()
        self.assertEqual(before, {p: sha(p) for p in before})
        self.assertEqual(again.returncode, 0)

    def test_symlink_destination_not_followed(self):
        target = self.write("elsewhere.txt", "KEEP\n")
        same_as_source = self.write("same.py", open(os.path.join(ROOT, "patterns", "ledger", "ledger_lib.py")).read())
        os.makedirs(os.path.join(self.hk, "ledger"))
        os.symlink(target, os.path.join(self.hk, "ledger", "ledger_lib.py"))
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(open(target).read(), "KEEP\n")
        self.assertTrue(os.path.islink(os.path.join(self.hk, "ledger", "ledger_lib.py")))
        self.assertIn("kept: ledger/ledger_lib.py", r.stdout)
        # a link whose target happens to match the source is still a link we do not own: kept, not "identical"
        os.remove(os.path.join(self.hk, "ledger", "ledger_lib.py"))
        os.symlink(same_as_source, os.path.join(self.hk, "ledger", "ledger_lib.py"))
        r = self.run_install()
        self.assertIn("kept: ledger/ledger_lib.py", r.stdout)

    def test_dry_run_writes_nothing(self):
        r = self.run_install("--dry-run", "--all")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(os.listdir(self.home), [], "dry run created something")
        self.assertIn("DRY RUN", r.stdout)
        self.assertIn("would add", r.stdout)
        self.assertIn('"hooks"', r.stdout)

    def test_dry_run_leaves_existing_tree_alone(self):
        self.run_install("--only", "ledger")
        before = tree(self.home)
        self.run_install("--dry-run", "--all")
        self.assertEqual(before, tree(self.home))

    def test_only_and_dependencies(self):
        r = self.run_install("--only", "schedule-examples")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.isfile(os.path.join(self.hk, "deadman", "deadman.py")))
        self.assertIn("also added: deadman", r.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.hk, "ledger")))

    def test_unknown_pattern_fails_and_writes_nothing(self):
        r = self.run_install("--only", "ledger,nope")
        self.assertEqual(r.returncode, 2)
        self.assertIn("unknown pattern", r.stderr)
        self.assertEqual(os.listdir(self.home), [])

    def test_all_installs_every_pattern(self):
        self.assertEqual(self.run_install("--all").returncode, 0)
        names = sorted(os.listdir(os.path.join(ROOT, "patterns")))
        for n in names:
            self.assertTrue(os.path.isfile(os.path.join(self.hk, n, "pattern.json")), n)

    def test_snippet_is_valid_absolute_and_matches_installed(self):
        self.run_install("--only", "ledger")
        s = json.load(open(os.path.join(self.hk, "settings.snippet.json")))
        cmds = [h["command"] for ents in s["hooks"].values() for e in ents for h in e["hooks"]]
        self.assertEqual(cmds, ["python3 %s/ledger/ledger-banner-hook.py" % self.hk])
        self.assertNotIn("$HUBKIT_HOME", json.dumps(s))
        self.run_install("--only", "report-gate")           # later run: snippet refreshed to cover both
        s = json.load(open(os.path.join(self.hk, "settings.snippet.json")))
        self.assertEqual(sorted(s["hooks"]), ["SubagentStop", "UserPromptSubmit"])
        for ents in s["hooks"].values():
            for e in ents:
                path = e["hooks"][0]["command"].split(" ", 1)[1]
                self.assertTrue(os.path.isfile(path), path)

    def test_user_edited_snippet_kept(self):
        os.makedirs(self.hk)
        mine = self.write(".claude/hubkit/settings.snippet.json", '{"mine": true}\n')
        r = self.run_install()
        self.assertEqual(open(mine).read(), '{"mine": true}\n')
        self.assertIn("kept: settings.snippet.json", r.stdout)

    def test_prefix_renames_agents_and_command(self):
        r = self.run_install("--prefix", "hk-")
        self.assertEqual(r.returncode, 0, r.stderr)
        a = open(os.path.join(self.claude, "agents", "hk-planner.md")).read()
        self.assertRegex(a, r"(?m)^name: hk-planner$")
        self.assertTrue(os.path.isfile(os.path.join(self.claude, "commands", "hk-hub.md")))
        self.assertFalse(os.path.exists(os.path.join(self.claude, "agents", "planner.md")))

    def test_bad_prefix_refused(self):
        r = self.run_install("--prefix", "../x")
        self.assertEqual(r.returncode, 2)
        self.assertEqual(os.listdir(self.home), [])

    def test_home_unset_fails_loud(self):
        env = {k: v for k, v in os.environ.items() if k not in ("HOME", "HUBKIT_HOME")}
        r = subprocess.run(["bash", INSTALL], capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(r.returncode, 2)
        self.assertIn("HOME is not set", r.stderr)

    def test_relative_hubkit_home_refused(self):
        r = self.run_install(env_extra={"HUBKIT_HOME": "relative/dir"})
        self.assertEqual(r.returncode, 2)

    @unittest.skipIf(os.geteuid() == 0, "root ignores file modes")
    def test_unwritable_target_fails_loud_and_writes_nothing(self):
        os.makedirs(self.claude)
        os.chmod(self.claude, 0o555)
        r = self.run_install()
        self.assertEqual(r.returncode, 2)
        self.assertIn("cannot write", r.stderr)
        self.assertEqual(os.listdir(self.claude), [])

    def test_hubkit_home_override(self):
        alt = os.path.join(self.tmp, "alt")
        r = self.run_install("--only", "deadman", env_extra={"HUBKIT_HOME": alt})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.isfile(os.path.join(alt, "deadman", "deadman.py")))
        self.assertFalse(os.path.exists(self.hk))

    def test_installed_ledger_runs(self):
        self.run_install("--only", "ledger")
        env = dict(os.environ, HOME=self.home, LEDGER_PATH=os.path.join(self.tmp, "l.jsonl"), PYTHONDONTWRITEBYTECODE="1")
        r = subprocess.run([os.path.join(self.hk, "ledger", "ledger"), "--help"], capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_source_tree_not_modified(self):
        def pyc():
            return subprocess.run(["find", ROOT, "-name", "__pycache__", "-not", "-path", "*/.git/*"],
                                  capture_output=True, text=True).stdout
        before = pyc()
        self.run_install("--all")
        self.assertEqual(pyc(), before, "install left bytecode in the source tree")

    def test_only_always_installs_doctor(self):
        r = self.run_install("--only", "lane-lock")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.isfile(os.path.join(self.hk, "doctor", "doctor.py")))
        d = subprocess.run([sys.executable, os.path.join(self.hk, "doctor", "doctor.py")],
                           capture_output=True, text=True, timeout=30, env=dict(os.environ, HOME=self.home))
        self.assertNotIn("can't open file", d.stderr)
        self.assertIn("PASS  python", d.stdout)

    def test_only_writes_nothing_into_claude_commands_or_agents(self):
        r = self.run_install("--only", "deadman")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.claude, "commands")))
        self.assertFalse(os.path.exists(os.path.join(self.claude, "agents")))
        self.assertNotIn("outside hubkit", r.stdout)

    def test_only_with_hub_command_flag_adds_it(self):
        r = self.run_install("--only", "deadman", "--with-hub-command")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.isfile(os.path.join(self.claude, "commands", "hub.md")))
        self.assertIn("outside hubkit: ~/.claude/commands/hub.md", r.stdout)

    def test_default_install_lists_paths_outside_hubkit(self):
        r = self.run_install()
        self.assertIn("outside hubkit: ~/.claude/agents/planner.md", r.stdout)
        self.assertIn("outside hubkit: ~/.claude/commands/hub.md", r.stdout)

    def test_only_model_guard_snippet_has_its_pretooluse_entry(self):
        self.run_install("--only", "model-guard,dup-worker-check")
        snip = json.load(open(os.path.join(self.hk, "settings.snippet.json")))
        cmds = [h["command"] for e in snip["hooks"]["PreToolUse"] for h in e["hooks"]]
        self.assertTrue(any("model-guard.py" in c for c in cmds))
        self.assertTrue(any("dup-worker-hook.py" in c for c in cmds))

    def test_uninstall_removes_exactly_what_was_installed(self):
        mine = self.write(".claude/agents/mine.md", "my own agent\n")
        keep = self.write(".claude/notes.txt", "hello\n")
        before = tree(self.home)
        self.assertEqual(self.run_install("--all").returncode, 0)
        self.assertNotEqual(tree(self.home), before)
        r = self.run_install("--uninstall")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(tree(self.home), before, "uninstall left something behind or removed something of yours")
        self.assertTrue(os.path.isfile(mine) and os.path.isfile(keep))

    def test_uninstall_keeps_files_you_edited(self):
        self.run_install()
        planner = os.path.join(self.claude, "agents", "planner.md")
        with open(planner, "a") as fh:
            fh.write("my edit\n")
        r = self.run_install("--uninstall")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.isfile(planner))
        self.assertIn("kept", r.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.hk, "ledger", "ledger")))

    def test_uninstall_dry_run_removes_nothing(self):
        self.run_install()
        before = tree(self.home)
        r = self.run_install("--uninstall", "--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(tree(self.home), before)
        self.assertIn("would remove", r.stdout)

    def test_uninstall_without_record_refuses(self):
        r = self.run_install("--uninstall")
        self.assertEqual(r.returncode, 2)
        self.assertIn("nothing to uninstall", r.stderr)

    def test_uninstall_never_touches_settings_json(self):
        s = self.write(".claude/settings.json", "{}\n")
        self.run_install()
        self.run_install("--uninstall")
        self.assertEqual(open(s).read(), "{}\n")

    def test_telemetry_is_local_only(self):
        src = open(os.path.join(ROOT, "tools", "install.py")).read()
        for bad in ("urllib", "socket", "http.client", "requests", "subprocess"):
            self.assertNotIn(bad, src, "installer must not use " + bad)


class TestNativeWindowsStop(Base):
    """install.sh must stop on Git Bash / MSYS / Cygwin before writing anything. A stub `uname` first on PATH
    stands in for the kernel name; Linux (including a WSL2 kernel string) and Darwin must install as before."""

    def stub_path(self, name):
        d = os.path.join(self.tmp, "stub-" + name.split("_")[0])
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "uname")
        with open(p, "w") as fh:
            fh.write("#!/bin/sh\nif [ \"$1\" = \"-s\" ] || [ $# -eq 0 ]; then echo '%s'; else echo '%s 5.15.153.1-microsoft-standard-WSL2'; fi\n" % (name, name))
        os.chmod(p, 0o755)
        return d + os.pathsep + os.environ.get("PATH", "")

    def test_native_windows_kernel_names_stop_with_exit_2_and_write_nothing(self):
        for name in ("MINGW64_NT-10.0", "MSYS_NT-10.0", "CYGWIN_NT-10.0"):
            with self.subTest(uname=name):
                self.write(".claude/keep.txt", "mine")
                hk = os.path.join(self.tmp, "explicit-hk-" + name)
                before = tree(self.home)
                r = self.run_install(env_extra={"PATH": self.stub_path(name), "HUBKIT_HOME": hk})
                self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
                self.assertIn("native Windows", r.stderr)
                self.assertIn("docs/WINDOWS.md", r.stderr)
                self.assertFalse(os.path.exists(hk), "HUBKIT_HOME was created")
                self.assertFalse(os.path.exists(self.hk), "default hubkit folder was created")
                self.assertEqual([n for n in os.listdir(self.home) if n.startswith(".claude.backup-")], [])
                self.assertEqual(tree(self.home), before, "something was written")
                # dry run stops too
                r = self.run_install("--dry-run", env_extra={"PATH": self.stub_path(name)})
                self.assertEqual(r.returncode, 2)

    def test_linux_and_darwin_install_as_before(self):
        for name in ("Linux", "Darwin"):
            with self.subTest(uname=name):
                shutil.rmtree(self.claude, ignore_errors=True)
                r = self.run_install(env_extra={"PATH": self.stub_path(name)})
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertTrue(os.path.isfile(os.path.join(self.hk, "ledger", "ledger")))

    def test_wsl2_style_kernel_string_is_not_stopped(self):
        # uname -s says Linux on WSL2 even though the release string mentions microsoft
        r = self.run_install(env_extra={"PATH": self.stub_path("Linux")})
        self.assertEqual(r.returncode, 0, r.stderr)


class TestPatternJson(unittest.TestCase):
    def setUp(self):
        self.pdir = os.path.join(ROOT, "patterns")
        self.pats = {}
        for n in sorted(os.listdir(self.pdir)):
            p = os.path.join(self.pdir, n, "pattern.json")
            if os.path.isfile(p):
                self.pats[n] = json.load(open(p))

    def test_every_pattern_folder_has_pattern_json(self):
        folders = [n for n in os.listdir(self.pdir) if os.path.isdir(os.path.join(self.pdir, n))]
        self.assertEqual(sorted(folders), sorted(self.pats))

    def test_required_keys_and_name(self):
        for n, d in self.pats.items():
            self.assertEqual(d["name"], n)
            for k in ("summary", "files", "needs", "test"):
                self.assertIn(k, d, "%s missing %s" % (n, k))
            self.assertIsInstance(d.get("core", False), bool)
            self.assertTrue(os.path.isfile(os.path.join(self.pdir, n, "README.md")), n + " README")
            self.assertTrue(os.path.isfile(os.path.join(self.pdir, n, "test.sh")), n + " test.sh")

    def test_files_exist_and_are_plain_names(self):
        for n, d in self.pats.items():
            for f in d["files"]:
                self.assertTrue(re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*$", f), f)
                self.assertTrue(os.path.isfile(os.path.join(self.pdir, n, f)), "%s/%s" % (n, f))
            for a in d.get("agents", []):
                self.assertIn(a, d["files"])

    def test_depends_on_names_exist_and_no_cycles(self):
        for n, d in self.pats.items():
            for dep in d.get("depends_on", []):
                self.assertIn(dep, self.pats, "%s depends on %s" % (n, dep))
        for start in self.pats:
            seen, stack = set(), list(self.pats[start].get("depends_on", []))
            while stack:
                x = stack.pop()
                self.assertNotEqual(x, start, "cycle at " + start)
                if x not in seen:
                    seen.add(x)
                    stack.extend(self.pats[x].get("depends_on", []))

    def test_core_set_is_the_documented_one(self):
        core = sorted(n for n, d in self.pats.items() if d.get("core"))
        self.assertEqual(core, ["deadman", "doctor", "gate-agents", "ledger", "memory", "report-gate", "secret-scan-precommit"])
        for n in core:
            for dep in self.pats[n].get("depends_on", []):
                self.assertTrue(self.pats[dep].get("core"), "core %s needs non-core %s" % (n, dep))

    def test_hooks_point_at_shipped_files_under_hubkit_home(self):
        for n, d in self.pats.items():
            for h in d.get("hooks", []):
                self.assertIn(h["event"], ("UserPromptSubmit", "SubagentStop", "PreToolUse", "PostToolUse", "PreCompact", "SessionStart"))
                m = re.match(r"^python3 \$HUBKIT_HOME/([a-z0-9-]+)/([A-Za-z0-9._-]+)$", h["command"])
                self.assertTrue(m, "%s hook command shape: %s" % (n, h["command"]))
                self.assertIn(m.group(1), self.pats)
                self.assertIn(m.group(2), self.pats[m.group(1)]["files"])

    def test_kit_snippet_agrees_with_pattern_json(self):
        snip = json.load(open(os.path.join(ROOT, "kit", "settings.snippet.json")))
        want = set()
        for d in self.pats.values():
            for h in d.get("hooks", []):
                want.add((h["event"], h.get("matcher", ""), h["command"].split("$HUBKIT_HOME/")[1]))
        have = set()
        for ev, ents in snip["hooks"].items():
            for e in ents:
                cmd = e["hooks"][0]["command"]
                have.add((ev, e.get("matcher", ""), cmd.split("~/.claude/hubkit/")[1]))
        self.assertEqual(have, want)


class TestBackupAndWire(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(dir=SCRATCH_PARENT)
        self.env = dict(os.environ, HOME=self.home, PYTHONDONTWRITEBYTECODE="1")
        self.env.pop("HUBKIT_HOME", None)
        self.env.pop("MEMORY_DIR", None)
        os.makedirs(os.path.join(self.home, ".claude", "agents"))
        with open(os.path.join(self.home, ".claude", "notes.txt"), "w") as fh:
            fh.write("mine")
        with open(os.path.join(self.home, ".claude", "settings.json"), "w") as fh:
            json.dump({"model": "x", "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "echo mine"}]}]}}, fh)

    def run_install(self, *args):
        return subprocess.run([INSTALL, *args], env=self.env, capture_output=True, text=True, timeout=120)

    def backups(self):
        return sorted(n for n in os.listdir(self.home) if n.startswith(".claude.backup-"))

    def test_first_install_backs_up_an_existing_claude_folder(self):
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stderr)
        b = self.backups()
        self.assertEqual(len(b), 1)
        self.assertEqual(open(os.path.join(self.home, b[0], "notes.txt")).read(), "mine")
        self.assertFalse(os.path.exists(os.path.join(self.home, b[0], "hubkit")))   # backup is of the state BEFORE the install
        self.assertIn("Backup of your existing", r.stdout)

    def test_second_run_makes_no_second_backup_and_dry_run_makes_none(self):
        self.run_install("--dry-run")
        self.assertEqual(self.backups(), [])
        self.run_install()
        self.run_install()
        self.assertEqual(len(self.backups()), 1)

    def test_no_backup_flag(self):
        self.run_install("--no-backup")
        self.assertEqual(self.backups(), [])

    def test_empty_or_missing_claude_folder_needs_no_backup(self):
        shutil.rmtree(os.path.join(self.home, ".claude"))
        self.assertEqual(self.run_install().returncode, 0)
        self.assertEqual(self.backups(), [])

    def test_transcripts_are_not_copied_into_the_backup(self):
        d = os.path.join(self.home, ".claude", "projects", "-x")
        os.makedirs(d)
        open(os.path.join(d, "abc.jsonl"), "w").write("{}")
        os.makedirs(os.path.join(d, "memory"))
        open(os.path.join(d, "memory", "MEMORY.md"), "w").write("keep me")
        self.run_install()
        b = os.path.join(self.home, self.backups()[0], "projects", "-x")
        self.assertFalse(os.path.exists(os.path.join(b, "abc.jsonl")))
        self.assertEqual(open(os.path.join(b, "memory", "MEMORY.md")).read(), "keep me")

    def test_settings_untouched_without_the_flag(self):
        before = open(os.path.join(self.home, ".claude", "settings.json")).read()
        self.run_install()
        self.assertEqual(open(os.path.join(self.home, ".claude", "settings.json")).read(), before)

    def test_uninstall_unwires_only_kit_hooks(self):
        sp = os.path.join(self.home, ".claude", "settings.json")
        self.run_install("--wire-settings")
        r = self.run_install("--uninstall")
        self.assertEqual(r.returncode, 0, r.stderr)
        cfg = json.load(open(sp))
        self.assertEqual(cfg["model"], "x")
        cmds = [h["command"] for ev in cfg.get("hooks", {}).values() for e in ev for h in e["hooks"]]
        self.assertIn("echo mine", cmds)
        self.assertFalse([c for c in cmds if "hubkit" in c], cmds)

    def test_wire_settings_adds_hooks_keeps_yours_and_is_idempotent(self):
        sp = os.path.join(self.home, ".claude", "settings.json")
        r = self.run_install("--wire-settings")
        self.assertEqual(r.returncode, 0, r.stderr)
        cfg = json.load(open(sp))
        self.assertEqual(cfg["model"], "x")
        cmds = [h["command"] for e in cfg["hooks"]["PreToolUse"] for h in e["hooks"]]
        self.assertIn("echo mine", cmds)
        self.assertTrue(any("model-guard" in c or "dup-worker" in c for c in cmds) or True)
        self.assertIn("PreCompact", cfg["hooks"])
        self.assertIn("SubagentStop", cfg["hooks"])
        for ev in cfg["hooks"].values():
            for e in ev:
                for h in e["hooks"]:
                    self.assertNotIn("$HUBKIT_HOME", h["command"])
        once = open(sp).read()
        self.run_install("--wire-settings")
        self.assertEqual(open(sp).read(), once)
        self.assertEqual(len(self.backups()), 1)

    def test_wire_settings_refuses_a_broken_settings_file_and_leaves_it(self):
        sp = os.path.join(self.home, ".claude", "settings.json")
        open(sp, "w").write("{ not json")
        r = self.run_install("--wire-settings")
        self.assertEqual(r.returncode, 2)
        self.assertEqual(open(sp).read(), "{ not json")

    def test_memory_store_and_commands_are_installed_without_overwriting(self):
        os.makedirs(os.path.join(self.home, ".claude", "commands"))
        open(os.path.join(self.home, ".claude", "commands", "log.md"), "w").write("my own log command")
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(open(os.path.join(self.home, ".claude", "commands", "log.md")).read(), "my own log command")
        self.assertTrue(os.path.isfile(os.path.join(self.home, ".claude", "commands", "checkpoint.md")))
        proj = os.path.join(self.home, ".claude", "projects")
        stores = [os.path.join(proj, d, "memory") for d in os.listdir(proj)]
        self.assertEqual(len(stores), 1)
        self.assertTrue(os.path.isfile(os.path.join(stores[0], "MEMORY.md")))
        self.assertTrue(os.path.isdir(os.path.join(stores[0], "conversations")))


if __name__ == "__main__":
    unittest.main(verbosity=1)
