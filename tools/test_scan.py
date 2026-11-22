#!/usr/bin/env python3
"""Tests for tools/scan.py. Opens with the plan's Attack list: every attack is
planted in a scratch git repo, and the scan must exit 1 with FILE:LINE category
and must NOT echo the planted text. Planted strings are assembled at runtime so
this file does not trip the scanner itself. The denylist here is synthetic.

Run: python3 tools/test_scan.py   (or: bash tools/test-scan.sh)
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True   # direct runs must not leave __pycache__ either
HERE = os.path.dirname(os.path.abspath(__file__))
SCAN = os.path.join(HERE, "scan.py")

# runtime-assembled attack strings (never literal in this file)
USERS = "/Us" + "ers"
HEX32 = "a1b2c3d4" * 4
UUID = "12345678-1234-1234-1234-" + "123456789abc"
RES = "res" + "earch"      # folder names that are plain words, assembled so this file stays clean
EXT = "ext" + "ract"
FAKE_TERMS = "frobnitzco\nw:quillby\n"


def sh(cmd, cwd):
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True)


class Repo:
    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="scantest-")
        self.deny = os.path.join(self.dir, "..", os.path.basename(self.dir) + ".deny")
        with open(self.deny, "w") as f:
            f.write(FAKE_TERMS)
        self.root = os.path.join(self.dir, "repo")
        os.makedirs(self.root)
        sh(["git", "init", "-q"], self.root)
        self.ident("hub-starter", "hub-starter@users.noreply.github.com")
        self.put("README.md", "Install to $HOME and ~/.claude/hubkit. Mail you@example.com.\n"
                 "Use <YOUR_PROJECT>. The operator opens the hub; see the ledger: L-0001 to L-0010.\n")
        self.put("tools/x.py", "print('hello')\n")
        self.sync_manifest()
        self.commit("init")

    def ident(self, name, email):
        sh(["git", "config", "user.name", name], self.root)
        sh(["git", "config", "user.email", email], self.root)

    def put(self, rel, content, mode="w"):
        p = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, mode) as f:
            f.write(content)

    def sync_manifest(self):
        files = []
        for dp, dn, fn in os.walk(self.root):
            dn[:] = [d for d in dn if d != ".git"]
            files += [os.path.relpath(os.path.join(dp, f), self.root) for f in fn]
        files = sorted(set(files) | {"MANIFEST.txt"})
        self.put("MANIFEST.txt", "\n".join(files) + "\n")

    def commit(self, msg="c"):
        sh(["git", "add", "-A", "-f"], self.root)
        sh(["git", "commit", "-q", "-m", msg, "--allow-empty"], self.root)

    def plant(self, rel, content, mode="w"):
        """add a file that IS in the manifest, so only the content rule can catch it"""
        self.put(rel, content, mode)
        self.sync_manifest()
        self.commit("plant")

    def scan(self, extra=()):
        r = subprocess.run([sys.executable, SCAN, "--root", self.root,
                            "--manifest", os.path.join(self.root, "MANIFEST.txt"),
                            "--denylist", self.deny, *extra],
                           capture_output=True, text=True)
        return r.returncode, r.stdout

    def close(self):
        shutil.rmtree(self.dir, ignore_errors=True)
        try:
            os.remove(self.deny)
        except OSError:
            pass


class Base(unittest.TestCase):
    def setUp(self):
        self.r = Repo()
        self.addCleanup(self.r.close)

    def blocked(self, category, secret_text=None):
        rc, out = self.r.scan()
        self.assertEqual(rc, 1, "scan must exit 1; output was: " + out)
        self.assertIn(" " + category, out)
        for s in ([secret_text] if secret_text else []):
            self.assertNotIn(s, out, "scanner echoed the matched text")
        self.assertRegex(out, r"\S+:\d+ " + category)
        return out


class Allowed(Base):
    def test_clean_tree_exits_zero(self):
        rc, out = self.r.scan()
        self.assertEqual((rc, out), (0, ""))

    def test_allowed_strings_pass(self):
        self.r.plant("docs/ok.md", "$HOME ~/.claude/hubkit you@example.com <YOUR_PROJECT> L-0010 L-0001\n"
                     "operator hub ledger noreply@users.noreply.github.com\n")
        self.assertEqual(self.r.scan()[0], 0)

    def test_aggregate_does_not_trip_word_term(self):
        self.r.plant("docs/w.md", "the aggregate of quillbyfoo is fine\n")
        self.assertEqual(self.r.scan()[0], 0)


class Attack1PathLeak(Base):
    def test_users_path(self):
        self.r.plant("tests/f.sh", "cp x " + USERS + "/someone/.claude" + "/hub/y\n")
        self.blocked("path-leak", "someone")

    def test_slug(self):
        self.r.plant("docs/s.md", "dir -" + USERS[1:].replace("/", "-") + "-someone-proj\n")
        self.blocked("path-leak", "someone")

    def test_leak_in_filename(self):
        self.r.plant("docs/" + USERS.replace("/", "_") + "x.md", "ok\n")  # underscore form: must still pass
        self.assertEqual(self.r.scan()[0], 0)


class Attack2Files(Base):
    def test_pyc(self):
        os.makedirs(os.path.join(self.r.root, "hooks", "__pycache__"))
        self.r.put("hooks/__pycache__/x.cpython-311.pyc", b"\x00\x01binary", mode="wb")
        self.blocked("ext")

    def test_pyc_content_is_scanned(self):
        self.r.plant("hooks/__pycache__/x.cpython-311.pyc",
                     b"\x00\x01" + (USERS + "/someone/.claude").encode(), mode="wb")
        out = self.blocked("path-leak", "someone")
        self.assertIn("binary", out)

    def test_bak_lock_log_env(self):
        for name in ("a.json.bak-2026-09-01.txt", "x.lock", "run.log", ".env", ".env.local",
                     "a.bak-2026-09-01.md", ".env.md", ".env.json"):
            r = Repo()
            try:
                r.plant(name, "x\n")
                rc, out = r.scan()
                self.assertEqual(rc, 1, name)
                self.assertIn(" ext", out, name)
            finally:
                r.close()

    def test_unlisted_extensions(self):
        for name in ("data.bin", "pic.png", "app.js", "x.sqlite", "notes.docx"):
            r = Repo()
            try:
                r.plant(name, "x\n")
                rc, out = r.scan()
                self.assertEqual(rc, 1, name)
                self.assertIn(name + ":0 ext", out)
            finally:
                r.close()

    def test_extensionless_script_allowed_only_at_its_exact_path(self):
        r = Repo()
        try:
            r.plant("patterns/ledger/ledger", "#!/usr/bin/env python3\n")
            rc, out = r.scan()
            self.assertNotIn("patterns/ledger/ledger:0 ext", out)
            r.plant("patterns/ledger/other", "x\n")
            r.plant("tools/ledger", "x\n")
            rc, out = r.scan()
            self.assertEqual(rc, 1)
            self.assertIn("patterns/ledger/other:0 ext", out)
            self.assertIn("tools/ledger:0 ext", out)
        finally:
            r.close()

    def test_file_not_in_manifest(self):
        self.r.put("docs/sneaky.md", "innocent\n")  # on disk, not in MANIFEST
        self.blocked("manifest")

    def test_file_tracked_but_not_in_manifest(self):
        self.r.put("docs/sneaky.md", "innocent\n")
        self.r.commit("add without manifest")
        self.blocked("manifest")

    def test_manifest_lists_missing_file(self):
        self.r.put("MANIFEST.txt", open(os.path.join(self.r.root, "MANIFEST.txt")).read() + "ghost.md\n")
        self.blocked("manifest")

    def test_ignored_file_still_seen(self):
        self.r.put(".gitignore", "secret.md\n")
        self.r.put("secret.md", "x\n")
        self.r.sync_manifest()
        self.r.put("MANIFEST.txt", "\n".join(l for l in open(os.path.join(self.r.root, "MANIFEST.txt"))
                                              .read().split() if l != "secret.md") + "\n")
        self.blocked("manifest")

    def test_symlink(self):
        os.symlink("/etc/hosts", os.path.join(self.r.root, "docs_link.md"))
        self.r.sync_manifest()
        self.blocked("symlink")


class Attack3InfraIds(Base):
    def test_hex32(self):
        self.r.plant("docs/c.json", '{"account": "' + HEX32 + '"}\n')
        self.blocked("infra-id", HEX32)

    def test_uuid(self):
        self.r.plant("docs/c.json", '{"db": "' + UUID + '"}\n')
        self.blocked("infra-id", UUID)

    def test_hosting_hostnames(self):
        for host in (".pages" + ".dev", ".workers" + ".dev"):
            r = Repo()
            try:
                r.plant("docs/h.md", "see https://thing" + host + "/x\n")
                rc, out = r.scan()
                self.assertEqual(rc, 1, host)
                self.assertIn(" infra-id", out)
            finally:
                r.close()

    def test_tree_has_no_private_hub_path(self):
        """The shipped tree must not name a maintainer's private hub folder (built from pieces so this file is clean too)."""
        needle = "." + "claude/" + "hub" + "/"
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for dp, dn, fns in os.walk(root):
            dn[:] = [d for d in dn if d != ".git"]
            for fn in fns:
                with open(os.path.join(dp, fn), encoding="utf-8", errors="replace") as fh:
                    self.assertNotIn(needle, fh.read(), os.path.join(dp, fn))

    def test_prepush_requires_denylist_or_generic_only(self):
        import subprocess
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prepush-check.sh")
        env = {k: v for k, v in os.environ.items() if k != "HUBSTARTER_DENYLIST"}
        r = subprocess.run(["bash", script], env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)
        self.assertIn("HUBSTARTER_DENYLIST", r.stdout)
        self.assertIn("--generic-only", r.stdout)

    def test_readme_uninstall_line_is_real(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "README.md"), encoding="utf-8") as fh:
            t = fh.read()
        self.assertNotIn("Uninstall is deleting", t)
        self.assertIn("./install.sh --uninstall", t)
        self.assertIn("ledger and reports are kept", t)


class Attack4Names(Base):
    VARIANTS = {
        "plain": "Frobnitz Co\n",
        "underscore": "frobnitz_co\n",
        "dash-upper": "FROBNITZ-CO\n",
        "unicode-apostrophe": "Frob’nitz Co\n",
        "zero-width": "Frob​nitz​Co\n",
        "concat": 'name = "frob" + "nitz" + "co"\n',
        "multiline-split": 'x = ("frobnitz"\n     "co")\n',
        "word-term": "ask Quillby about it\n",
        "word-term-sep": "quill-by\n",
    }

    def test_every_variant_blocked(self):
        for label, text in self.VARIANTS.items():
            r = Repo()
            try:
                r.plant("docs/n.md", text)
                rc, out = r.scan()
                self.assertEqual(rc, 1, label)
                self.assertIn(" denylist", out, label)
                self.assertRegex(out, r"docs/n\.md:\d+ denylist")
                self.assertNotIn("frob", out.lower())
                self.assertNotIn("quill", out.lower())
            finally:
                r.close()

    def test_real_homoglyph(self):
        r = Repo()
        try:
            r.plant("docs/n.md", "fr\u043ebnitzc\u043e\n")  # cyrillic o
            self.assertEqual(r.scan()[0], 1)
        finally:
            r.close()

    def test_name_in_filename(self):
        self.r.plant("docs/frobnitz-co-notes.md", "clean\n")
        self.blocked("denylist")

    def test_name_in_commit_message(self):
        self.r.commit("notes for frobnitz co")
        self.blocked("denylist")

    def test_name_in_git_config(self):
        sh(["git", "remote", "add", "origin", "https://example.test/frobnitzco/repo.git"], self.r.root)
        self.blocked("denylist")

    def test_dynamic_machine_facts(self):
        self.r.plant("docs/d.md", "hello zzquuxlongname\n")
        home = tempfile.mkdtemp()
        os.makedirs(os.path.join(home, "projects", "zzquuxlongname"))
        env = dict(os.environ, HOME=home, USER="nobody")
        p = subprocess.run([sys.executable, SCAN, "--root", self.r.root, "--dynamic"],
                           capture_output=True, text=True, env=env)
        shutil.rmtree(home)
        self.assertEqual(p.returncode, 1)
        self.assertIn("docs/d.md:1 denylist", p.stdout)
        self.assertNotIn("zzquux", p.stdout)

    def _dyn(self, text, names):
        self.r.plant("docs/d.md", text)
        home = tempfile.mkdtemp()
        for n in names:
            os.makedirs(os.path.join(home, "projects", n))
        env = dict(os.environ, HOME=home, USER="nobody")
        p = subprocess.run([sys.executable, SCAN, "--root", self.r.root, "--dynamic"],
                           capture_output=True, text=True, env=env)
        shutil.rmtree(home)
        return p

    def test_dictionary_word_project_does_not_false_match(self):
        # folders named like plain words must not hit re.search( or extract_text
        p = self._dyn("m = re.search(x)\ndef extract_text(s): pass\nREPORTS = 1\n",
                      [RES, EXT])
        self.assertEqual((p.returncode, p.stdout), (0, ""))

    def test_dictionary_word_still_hits_as_a_whole_word(self):
        p = self._dyn("we shipped the " + RES + " folder\n", [RES])
        self.assertEqual(p.returncode, 1)
        self.assertIn("docs/d.md:1 denylist", p.stdout)

    def test_non_dictionary_project_still_caught_through_tricks(self):
        p = self._dyn("z.z.quux-long_name\nzzquux\nlongname\n", ["zzquuxlongname"])
        self.assertEqual(p.returncode, 1)
        self.assertIn("docs/d.md:1 denylist", p.stdout)


class Attack5Identity(Base):
    def test_author_name_is_denylisted(self):
        self.r.ident("Frobnitz Co", "hub-starter@users.noreply.github.com")
        self.r.commit("authored")
        self.blocked("identity")

    def test_author_email_is_denylisted(self):
        self.r.ident("hub-starter", "someone" + "@frobnitzco.test")
        self.r.commit("authored")
        self.blocked("identity")

    def test_any_non_noreply_email_blocked(self):
        self.r.ident("hub-starter", "someone" + "@corp.test")
        self.r.commit("authored")
        self.blocked("identity")

    def test_committer_only(self):
        env = dict(os.environ, GIT_COMMITTER_EMAIL="x" + "@corp.test", GIT_COMMITTER_NAME="hub-starter")
        subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "c"], cwd=self.r.root, env=env,
                       check=True, capture_output=True)
        self.blocked("identity")

    def test_personal_looking_name_with_a_noreply_address_is_blocked(self):
        self.r.ident("Pat Example", "hub-starter@users.noreply.github.com")
        self.r.commit("authored")
        self.blocked("identity")

    def test_noreply_address_embedding_an_account_id_is_blocked(self):
        self.r.ident("hub-starter", "4982" + "1234+somehandle@users.noreply.github.com")
        self.r.commit("authored")
        self.blocked("identity")

    def test_committer_with_a_personal_name_only_is_blocked(self):
        env = dict(os.environ, GIT_COMMITTER_EMAIL="hub-starter@users.noreply.github.com", GIT_COMMITTER_NAME="Pat Example")
        subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "c"], cwd=self.r.root, env=env,
                       check=True, capture_output=True)
        self.blocked("identity")

    def test_the_standard_identity_passes(self):
        self.r.commit("authored")
        rc, out = self.r.scan()
        self.assertNotIn(" identity", out)

    def test_not_a_git_repo_fails_closed(self):
        shutil.rmtree(os.path.join(self.r.root, ".git"))
        self.blocked("no-git-repo")

    def test_old_commit_with_bad_identity_still_caught(self):
        self.r.ident("hub-starter", "someone" + "@corp.test")
        self.r.commit("old")
        self.r.ident("hub-starter", "hub-starter@users.noreply.github.com")
        self.r.commit("new")
        self.blocked("identity")


class Attack6Provenance(Base):
    def test_memory_file_name(self):
        self.r.plant("docs/p.md", "see " + "feed" + "back_rule.md\n")
        self.blocked("provenance")

    def test_wiki_link(self):
        self.r.plant("docs/p.md", "see " + "[" + "[some-note]" + "]\n")
        self.blocked("provenance")

    def test_ticket_above_fixture_range(self):
        for tid in ("L-" + "0011", "L-" + "1611", "L-" + "12345"):
            r = Repo()
            try:
                r.plant("docs/t.md", "fixed in " + tid + "\n")
                rc, out = r.scan()
                self.assertEqual(rc, 1, tid)
                self.assertIn(" provenance", out)
            finally:
                r.close()

    def test_secret_shape(self):
        for secret in ("sk-" + "ant-" + "A" * 24, "gh" + "p_" + "B" * 36, "AK" + "IA" + "C" * 16):
            r = Repo()
            try:
                r.plant("tools/s.py", 'k = "' + secret + '"\n')
                rc, out = r.scan()
                self.assertEqual(rc, 1, secret[:4])
                self.assertIn(" secret", out)
                self.assertNotIn(secret, out)
            finally:
                r.close()

    def test_real_email(self):
        self.r.plant("docs/e.md", "mail someone" + "@corp.test\n")
        self.blocked("email", "someone")


class Attack7HistoryAndRules(Base):
    """B2: a leak committed and later deleted, a leaking commit message, and the rule gaps the bug gate found."""

    def delete_planted(self, rel):
        sh(["git", "rm", "-q", "-f", rel], self.r.root)
        self.r.sync_manifest()
        self.r.commit("remove")

    def test_deleted_blob_with_email_and_hex_is_still_found(self):
        self.r.plant("docs/old.md", "mail " + "someone" + "@" + "corp-mail" + ".io and " + HEX32 + "\n")
        self.delete_planted("docs/old.md")
        rc, out = self.r.scan()                      # the working tree is clean; only history holds it
        self.assertEqual(rc, 1, out)
        self.assertRegex(out, r"history/docs/old\.md@[0-9a-f]{8}:1 email")
        self.assertRegex(out, r"history/docs/old\.md@[0-9a-f]{8}:1 infra-id")
        self.assertNotIn("corp-mail", out)
        self.assertNotIn(HEX32, out)

    def test_leak_only_on_another_branch_is_found(self):
        sh(["git", "checkout", "-q", "-b", "side"], self.r.root)
        self.r.plant("docs/side.md", "path " + USERS + "/someone/x\n")
        sh(["git", "checkout", "-q", "-"], self.r.root)
        rc, out = self.r.scan()
        self.assertEqual(rc, 1, out)
        self.assertRegex(out, r"history/docs/side\.md@[0-9a-f]{8}:1 path-leak")

    def test_commit_message_with_path_and_email(self):
        self.r.put("tools/y.py", "x = 1\n")
        self.r.sync_manifest()
        sh(["git", "add", "-A", "-f"], self.r.root)
        sh(["git", "commit", "-q", "-m", "fix " + USERS + "/someone/x mail " + "someone" + "@" + "corp-mail" + ".io"], self.r.root)
        rc, out = self.r.scan()
        self.assertEqual(rc, 1, out)
        self.assertRegex(out, r"\.git/commit-[0-9a-f]{8}:1 path-leak")
        self.assertRegex(out, r"\.git/commit-[0-9a-f]{8}:1 email")
        self.assertNotIn("corp-mail", out)

    def test_clean_history_passes(self):
        self.r.plant("docs/ok.md", "plain words only\n")
        self.delete_planted("docs/ok.md")
        self.assertEqual(self.r.scan(), (0, ""))

    def test_history_allow_waives_exactly_one_blob(self):
        self.r.plant("docs/old.md", "path " + USERS + "/someone/x\n")
        self.delete_planted("docs/old.md")
        blob = subprocess.run(["git", "rev-parse", "HEAD~1:docs/old.md"], cwd=self.r.root, capture_output=True,
                              text=True).stdout.strip()
        allow = os.path.join(self.r.dir, "allow.txt")
        with open(allow, "w") as f:
            f.write("# reviewed\n%s reason\n" % blob)
        self.assertEqual(self.r.scan(["--history-allow", allow]), (0, ""))
        self.r.plant("docs/new.md", "path " + USERS + "/someone/y\n")      # a different blob is not waived
        self.delete_planted("docs/new.md")
        rc, out = self.r.scan(["--history-allow", allow])
        self.assertEqual(rc, 1)
        self.assertIn("history/docs/new.md", out)

    def test_bad_history_allow_fails_closed(self):
        allow = os.path.join(self.r.dir, "allow.txt")
        with open(allow, "w") as f:
            f.write("not-a-blob-id\n")
        self.assertEqual(self.r.scan(["--history-allow", allow])[0], 2)
        self.assertEqual(self.r.scan(["--history-allow", os.path.join(self.r.dir, "missing.txt")])[0], 2)

    def test_denylist_term_in_deleted_blob(self):
        self.r.plant("docs/old.md", "talk to quill" + "by about it\n")
        self.delete_planted("docs/old.md")
        rc, out = self.r.scan()
        self.assertEqual(rc, 1)
        self.assertRegex(out, r"history/docs/old\.md@[0-9a-f]{8}:1 denylist")

    # ---- new rules, each planted in a tracked file (and so also caught if it only lives in history)
    CASES = {
        "bearer": ("secret", "curl -H 'Authorization: " + "Bea" + "rer " + "abcdEFGH1234" * 2 + "'\n"),
        "jwt": ("secret", "t = '" + "ey" + "J" + "hbGciOiJIUzI1" + "." + "ey" + "J" + "zdWIiOiIxMjM0" + "." + "SflKxwRJSMeKKF2QT4" + "'\n"),
        "slack-webhook": ("secret", "url = https://hooks." + "slack" + ".com/services/T0000000/B0000000/" + "abcdefghij1234" + "\n"),
        "unquoted-password": ("secret", "pass" + "word=" + "Zx9Qw8Er7Ty6Ui5Op4As3Df\n"),
        "unquoted-token-colon": ("secret", "to" + "ken: " + "Zx9Qw8Er7Ty6Ui5Op4As3Df\n"),
        "private-ip-10": ("infra-id", "host 10." + "20.30.40\n"),
        "private-ip-192": ("infra-id", "host 192." + "168.1.20\n"),
        "private-ip-172": ("infra-id", "host 172." + "16.5.9\n"),
        "cgnat-ip": ("infra-id", "host 100." + "101.2.3\n"),
        "tailnet-host": ("infra-id", "ssh box." + "tail1234" + ".ts" + ".net\n"),
        "ticket-space": ("provenance", "see L" + " 1234 for details\n"),
        "ticket-underscore": ("provenance", "see L" + "_4321\n"),
    }

    def test_each_new_rule_blocks_and_does_not_echo(self):
        for name, (cat, text) in self.CASES.items():
            r = Repo()
            try:
                r.plant("docs/n.md", text)
                rc, out = r.scan()
                self.assertEqual(rc, 1, name + ": " + out)
                self.assertRegex(out, r"docs/n\.md:1 " + cat, name)
                self.assertNotIn("Zx9Qw8", out, name)
            finally:
                r.close()

    def test_new_rules_leave_normal_text_alone(self):
        ok = ("token = os.environ.get('X')\n"
              "password=$PASSWORD\n"
              "token: <your-token-here>\n"
              "Authorization: Bearer <token>\n"
              "version 10.0 and 1.2.3.4 and 8.8.8.8\n"
              "see L-0001 and L-0010\n"
              "SECRET_SCAN_ALLOW_PATHS=docs/fixtures\n"
              "a bearer of bad news\n")
        self.r.plant("docs/fine.md", ok)
        self.assertEqual(self.r.scan(), (0, ""))


class Attack8IdentityAllowlistAndGuard(Base):
    """Frozen allowlist of already-published personal-identity commits, plus the commit-time guard."""
    PAT = ("Pat Example", "49824130+patexample@users.noreply.github.com")

    def _personal_commit(self, name=None, email=None):
        self.r.ident(name or self.PAT[0], email if email is not None else self.PAT[1])
        self.r.commit("personal")
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.r.root, capture_output=True, text=True).stdout.strip()
        self.r.ident("hub-starter", "hub-starter@users.noreply.github.com")
        return sha

    def _allow_file(self, *lines):
        p = os.path.join(self.r.dir, "identity-allow.txt")
        with open(p, "w") as f:
            f.write("# reviewed\n" + "".join(l + "\n" for l in lines))
        return p

    def test_listed_commit_is_excused_but_only_that_one(self):
        sha = self._personal_commit()
        allow = self._allow_file(sha + " reviewed")
        rc, out = self.r.scan(["--identity-allow", allow])
        self.assertNotIn(" identity", out)
        self.assertEqual(rc, 0, out)
        sha2 = self._personal_commit()          # a NEW personal commit, not listed
        self.r.commit("later")
        rc, out = self.r.scan(["--identity-allow", allow])
        self.assertEqual(rc, 1)
        self.assertIn(".git/commit-" + sha2[:8] + ":0 identity", out)
        self.assertNotIn(sha[:8], out)

    def test_without_the_allowlist_the_commit_still_fails(self):
        self._personal_commit()
        self.blocked("identity")

    def test_new_personal_name_or_email_blocked_even_with_an_allowlist(self):
        allow = self._allow_file()
        for nm, em in (("Pat Example", "hub-starter@users.noreply.github.com"),
                       ("hub-starter", "pat" + "@corp.test"),
                       ("hub-starter", "49824130+patexample@users.noreply.github.com")):
            self._personal_commit(nm, em)
            rc, out = self.r.scan(["--identity-allow", allow])
            self.assertEqual(rc, 1, (nm, em, out))
            self.assertIn(" identity", out)

    def test_empty_email_is_blocked(self):
        self._personal_commit("hub-starter", "")
        self.blocked("identity")

    def test_listed_sha_missing_from_history_is_a_finding(self):
        allow = self._allow_file("0123456789abcdef0123456789abcdef01234567 not in history")
        rc, out = self.r.scan(["--identity-allow", allow])
        self.assertEqual(rc, 1)
        self.assertIn("identity-allow-stale", out)

    def test_malformed_allowlist_line_fails_closed(self):
        allow = self._allow_file("abc123 short id")
        self.assertEqual(self.r.scan(["--identity-allow", allow])[0], 2)
        self.assertEqual(self.r.scan(["--identity-allow", os.path.join(self.r.dir, "nope.txt")])[0], 2)

    def test_allowlist_excuses_only_the_identity_rule(self):
        self.r.ident(*self.PAT)
        self.r.commit("see " + USERS + "/someone/x")
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.r.root, capture_output=True, text=True).stdout.strip()
        rc, out = self.r.scan(["--identity-allow", self._allow_file(sha + " r")])
        self.assertNotIn(" identity", out)
        self.assertIn("path-leak", out)

    def test_allowlist_never_excuses_a_denylisted_identity(self):
        # the sha is allowlisted, but the recorded name is on the private denylist: still blocked
        sha = self._personal_commit("frobnitzco", "hub-starter@users.noreply.github.com")
        rc, out = self.r.scan(["--identity-allow", self._allow_file(sha + " reviewed")])
        self.assertEqual(rc, 1, out)
        self.assertIn(".git/commit-" + sha[:8] + ":0 identity", out)
        # control: a non-denylisted personal identity with the same allowlisting is excused
        sha2 = self._personal_commit()
        rc, out = self.r.scan(["--identity-allow", self._allow_file(sha + " r", sha2 + " r")])
        self.assertIn(".git/commit-" + sha[:8] + ":0 identity", out)
        self.assertNotIn(".git/commit-" + sha2[:8] + ":0 identity", out)

    def test_allowlist_matches_the_full_sha_not_an_eight_char_prefix(self):
        sha = self._personal_commit()
        other = sha[:8] + "0" * 32   # same 8-char prefix, different id
        self.assertNotEqual(other, sha)
        rc, out = self.r.scan(["--identity-allow", self._allow_file(other + " prefix collision")])
        self.assertEqual(rc, 1, out)
        self.assertIn(".git/commit-" + sha[:8] + ":0 identity", out)
        # the exact full id is still excused
        rc, out = self.r.scan(["--identity-allow", self._allow_file(sha + " exact")])
        self.assertEqual((rc, out), (0, ""))

    def _split_commit(self, an, ae, cn, ce, msg="split"):
        """commit with a different author and committer, set through the environment"""
        env = dict(os.environ, GIT_AUTHOR_NAME=an, GIT_AUTHOR_EMAIL=ae, GIT_COMMITTER_NAME=cn, GIT_COMMITTER_EMAIL=ce)
        sh(["git", "add", "-A", "-f"], self.r.root)
        subprocess.run(["git", "commit", "-q", "-m", msg, "--allow-empty"], cwd=self.r.root, env=env,
                       check=True, capture_output=True, text=True)
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.r.root, capture_output=True, text=True).stdout.strip()

    def test_allowlist_never_excuses_a_denylisted_message(self):
        self.r.ident(*self.PAT)
        self.r.commit("notes for frobnitzco")
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.r.root, capture_output=True, text=True).stdout.strip()
        rc, out = self.r.scan(["--identity-allow", self._allow_file(sha + " reviewed")])
        self.assertEqual(rc, 1, out)
        self.assertIn(".git/commit-" + sha[:8] + ":0 denylist", out)
        self.assertNotIn(".git/commit-" + sha[:8] + ":0 identity", out)

    def test_allowlist_matches_the_full_sha_not_a_twelve_char_prefix(self):
        sha = self._personal_commit()
        other = sha[:12] + "0" * 28
        self.assertNotEqual(other, sha)
        rc, out = self.r.scan(["--identity-allow", self._allow_file(other + " prefix collision")])
        self.assertEqual(rc, 1, out)
        self.assertIn(".git/commit-" + sha[:8] + ":0 identity", out)

    def test_personal_author_with_kit_committer_is_blocked(self):
        # the committer alone is clean, the author is personal: both roles are checked
        sha = self._split_commit(self.PAT[0], self.PAT[1], "hub-starter", "hub-starter@users.noreply.github.com")
        rc, out = self.r.scan()
        self.assertEqual(rc, 1, out)
        self.assertIn(".git/commit-" + sha[:8] + ":0 identity", out)

    def test_denylisted_committer_is_blocked_even_when_allowlisted(self):
        # author is the kit identity; the denylisted term is only in the committer name or email
        sha = self._split_commit("hub-starter", "hub-starter@users.noreply.github.com",
                                 "frobnitzco", "hub-starter@users.noreply.github.com")
        sha2 = self._split_commit("hub-starter", "hub-starter@users.noreply.github.com",
                                  "hub-starter", "hub-starter" + "@frobnitzco.test", "email")
        rc, out = self.r.scan(["--identity-allow", self._allow_file(sha + " r", sha2 + " r")])
        self.assertEqual(rc, 1, out)
        self.assertIn(".git/commit-" + sha[:8] + ":0 identity", out)
        self.assertIn(".git/commit-" + sha2[:8] + ":0 identity", out)

    def test_ident_bad_rejects_an_email_without_an_at_sign(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("scan_under_test", SCAN)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertFalse(mod.ident_bad("hub-starter", "hub-starter@users.noreply.github.com"))
        for em in ("hub-starter", "", "noreply", "hub-starter.users.noreply.github.com"):
            self.assertTrue(mod.ident_bad("hub-starter", em), em)

    def test_system_dictionary_is_read_once(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("scan_cache_test", SCAN)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod._system_words()
        mod._system_words()
        self.assertGreaterEqual(mod._system_words.cache_info().hits, 1)

    def test_real_allowlist_is_frozen(self):
        import hashlib
        ids = sorted(ln.split()[0] for ln in open(os.path.join(HERE, "identity-allow.txt"))
                     if ln.strip() and not ln.startswith("#"))
        self.assertEqual(len(ids), 0)
        self.assertEqual(hashlib.sha256(("\n".join(ids) + "\n").encode()).hexdigest(),
                         "01ba4719c80b6fe911b091a7c05124b64eeece964e09c058ef8f9805daca546b",
                         "identity-allow.txt changed: this pin and a reviewer must change with it")

    # commit-time guard
    def _guard(self, env=None):
        e = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null", **(env or {}))
        for k in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
            if not (env and k in env):
                e.pop(k, None)
        return subprocess.run([sys.executable, SCAN, "--guard-identity"], cwd=self.r.root, env=e,
                              capture_output=True, text=True)

    def test_guard_allows_the_kit_identity(self):
        self.assertEqual(self._guard().returncode, 0)

    def test_guard_refuses_personal_config_and_env_without_echoing_it(self):
        self.r.ident(*self.PAT)
        p = self._guard()
        self.assertEqual(p.returncode, 1)
        self.assertNotIn("Pat", p.stdout + p.stderr)
        self.assertNotIn("patexample", p.stdout + p.stderr)
        self.r.ident("hub-starter", "hub-starter@users.noreply.github.com")
        p = self._guard({"GIT_COMMITTER_NAME": "Pat Example"})
        self.assertEqual(p.returncode, 1)
        self.assertIn("committer", p.stdout)
        p = self._guard({"GIT_AUTHOR_EMAIL": "pat" + "@corp.test"})
        self.assertEqual(p.returncode, 1)

    def test_guard_refuses_empty_email(self):
        self.r.ident("hub-starter", "")
        self.assertEqual(self._guard().returncode, 1)

    def test_installed_hooks_block_commit_and_merge_commit(self):
        sh(["git", "config", "core.hooksPath", os.path.join(HERE, "githooks")], self.r.root)
        self.r.ident(*self.PAT)
        p = subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "x"], cwd=self.r.root,
                           capture_output=True, text=True)
        self.assertNotEqual(p.returncode, 0)
        self.r.ident("hub-starter", "hub-starter@users.noreply.github.com")
        p = subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "x"], cwd=self.r.root,
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        # merge commit as a personal identity is refused too
        sh(["git", "checkout", "-q", "-b", "side"], self.r.root)
        sh(["git", "commit", "-q", "--allow-empty", "-m", "side"], self.r.root)
        sh(["git", "checkout", "-q", "-"], self.r.root)
        sh(["git", "commit", "-q", "--allow-empty", "-m", "main"], self.r.root)
        self.r.ident(*self.PAT)
        p = subprocess.run(["git", "merge", "--no-ff", "-m", "m", "side"], cwd=self.r.root,
                           capture_output=True, text=True)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("identity-guard", p.stdout + p.stderr)


class Attack9BaselinePin(Base):
    """Pinned history baseline: old published history is judged once by a person; every commit after the pin
    faces every rule, and no allow file can excuse it. A bad pin never narrows the scan."""
    PAT = ("Pat Example", "49824130+patexample@users.noreply.github.com")
    ZERO = "0" * 40

    def head(self):
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.r.root, capture_output=True, text=True).stdout.strip()

    def run_scan(self, *extra):
        r = subprocess.run([sys.executable, SCAN, "--root", self.r.root,
                            "--manifest", os.path.join(self.r.root, "MANIFEST.txt"),
                            "--denylist", self.r.deny, *extra], capture_output=True, text=True)
        return r.returncode, r.stdout, r.stderr

    def personal_commit(self):
        self.r.ident(*self.PAT)
        self.r.commit("personal")
        sha = self.head()
        self.r.ident("hub-starter", "hub-starter@users.noreply.github.com")
        return sha

    def pin_file(self, *lines):
        self.n_pins = getattr(self, "n_pins", 0) + 1          # one file per call: callers build several at once
        p = os.path.join(self.r.dir, "history-base-%d.txt" % self.n_pins)
        with open(p, "w") as f:
            f.write("".join(ln + "\n" for ln in lines))
        return p

    def old_leak_then_pin(self):
        """a leak committed before the pin, a clean commit after it; returns the pin"""
        self.r.plant("tests/old.sh", "cp x " + USERS + "/someone/.claude" + "/hub/y\n")
        pin = self.head()
        os.remove(os.path.join(self.r.root, "tests/old.sh"))     # the tree is clean now; only history still holds it
        self.r.sync_manifest()
        self.r.commit("clean after pin")
        return pin

    # range mode: old findings stay out, new ones come in
    def test_findings_before_the_pin_are_not_reported_after_it_are(self):
        self.personal_commit()
        pin = self.old_leak_then_pin()
        self.assertEqual(self.run_scan()[0], 1)                       # full scan sees old identity + old path
        rc, out, err = self.run_scan("--base", pin)
        self.assertEqual((rc, out), (0, ""), out)
        self.assertIn("range mode", err)

    def test_each_finding_type_in_a_new_commit_is_refused_even_if_allow_listed(self):
        cases = {
            "path-leak": ("tests/f.sh", "cp x " + USERS + "/someone/.claude" + "/hub/y\n"),
            "provenance": ("docs/t.md", "fixed in L-" + "4117\n"),
            "infra": ("docs/i.md", "id " + UUID + "\n"),
            "secret": ("tools/s.py", 'k = "' + "AK" + "IA" + "C" * 16 + '"\n'),
        }
        for cat, (rel, text) in cases.items():
            with self.subTest(cat):
                r = Repo()
                try:
                    pin = subprocess.run(["git", "rev-parse", "HEAD"], cwd=r.root, capture_output=True, text=True).stdout.strip()
                    r.plant(rel, text)
                    blob = subprocess.run(["git", "rev-parse", "HEAD:" + rel], cwd=r.root, capture_output=True, text=True).stdout.strip()
                    allow = os.path.join(r.dir, "history-allow.txt")
                    with open(allow, "w") as f:
                        f.write(blob + " reviewed\n")
                    rc, out = r.scan(["--base", pin, "--history-allow", allow])
                    self.assertEqual(rc, 1, out)
                    self.assertRegex(out, r"history/" + re.escape(rel) + r"@\w+:\d+ ")
                finally:
                    r.close()

    def test_new_personal_identity_commit_refused_even_if_its_sha_is_allow_listed(self):
        pin = self.head()
        sha = self.personal_commit()
        self.r.commit("clean on top")
        allow = os.path.join(self.r.dir, "identity-allow.txt")
        with open(allow, "w") as f:
            f.write(sha + " reviewed\n")
        rc, out, _ = self.run_scan("--base", pin, "--identity-allow", allow)
        self.assertEqual(rc, 1, out)
        self.assertIn(".git/commit-" + sha[:8] + ":0 identity", out)

    def test_a_leak_in_the_first_commit_after_the_pin_is_seen_though_head_is_clean(self):
        pin = self.head()
        self.r.plant("tests/f.sh", "cp x " + USERS + "/someone/.claude" + "/hub/y\n")
        self.r.put("tests/f.sh", "clean\n")
        self.r.commit("remove it")
        self.r.commit("another")
        rc, out, _ = self.run_scan("--base", pin)
        self.assertEqual(rc, 1)
        self.assertIn(" path-leak", out)

    def test_stale_identity_allow_lines_are_not_reported_in_range_mode(self):
        pin = self.head()
        rc, out, _ = self.run_scan("--base", pin, "--identity-allow", self.pin_file(self.ZERO + " gone"))
        self.assertEqual((rc, out), (0, ""), out)
        rc, out, _ = self.run_scan("--identity-allow", self.pin_file(self.ZERO + " gone"))
        self.assertIn("identity-allow-stale", out)             # full mode still reports it

    # bad pin
    def test_bad_base_argument_exits_2(self):
        pin = self.head()
        self.r.commit("next")
        tree = subprocess.run(["git", "rev-parse", "HEAD^{tree}"], cwd=self.r.root, capture_output=True, text=True).stdout.strip()
        sh(["git", "checkout", "-q", "-b", "side", pin], self.r.root)
        self.r.commit("side only")
        side = self.head()
        sh(["git", "checkout", "-q", "-"], self.r.root)
        for bad in ("", pin[:12], pin.upper(), "z" * 40, self.ZERO, tree, side, pin + "\n" + pin, "HEAD", "--all"):
            with self.subTest(bad=bad[:14]):
                rc, out, err = self.run_scan("--base", bad)
                self.assertEqual(rc, 2, (out, err))
        self.assertEqual(self.run_scan("--base")[0], 2)

    def test_bad_pin_file_falls_back_to_scanning_all_history(self):
        self.personal_commit()
        pin = self.old_leak_then_pin()
        side_sha = None
        sh(["git", "checkout", "-q", "-b", "side", pin + "~1"], self.r.root)
        self.r.commit("side only")
        side_sha = self.head()
        sh(["git", "checkout", "-q", "-"], self.r.root)
        bads = {
            "missing": os.path.join(self.r.dir, "nope.txt"),
            "empty": self.pin_file(),
            "comments only": self.pin_file("# nothing"),
            "short": self.pin_file(pin[:12]),
            "two lines": self.pin_file(pin, pin),
            "not hex": self.pin_file("g" * 40),
            "unknown commit": self.pin_file(self.ZERO),
            "not an ancestor": self.pin_file(side_sha),
            "trailing junk": self.pin_file(pin + " reason"),
        }
        for name, path in bads.items():
            with self.subTest(name):
                rc, out, err = self.run_scan("--base-file", path)
                self.assertEqual(rc, 1, (out, err))
                self.assertIn("identity", out)
                self.assertIn("path-leak", out)
                self.assertIn("ALL history", err)

    def test_good_pin_file_with_comment_lines_uses_range_mode(self):
        self.personal_commit()
        pin = self.old_leak_then_pin()
        rc, out, err = self.run_scan("--base-file", self.pin_file("# reviewed by a person", "", pin))
        self.assertEqual((rc, out), (0, ""), (out, err))
        self.r.plant("docs/t.md", "fixed in L-" + "4117\n")
        self.assertEqual(self.run_scan("--base-file", self.pin_file(pin))[0], 1)

    # the real script and the real shipped pin
    def layout(self, pin_lines):
        """a temp repo holding a copy of tools/ (the scripts under test), a pin file and one old personal commit"""
        tools = os.path.join(self.r.root, "tools")
        for n in ("prepush-check.sh", "scan.py", "ship-list.sh", "ship_list.py"):
            shutil.copy(os.path.join(HERE, n), os.path.join(tools, n))
        self.personal_commit()
        pin = self.head()
        self.r.put("tools/history-base.txt", "".join(l.replace("PIN", pin) + "\n" for l in pin_lines))
        self.r.sync_manifest()
        self.r.commit("pin")
        return os.path.join(tools, "prepush-check.sh"), pin

    def prepush(self, script, *args):
        env = {k: v for k, v in os.environ.items() if k != "HUBSTARTER_DENYLIST"}
        return subprocess.run(["bash", script, "--generic-only", *args], env=env, capture_output=True, text=True)

    def test_prepush_defaults_to_range_and_full_flag_restores_all_history(self):
        script, pin = self.layout(["# base", "PIN"])
        r = self.prepush(script)
        self.assertNotIn("identity", r.stdout)
        self.assertIn("range mode", r.stderr)
        r = self.prepush(script, "--full")
        self.assertEqual(r.returncode, 1)
        self.assertIn(" identity", r.stdout)
        self.assertIn("ALL history", r.stdout)
        r = self.prepush(script, "--full", "--generic-only")      # order does not matter
        self.assertIn(" identity", r.stdout)

    def test_prepush_with_a_bad_or_missing_pin_scans_everything(self):
        for lines in (["PIN", "PIN"], ["abc123"], ["# only a comment"], []):
            with self.subTest(lines=lines):
                script, _ = self.layout(lines)
                r = self.prepush(script)
                self.assertEqual(r.returncode, 1)
                self.assertIn(" identity", r.stdout)
                self.assertIn("ALL history", r.stderr)
                self.r.close()
                self.r = Repo()
        script, _ = self.layout(["PIN"])
        os.remove(os.path.join(self.r.root, "tools", "history-base.txt"))
        self.r.commit("pin gone")
        r = self.prepush(script)
        self.assertIn(" identity", r.stdout)

    def test_prepush_rejects_unknown_arguments(self):
        script, _ = self.layout(["PIN"])
        self.assertEqual(self.prepush(script, "--nonsense").returncode, 2)

    def test_the_shipped_pin_is_frozen(self):
        import hashlib
        path = os.path.join(HERE, "history-base.txt")
        with open(path) as fh:
            self.assertEqual([ln for ln in fh if ln.strip() and not ln.startswith("#")], [])
        with open(path, "rb") as fh:
            self.assertEqual(hashlib.sha256(fh.read()).hexdigest(),
                             "15235a6eaf4173137f74524c646e4cc4db17cbc920db168660bbb03ea4513b47",
                             "history-base.txt changed: this pin and a reviewer must change with it")


def read_pin(path):
    with open(path) as fh:
        return [ln.strip() for ln in fh if ln.strip() and not ln.startswith("#")][0]


class Usage(unittest.TestCase):
    def test_missing_denylist_fails_closed(self):
        r = Repo()
        try:
            p = subprocess.run([sys.executable, SCAN, "--root", r.root, "--denylist", "/nonexistent/x"],
                               capture_output=True, text=True)
            self.assertEqual(p.returncode, 2)
        finally:
            r.close()

    def test_missing_manifest_fails_closed(self):
        r = Repo()
        try:
            p = subprocess.run([sys.executable, SCAN, "--root", r.root, "--manifest", "/nonexistent/m"],
                               capture_output=True, text=True)
            self.assertEqual(p.returncode, 2)
        finally:
            r.close()

    def test_the_scanner_passes_itself(self):
        # tools/ of this repo, scanned as a tree: no self-trip (no manifest/git here)
        d = tempfile.mkdtemp()
        try:
            shutil.copytree(HERE, os.path.join(d, "tools"), ignore=shutil.ignore_patterns("__pycache__"))
            p = subprocess.run([sys.executable, SCAN, "--root", d, "--no-git"], capture_output=True, text=True)
            self.assertEqual((p.returncode, p.stdout), (0, ""), p.stdout)
        finally:
            shutil.rmtree(d)


class DynamicCompoundFolderName(unittest.TestCase):
    """Project folders are read through dynamic_terms() from a temp HOME (synthetic names only).
    A two-part name with no distinctive part must not fire on ordinary prose spacing but must fire as one
    identifier; every other dynamic name keeps the loose match."""
    sys.path.insert(0, HERE)
    import scan as _scan

    SHORT = "fro" + "-scr" + "aper"                 # 3-char part + plain word: relaxed
    FOUR = "frob" + "-scr" + "aper"                 # 4-char non-plain part: loose
    THREE = "fro" + "-bnizq-scr" + "aper"           # three parts with a long one: loose
    SINGLE = "fro" + "bnizq"                        # single word: loose

    def setUp(self):
        import tempfile
        self._tmp = tempfile.mkdtemp()
        os.makedirs(os.path.join(self._tmp, "projects"))
        for f in (self.SHORT, self.FOUR, self.THREE, self.SINGLE):
            os.makedirs(os.path.join(self._tmp, "projects", f))
        self._home = os.environ.get("HOME")
        os.environ["HOME"] = self._tmp
        self.addCleanup(self._restore)
        self.terms = {t.key: t for t in self._scan.dynamic_terms(os.path.join(self._tmp, "elsewhere"))}

    def _restore(self):
        import shutil
        os.environ["HOME"] = self._home
        shutil.rmtree(self._tmp, ignore_errors=True)

    def hits(self, folder, text):
        t = self.terms[self._scan.fold(folder)]
        return self._scan.term_hits_in_text([t], text)

    def test_wiring_marks_only_the_narrow_class(self):
        sc = self._scan
        self.assertTrue(self.terms[sc.fold(self.SHORT)].ident)
        for f in (self.FOUR, self.THREE, self.SINGLE):
            self.assertFalse(self.terms[sc.fold(f)].ident, f)

    def test_class_rule(self):
        sc = self._scan
        self.assertTrue(sc.is_plain_compound(self.SHORT))
        self.assertTrue(sc.is_plain_compound("fr-scr" + "aper-ab"))
        for f in (self.FOUR, self.THREE, self.SINGLE):
            self.assertFalse(sc.is_plain_compound(f), f)

    def test_ordinary_prose_with_spaces_is_allowed(self):
        self.assertEqual(self.hits(self.SHORT, "a fro scraper helper\nthe fro, scraper\n"), [])

    def test_identifier_forms_are_blocked(self):
        for form in ("fro" + "-scr" + "aper", "fro" + "_scr" + "aper", "fro" + "scr" + "aper",
                     "fro" + ".scr" + "aper", "fro" + "--scr" + "aper", "myFro" + "Scr" + "aper",
                     "FRO" + "-SCR" + "APER"):
            self.assertEqual(self.hits(self.SHORT, "x = " + form + "\n"), [1], form)

    def test_invisible_and_typographic_separators_are_blocked(self):
        for sep in ("\u200b-", "-\u200b", "\u200d", "\u200c-", "\u00ad", "\u2010", "\u2011", "\u2013",
                    "\u2014", "\u2212", "\uff0d", "\u2060"):
            self.assertEqual(self.hits(self.SHORT, "x = fro" + sep + "scr" + "aper\n"), [1], repr(sep))
        self.assertEqual(self.hits(self.SHORT, "x = fr\u200bo-scr" + "aper\n"), [1])
        self.assertEqual(self.hits(self.SHORT, "x = fro-scr\u200b" + "aper\n"), [1])

    def test_confusable_letters_are_blocked(self):
        self.assertEqual(self.hits(self.SHORT, "x = fr\u043e-scr" + "aper\n"), [1])

    def test_embedded_in_a_longer_word_is_allowed(self):
        self.assertEqual(self.hits(self.SHORT, "xfro" + "-scr" + "aperz\n"), [])

    def test_other_dynamic_names_keep_the_loose_match(self):
        for f in (self.FOUR, self.THREE):
            a, rest = f.split("-", 1)
            for sep in (" ", "/", ": ", "\" + \""):
                self.assertEqual(self.hits(f, "see " + a + sep + rest.replace("-", sep) + " here\n"), [1], (f, sep))
            self.assertEqual(self.hits(f, "x = " + f + "\n"), [1], f)
        self.assertEqual(self.hits(self.SINGLE, "x = fro" + "bnizq\n"), [1])
        self.assertEqual(self.hits(self.SINGLE, "x = fr\u200bo" + "bnizq\n"), [1])

    def test_zero_width_inside_loose_names_is_blocked(self):
        self.assertEqual(self.hits(self.FOUR, "x = fr\u200bob-scr" + "aper\n"), [1])


    def test_a_compound_with_no_plain_word_is_not_relaxed(self):
        # guard: at least one plain word. Without it every short-parted name would be matched only as an identifier.
        sc = self._scan
        self.assertFalse(sc.is_plain_compound("fro" + "-bnz"))
        self.assertFalse(sc.is_plain_compound("ab-cd-ef"))
        os.makedirs(os.path.join(self._tmp, "projects", "fro" + "-bnz"))
        terms = {t.key: t for t in sc.dynamic_terms(os.path.join(self._tmp, "elsewhere"))}
        t = terms[sc.fold("fro" + "-bnz")]
        self.assertFalse(t.ident)
        self.assertEqual(sc.term_hits_in_text([t], "see fro bnz here\n"), [1])

    def test_identifier_needs_a_leading_word_edge(self):
        self.assertEqual(self.hits(self.SHORT, "xfro" + "-scr" + "aper\n"), [])
        self.assertEqual(self.hits(self.SHORT, "9fro" + "-scr" + "aper\n"), [])
        self.assertEqual(self.hits(self.SHORT, "fro" + "-scr" + "aperz\n"), [])
        self.assertEqual(self.hits(self.SHORT, "x-fro" + "-scr" + "aper\n"), [1])

    def test_system_dictionary_is_read_once(self):
        sc = self._scan
        sc._system_words.cache_clear()
        real = open
        calls = []

        def counting(path, *a, **k):
            calls.append(path)
            return real(path, *a, **k)
        import builtins
        builtins.open = counting
        try:
            for _ in range(5):
                sc.is_plain_word("scr" + "aper")
        finally:
            builtins.open = real
        self.assertLessEqual(len([c for c in calls if "dict" in str(c)]), 1)


if __name__ == "__main__":
    unittest.main(verbosity=1)
