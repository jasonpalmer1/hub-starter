#!/usr/bin/env python3
"""Tests for secret-scan.py. Every case stages a change in a throwaway git repo and runs the hook.
Every secret-shaped fixture is assembled at runtime, so this file itself holds no secret-shaped text."""
import os
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCAN = os.environ.get("SECRET_SCAN", os.path.join(HERE, "secret-scan.py"))
INSTALL = os.environ.get("INSTALL_HOOK", os.path.join(HERE, "install-hook.sh"))


def j(*pieces):
    return "".join(pieces)


K_PASS, K_CODE, K_TOKEN, K_APIKEY = j("pa", "ss", "word"), j("pa", "ss", "code"), j("to", "ken"), j("api", "key")
CRED_VAL = j("Qz7w", "Lp4R", "m9Kd", "2Xv8", "Tn")          # 18 chars, letters and digits
ENV_VAL = j("hK3p", "Z9mW", "2qR7", "vN5x", "B")
PUBLIC_VAL = j("pub3", "X7co", "nfig", "9Val", "ue2")
REAL_VAL = j("aB3x", "Y9kL", "m2Qw")


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.home = os.path.join(self.tmp, "home")
        self.store = os.path.join(self.tmp, "store")
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.home)
        os.makedirs(os.path.join(self.store, "proj"))
        self.env = dict(os.environ, HOME=self.home, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
                        PYTHONDONTWRITEBYTECODE="1")
        for k in ("SECRET_SCAN_VALUE_FILES", "SECRET_SCAN_ENV_ROOTS", "SECRET_SCAN_ALLOW_PATHS"):
            self.env.pop(k, None)
        self.git("init", "-q", self.repo)
        self.git("-C", self.repo, "config", "user.email", "t@example.com")
        self.git("-C", self.repo, "config", "user.name", "t")
        self.write("base.txt", "base\n")
        self.git("-C", self.repo, "add", "base.txt")
        self.git("-C", self.repo, "commit", "-qm", "base", "--no-verify")
        # a notes file with a login and a hostname next to a real-looking value, and a .env
        with open(os.path.join(self.store, "notes.md"), "w") as f:
            f.write("login: someone@example.com\nhost: app-9.example.com\n%s: %s\n"
                    "Full report: 2026-07-28-audit-and-fix-run2.md\n" % (K_CODE, CRED_VAL))
        with open(os.path.join(self.store, "proj", ".env"), "w") as f:
            f.write("API_%s=%s\nSITE_URL=%s\n" % (K_TOKEN.upper(), ENV_VAL, PUBLIC_VAL))
        self.stores = {"SECRET_SCAN_VALUE_FILES": os.path.join(self.store, "notes.md"),
                       "SECRET_SCAN_ENV_ROOTS": self.store}

    def tearDown(self):
        for root, dirs, files in os.walk(self.tmp, topdown=False):
            for f in files:
                os.remove(os.path.join(root, f))
            for d in dirs:
                p = os.path.join(root, d)
                os.rmdir(p) if not os.path.islink(p) else os.remove(p)
        os.rmdir(self.tmp)

    def git(self, *a, **kw):
        return subprocess.run(["git", *a], capture_output=True, text=True, env=self.env, **kw)

    def write(self, rel, text):
        p = os.path.join(self.repo, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(text)

    def reset(self):
        self.git("-C", self.repo, "reset", "-q", "--hard")
        self.git("-C", self.repo, "clean", "-qfd")

    def run_hook(self, env=None, use_stores=True):
        e = dict(self.env)
        e.update(self.stores if use_stores else {})
        e.update(env or {})
        r = subprocess.run([sys.executable, SCAN], cwd=self.repo, capture_output=True, text=True, env=e)
        return r.returncode, r.stderr

    def scan(self, rel, text, **kw):
        self.reset()
        self.write(rel, text + "\n")
        self.git("-C", self.repo, "add", "-f", rel)
        return self.run_hook(**kw)

    def blocked(self, rel, text, **kw):
        rc, err = self.scan(rel, text, **kw)
        self.assertEqual(rc, 1, "should be blocked: %s" % rel)
        return err

    def allowed(self, rel, text, **kw):
        rc, err = self.scan(rel, text, **kw)
        self.assertEqual(rc, 0, "should pass: %s\n%s" % (rel, err))


class Formats(Case):
    def test_credential_formats_are_blocked(self):
        for label, text in (
                ("anthropic", "key: " + j("sk-", "ant-", "api03-", "AbCdEfGhIjKlMnOpQrStUv")),
                ("openai", j("sk-", "proj-", "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6")),
                ("github", j("gh", "p_", "A" * 36)),
                ("github fine-grained", j("github", "_pat_", "A1" * 25)),
                ("aws", j("AK", "IA", "IOSFODNN7", "EXAMPLE")),
                ("stripe", j("sk_", "live_", "abcdefghij", "0123456789")),
                ("slack", j("xo", "xb-", "1234567890-", "abcdefghij")),
                ("google", j("AI", "za", "A" * 35)),
                ("private key", j("-----", "BEGIN ", "RSA ", "PRIV", "ATE ", "KEY", "-----")),
                ("jwt", j("eyJ", "hbGciOiJIUzI1NiJ9", ".eyJ", "zdWIiOiIxMjM0NTY3", ".SflKxwRJSMeKKF2QT4fwpM"))):
            err = self.blocked("a.md", text)
            self.assertIn("a.md:1", err, label)

    def test_credential_assignment_with_a_real_looking_value(self):
        self.blocked("a.sh", "%s=%s" % (K_PASS, j("Tr0u", "b4do", "r3xY", "z")))
        self.blocked("a.md", "%s: %s" % (K_APIKEY, j("ABCD", "-1234", "-EFGH", "-5678")))

    def test_things_that_only_look_like_credentials_pass(self):
        self.allowed("a.md", "the %s SERVICE_API_%s is read from .env" % (K_TOKEN, K_TOKEN.upper()))
        self.allowed("a.md", "?%s=PLACEHOLDER-NOT-A-REAL-%s" % (K_TOKEN, K_TOKEN.upper()))
        self.allowed("a.js", "const %s = process.env.API_%s" % (K_TOKEN, K_TOKEN.upper()))
        self.allowed("a.md", "%s 2026-09-23 refreshed" % K_TOKEN)
        self.allowed("a.md", "%s sauce: review done, nothing touched." % j("Sec", "ret"))
        self.allowed("a.md", "%s: your-%s-here" % (K_APIKEY, K_APIKEY))

    def test_env_name_then_a_real_value_on_one_line_is_still_caught(self):
        self.blocked("a.md", "%s SERVICE_API_%s then %s=%s" % (K_TOKEN, K_TOKEN.upper(), K_TOKEN, REAL_VAL))


class Stores(Case):
    def test_a_value_from_a_value_file_is_blocked(self):
        self.blocked("notes.md", "pasted %s by mistake" % CRED_VAL)

    def test_a_secret_named_env_value_is_blocked_and_a_public_one_is_not(self):
        self.blocked("notes.md", "curl -H x:%s" % ENV_VAL)
        self.allowed("notes.md", "config %s" % PUBLIC_VAL)

    def test_hostnames_and_file_names_in_the_notes_are_not_secrets(self):
        self.allowed("notes.md", "see https://app-9.example.com/")
        self.allowed("notes.md", "see 2026-07-28-audit-and-fix-run2.md")

    def test_no_stores_configured_means_only_the_shape_rules_apply(self):
        self.allowed("notes.md", "pasted %s by mistake" % CRED_VAL, use_stores=False)

    def test_env_roots_default_to_the_repo_being_committed(self):
        with open(os.path.join(self.repo, ".env"), "w") as f:
            f.write("API_%s=%s\n" % (K_TOKEN.upper(), ENV_VAL))
        self.reset_keep_env()
        self.write("notes.md", "x %s\n" % ENV_VAL)
        self.git("-C", self.repo, "add", "notes.md")
        rc, err = self.run_hook(use_stores=False)
        self.assertEqual(rc, 1, err)

    def reset_keep_env(self):
        self.git("-C", self.repo, "reset", "-q")

    def test_missing_store_paths_are_ignored(self):
        self.allowed("notes.md", "nothing here", env={"SECRET_SCAN_VALUE_FILES": "/no/such/file",
                                                       "SECRET_SCAN_ENV_ROOTS": "/no/such/dir"})

    def test_a_6kb_secret_in_a_store_does_not_stall_the_commit(self):
        import random, string
        r = random.Random(7)
        long_val = "".join(r.choice(string.ascii_letters + string.digits + "-_") for _ in range(6000))
        with open(os.path.join(self.store, "proj", ".env"), "a") as f:
            f.write("LONG_%s=%s\n" % (K_TOKEN.upper(), long_val))
        t0 = time.time()
        self.allowed("notes.md", "nothing secret here")
        self.assertLess(time.time() - t0, 2.0)
        self.blocked("notes.md", "x " + long_val)


class Paths(Case):
    def test_secret_files_are_blocked_by_path(self):
        for rel in (".env", "proj/.env", ".env.local", ".env.production", ".dev.vars", "ssh/id_rsa", "id_ed25519"):
            err = self.blocked(rel, "X=1")
            self.assertIn("secret-file-path", err, rel)

    def test_example_files_and_public_keys_pass(self):
        for rel in (".env.example", "proj/.env.sample", ".env.template", "id_rsa.pub", "environment.md",
                    ".env.production.example", ".dev.vars.example", "prod.env.sample", ".envrc.example"):
            self.allowed(rel, "X=")

    def test_every_usual_secrets_file_name_is_blocked_with_a_plain_line_inside(self):
        # A generic NAME=value line is not caught by the content rules, so only the path saves it.
        for rel in (".env.production.local", ".env.development.local", ".env.local.bak-1790026563",
                    ".env.test.local", ".env.staging", ".env-backup", ".env.bak",
                    ".dev.vars.production", ".dev.vars.staging.bak", "secrets.env", "prod.env",
                    "deploy/prod.env", ".envrc", "sub/.envrc", ".envrc.local"):
            err = self.blocked(rel, "FOO=aaaaaaaaaaaaaaaaaaaa")
            self.assertIn("secret-file-path", err, rel)

    def test_odd_file_names_are_still_scanned(self):
        secret = j("gh", "p_", "A" * 36)
        for rel in ("my notes.md", 'we"ird.md', "café/nötes.md"):
            err = self.blocked(rel, secret)
            self.assertIn(":1:", err, rel)


class Diff(Case):
    def test_only_added_lines_count(self):
        self.write("legacy.md", "old %s\n" % CRED_VAL)
        self.git("-C", self.repo, "add", "legacy.md")
        self.git("-C", self.repo, "commit", "-qm", "legacy", "--no-verify")
        self.write("legacy.md", "old %s\nnew clean line\n" % CRED_VAL)
        self.git("-C", self.repo, "add", "legacy.md")
        self.assertEqual(self.run_hook()[0], 0)

    def test_line_numbers_point_at_the_added_line(self):
        self.write("multi.md", "one\ntwo\nthree\n")
        self.git("-C", self.repo, "add", "multi.md")
        self.git("-C", self.repo, "commit", "-qm", "multi", "--no-verify")
        self.write("multi.md", "one\ntwo\nthree\nfour %s\n" % j("gh", "p_", "A" * 36))
        self.git("-C", self.repo, "add", "multi.md")
        rc, err = self.run_hook()
        self.assertEqual(rc, 1)
        self.assertIn("multi.md:4:", err)

    def test_an_added_line_that_looks_like_a_file_header_is_still_scanned(self):
        self.blocked("a.md", "+++ b/fake\n%s" % j("gh", "p_", "A" * 36))
        self.blocked("a.md", "++ %s" % j("gh", "p_", "A" * 36))     # diff line starts with "+++ "

    def test_a_renamed_then_edited_file_is_scanned(self):
        self.write("old.md", "line\n" * 5)
        self.git("-C", self.repo, "add", "old.md")
        self.git("-C", self.repo, "commit", "-qm", "old", "--no-verify")
        self.git("-C", self.repo, "mv", "old.md", "new.md")
        self.write("new.md", "line\n" * 5 + j("gh", "p_", "A" * 36) + "\n")
        self.git("-C", self.repo, "add", "new.md")
        self.assertEqual(self.run_hook()[0], 1)

    def test_a_deleted_file_and_an_empty_stage_pass(self):
        self.git("-C", self.repo, "rm", "-q", "base.txt")
        self.assertEqual(self.run_hook()[0], 0)
        self.reset()
        self.assertEqual(self.run_hook()[0], 0)

    def test_the_fixture_allowlist_exempts_only_the_listed_path(self):
        fake = j("gh", "p_", "A" * 36)
        self.allowed("tests/fixture.py", fake, env={"SECRET_SCAN_ALLOW_PATHS": "tests/fixture.py"})
        self.blocked("src/real.py", fake, env={"SECRET_SCAN_ALLOW_PATHS": "tests/fixture.py"})
        self.blocked("tests/fixture.py", fake)        # nothing is exempt by default
        self.allowed("my notes.md", fake, env={"SECRET_SCAN_ALLOW_PATHS": "my notes.md"})   # a name with a space


class Output(Case):
    def test_never_prints_the_value(self):
        self.reset()
        self.write("notes.md", "a %s b %s\n" % (CRED_VAL, ENV_VAL))
        self.git("-C", self.repo, "add", "notes.md")
        rc, err = self.run_hook()
        self.assertEqual(rc, 1)
        self.assertNotIn(CRED_VAL, err)
        self.assertNotIn(ENV_VAL, err)
        self.assertIn("notes.md:1", err)
        self.assertIn("value-from-secret-store", err)

    def test_never_prints_a_matched_format_either(self):
        secret = j("gh", "p_", "A" * 36)
        self.reset()
        self.write("a.md", secret + "\n")
        self.git("-C", self.repo, "add", "a.md")
        rc, err = self.run_hook()
        self.assertNotIn(secret, err)
        self.assertIn("github-token", err)

    def test_more_than_twenty_hits_are_summarised(self):
        self.reset()
        self.write("a.md", (j("gh", "p_", "A" * 36) + "\n") * 25)
        self.git("-C", self.repo, "add", "a.md")
        rc, err = self.run_hook()
        self.assertIn("and 5 more", err)
        self.assertEqual(len([l for l in err.splitlines() if l.startswith("  a.md")]), 20)

    def test_a_crash_blocks_the_commit_and_says_so(self):
        # git itself cannot run: the scan must fail closed, not wave the commit through
        rc, err = self.run_hook(env={"PATH": os.path.dirname(sys.executable)})
        self.assertEqual(rc, 1)
        self.assertIn("scan itself failed", err)

GH = j("gh", "p_", "A" * 36)


class Hardening(Case):
    """Cases the first version missed: a stranger's ordinary git config, a git that errors, files
    git calls binary, and the usual spelling of a credential name (DB_PASSWORD)."""

    def write_bytes(self, rel, data):
        p = os.path.join(self.repo, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)

    def stage_bytes(self, rel, data, attrs=None):
        self.reset()
        if attrs:
            self.write(".gitattributes", attrs)
            self.git("-C", self.repo, "add", ".gitattributes")
        self.write_bytes(rel, data)
        self.git("-C", self.repo, "add", "-f", rel)
        return self.run_hook()

    def test_user_diff_prefix_settings_do_not_blind_it(self):
        for cfg in ((("diff.noprefix", "true"),), (("diff.mnemonicPrefix", "true"),),
                    (("diff.srcPrefix", "SRC/"), ("diff.dstPrefix", "DST/")),
                    (("diff.noprefix", "true"), ("diff.mnemonicPrefix", "true"))):
            for k, v in cfg:
                self.git("-C", self.repo, "config", k, v)
            rc, err = self.scan("new.md", "t=" + GH)
            self.assertEqual(rc, 1, "new file, config %s" % (cfg,))
            self.assertIn("new.md:1", err)
            # an edit of a tracked file takes the other path through the scanner
            self.reset()
            self.write("m.md", "one\n")
            self.git("-C", self.repo, "add", "m.md")
            self.git("-C", self.repo, "commit", "-qm", "m", "--no-verify")
            self.write("m.md", "one\ntwo " + GH + "\n")
            self.git("-C", self.repo, "add", "m.md")
            rc, err = self.run_hook()
            self.assertEqual(rc, 1, "edit, config %s" % (cfg,))
            self.assertIn("m.md:2:", err)
            for k, _ in cfg:
                self.git("-C", self.repo, "config", "--unset", k)

    def test_a_textconv_or_external_diff_that_errors_cannot_hide_a_secret(self):
        self.git("-C", self.repo, "config", "diff.rot.textconv", "false")
        self.git("-C", self.repo, "config", "diff.rot.command", "false")
        rc, err = self.stage_bytes("a.sec", ("t=" + GH + "\n").encode(), attrs="*.sec diff=rot\n")
        self.assertEqual(rc, 1)

    def fake_git(self, failing):
        """A `git` earlier on PATH that fails for one subcommand and runs the real git otherwise."""
        real = subprocess.run(["which", "git"], capture_output=True, text=True).stdout.strip()
        d = os.path.join(self.tmp, "fakegit")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "git"), "w") as f:
            f.write('#!/bin/sh\nfor a in "$@"; do [ "$a" = "%s" ] && { echo boom >&2; exit 128; }; done\n'
                    'exec "%s" "$@"\n' % (failing, real))
        os.chmod(os.path.join(d, "git"), 0o755)
        return {"PATH": d + os.pathsep + os.environ["PATH"]}

    def test_any_failing_git_call_blocks_the_commit(self):
        for sub in ("diff", "cat-file", "rev-parse"):
            self.reset()
            self.write("a.md", "t=" + GH + "\n")
            self.git("-C", self.repo, "add", "a.md")
            rc, err = self.run_hook(env=self.fake_git(sub))
            self.assertEqual(rc, 1, sub)
            self.assertIn("scan itself failed", err, sub)
        # a clean change with a failing git is blocked too: failure is never read as "nothing to scan"
        self.reset()
        self.write("ok.md", "fine\n")
        self.git("-C", self.repo, "add", "ok.md")
        self.assertEqual(self.run_hook(env=self.fake_git("diff"))[0], 1)

    def test_a_secret_in_a_file_git_calls_binary_is_still_caught(self):
        line = ("t=" + GH + "\n").encode()
        cases = (
            ("nul.txt", b"x\0\n" + line, None),
            ("utf16.txt", ("t=" + GH + "\n").encode("utf-16"), None),             # BOM
            ("utf16le.txt", ("t=" + GH + "\n").encode("utf-16-le"), None),        # no BOM
            ("utf16be.txt", ("t=" + GH + "\n").encode("utf-16-be"), None),
            ("utf32.txt", ("t=" + GH + "\n").encode("utf-32"), None),
            ("forced.txt", line, "*.txt binary\n"),
            ("nodiff.dat", line, "*.dat -diff\n"),
            ("edge.bin", b"abc\0" + GH.encode() + b"\n", None),          # NUL right before the secret: a word boundary only the raw view sees
            ("crlf.txt", ("a\r\nt=" + GH + "\r\n").encode(), None),
        )
        for rel, data, attrs in cases:
            rc, err = self.stage_bytes(rel, data, attrs)
            self.assertEqual(rc, 1, rel)
            self.assertIn(rel, err)
            self.assertNotIn(GH, err)

    def test_a_secret_added_to_an_existing_binary_file_is_caught(self):
        self.write_bytes("blob.bin", b"\0\1\2 start\n")
        self.git("-C", self.repo, "add", "blob.bin")
        self.git("-C", self.repo, "commit", "-qm", "bin", "--no-verify")
        self.write_bytes("blob.bin", b"\0\1\2 start\n" + ("t=" + GH + "\n").encode())
        self.git("-C", self.repo, "add", "blob.bin")
        self.assertEqual(self.run_hook()[0], 1)

    def test_clean_binary_and_utf16_files_pass(self):
        self.assertEqual(self.stage_bytes("img.bin", bytes(range(256)) * 40)[0], 0)
        self.assertEqual(self.stage_bytes("note.txt", "just a note\n".encode("utf-16"))[0], 0)

    def test_a_submodule_style_gitlink_does_not_crash_the_scan(self):
        self.reset()
        self.git("-C", self.repo, "update-index", "--add", "--cacheinfo",
                 "160000,%s,sub" % ("1" * 40))
        rc, err = self.run_hook()
        self.assertEqual(rc, 0, err)

    def test_prefixed_and_suffixed_credential_names_are_caught(self):
        v = j("xK9p", "Q2mZ", "8vL4", "nB7r")
        for text in ("DB_%s = \"%s\"" % (K_PASS.upper(), v),
                     "MY_%s=\"%s\"" % (j("SEC", "RET"), v),
                     "{\"client_%s\": \"%s\"}" % (j("sec", "ret"), v),
                     "OPENAI_API_%s=%s" % (j("K", "EY"), v),
                     "export STRIPE-%s: %s" % (K_TOKEN, v),
                     "aws_%s_access_key = %s" % (j("sec", "ret"), v),
                     "%s_hash: %s" % (K_PASS, v),
                     "{\"client%s\": \"%s\"}" % (j("Sec", "ret"), v),
                     "const api%s = '%s'" % (j("K", "ey"), v),
                     "db%s: %s" % (j("Pass", "word"), v)):
            err = self.blocked("a.md", text)
            self.assertIn("credential-assignment", err, text)
            self.assertNotIn(v, err)

    def test_names_that_merely_contain_a_keyword_do_not_cry_wolf(self):
        for text in ("%s_count = 1234567890" % K_TOKEN,                       # number, not a secret
                     "%s_field = \"hash_column_name\"" % K_PASS,               # words, no digit
                     "TOKEN_FILE=token_v2_final.json",                         # a file name
                     "max_%ss: 4096" % K_TOKEN,                                # short
                     "DB_%s=${DB_%s}" % (K_PASS.upper(), K_PASS.upper()),     # placeholder
                     "SERVICE_API_%s=process.env.X1_Y2_Z3" % K_TOKEN.upper(),
                     "my_%s_header = X-Api-Key-Header" % K_APIKEY,
                     "the %s_expiry 2026-09-23 refreshed" % K_TOKEN,
                     "pre%sed text here and %ss are fine" % (K_TOKEN, K_PASS),
                     "client%s: your-value-here" % j("Sec", "ret"),
                     "client%s: token_v2_final.json" % j("Sec", "ret"),
                     "client%s: plainwordsonlyhere" % j("Sec", "ret"),
                     "MY_%s = some-plain-words-here" % K_PASS.upper()):     # prefixed name, wordy value
            self.allowed("a.md", text)

    def test_the_allowlist_never_exempts_a_secrets_file_path(self):
        self.blocked(".env", "A=1", env={"SECRET_SCAN_ALLOW_PATHS": ".env"})

    def test_more_credential_shapes(self):
        self.blocked("a.md", j("npm", "_", "A1b2" * 9))
        self.blocked("a.md", j("S", "G.", "A1b2C3d4E5f6G7h8I9", ".", "A1b2C3d4E5f6G7h8I9"))


class Speed(Case):
    """One long line (a hex dump, a slug, a minified file) must never make the hook crawl."""

    def timed(self, text, rel="dump.txt"):
        self.reset()
        self.write(rel, text + "\n")
        self.git("-C", self.repo, "add", "-f", rel)
        e = dict(self.env)
        t0 = time.time()
        try:
            r = subprocess.run([sys.executable, SCAN], cwd=self.repo, capture_output=True, text=True,
                               env=e, timeout=30)
        except subprocess.TimeoutExpired:
            self.fail("hook took more than 30 s on %r..." % text[:12])
        return time.time() - t0, r.returncode

    def test_a_60kb_one_line_chain_scans_fast(self):
        for unit in ("3F-", "a-", "a_", "-password", "password-", "-token", "Secret", "aToken", "-eyJ",
                     "x-api-key", "-sk-ant-", "a1-"):
            text = (unit * (60000 // len(unit)))[:60000]
            took, rc = self.timed(text)
            self.assertLess(took, 2.0, "%r took %.1fs" % (unit, took))

    def test_chain_bounds_still_catch_real_compound_names(self):
        for line in ("a-b-DB_%s = %s" % (K_PASS, CRED_VAL), "x-DB_%s=%s" % (K_PASS.upper(), CRED_VAL),
                     "--%s=%s" % (K_PASS, CRED_VAL), "A_B_C_D_E_F_G_%s=%s" % (K_TOKEN.upper(), CRED_VAL)):
            self.blocked("n.txt", line)


class FlagGuards(Case):
    """The fixed git flags and the type-change filter are load-bearing, not belt-and-braces: each
    test here fails if its guard is deleted. All edit an EXISTING tracked file (blob-to-blob diff)."""

    def edit_existing(self, attrs=None, config=()):
        self.reset()
        for k, v in config:
            self.git("-C", self.repo, "config", k, v)
        if attrs:
            self.write(".gitattributes", attrs)
            self.git("-C", self.repo, "add", ".gitattributes")
        self.write("m.txt", "one\n")
        self.git("-C", self.repo, "add", "m.txt")
        self.git("-C", self.repo, "commit", "-qm", "m", "--no-verify")
        self.write("m.txt", "one\ntwo " + GH + "\n")
        self.git("-C", self.repo, "add", "m.txt")
        rc, err = self.run_hook()
        self.assertNotIn(GH, err)
        return rc

    def test_star_minus_diff_does_not_hide_an_edit(self):
        self.assertEqual(self.edit_existing(attrs="* -diff\n"), 1)

    def test_star_binary_does_not_hide_an_edit(self):
        self.assertEqual(self.edit_existing(attrs="* binary\n"), 1)

    def test_a_do_nothing_external_diff_does_not_hide_an_edit(self):
        self.assertEqual(self.edit_existing(config=(("diff.external", "/usr/bin/true"),)), 1)

    def test_a_textconv_driver_on_every_file_does_not_hide_an_edit(self):
        rc = self.edit_existing(attrs="* diff=rot\n", config=(("diff.rot.textconv", "tr a-z A-Z <"),))
        self.assertEqual(rc, 1)

    def test_a_symlink_replaced_by_a_file_with_a_secret_is_caught(self):
        self.reset()
        os.symlink("target.txt", os.path.join(self.repo, "link"))
        self.git("-C", self.repo, "add", "link")
        self.git("-C", self.repo, "commit", "-qm", "link", "--no-verify")
        os.remove(os.path.join(self.repo, "link"))
        self.write("link", "t=" + GH + "\n")
        self.git("-C", self.repo, "add", "link")
        self.assertIn("T", self.git("-C", self.repo, "diff", "--cached", "--name-status").stdout.split()[0])
        rc, err = self.run_hook()
        self.assertEqual(rc, 1)
        self.assertIn("link", err)


class Installer(Case):
    def hook_path(self):
        return os.path.join(self.repo, ".git", "hooks", "pre-commit")

    def install(self, repo=None):
        r = subprocess.run(["bash", INSTALL, repo or self.repo], capture_output=True, text=True, env=self.env)
        return r.returncode, r.stdout

    def test_install_wires_a_hook_that_really_blocks_a_commit(self):
        rc, out = self.install()
        self.assertEqual(rc, 0, out)
        self.assertTrue(os.access(self.hook_path(), os.X_OK))
        self.write("a.md", j("gh", "p_", "A" * 36) + "\n")
        self.git("-C", self.repo, "add", "a.md")
        r = self.git("-C", self.repo, "commit", "-m", "leak")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("BLOCKED by secret-scan", r.stderr)
        self.reset()
        self.write("b.md", "fine\n")
        self.git("-C", self.repo, "add", "b.md")
        self.assertEqual(self.git("-C", self.repo, "commit", "-m", "ok").returncode, 0)

    def test_install_twice_is_a_no_op(self):
        self.install()
        before = open(self.hook_path()).read()
        rc, out = self.install()
        self.assertEqual(rc, 0)
        self.assertIn("already wired", out)
        self.assertEqual(open(self.hook_path()).read(), before)

    def test_install_never_overwrites_someones_hook(self):
        os.makedirs(os.path.dirname(self.hook_path()), exist_ok=True)
        with open(self.hook_path(), "w") as f:
            f.write("#!/bin/sh\necho mine\n")
        rc, out = self.install()
        self.assertEqual(rc, 1)
        self.assertIn("not touching it", out)
        self.assertIn("secret-scan.py", out)
        self.assertEqual(open(self.hook_path()).read(), "#!/bin/sh\necho mine\n")

    def test_a_foreign_hook_that_only_mentions_the_scanner_is_not_wired(self):
        os.makedirs(os.path.dirname(self.hook_path()), exist_ok=True)
        here = os.path.dirname(INSTALL)
        line = 'python3 "%s/secret-scan.py" || exit 1' % here
        for body in ("#!/bin/sh\n# TODO wire secret-scan.py later\n",
                     "#!/bin/sh\nexit 0\n" + line + "\n",                  # line is dead code
                     "#!/bin/sh\n  exit\n" + line + "\n",
                     "#!/bin/sh\n# " + line + "\n",                          # commented out
                     "#!/bin/sh\necho 'secret-scan.py'\n"):
            with open(self.hook_path(), "w") as f:
                f.write(body)
            rc, out = self.install()
            self.assertEqual(rc, 1, body)
            self.assertIn("not touching it", out)
            self.assertNotIn("already wired", out)
            self.assertEqual(open(self.hook_path()).read(), body)

    def test_a_hook_with_our_exact_line_added_by_hand_counts_as_wired(self):
        os.makedirs(os.path.dirname(self.hook_path()), exist_ok=True)
        body = '#!/bin/sh\necho mine\npython3 "%s/secret-scan.py" || exit 1\n' % os.path.dirname(INSTALL)
        with open(self.hook_path(), "w") as f:
            f.write(body)
        rc, out = self.install()
        self.assertEqual(rc, 0)
        self.assertIn("already wired", out)
        self.assertEqual(open(self.hook_path()).read(), body)

    def test_a_dangling_symlink_hook_is_left_alone(self):
        os.makedirs(os.path.dirname(self.hook_path()), exist_ok=True)
        target = os.path.join(self.tmp, "shared-hook")
        os.symlink(target, self.hook_path())
        rc, out = self.install()
        self.assertEqual(rc, 1)
        self.assertFalse(os.path.exists(target))
        self.assertTrue(os.path.islink(self.hook_path()))

    def test_install_refuses_a_non_repo_and_touches_nothing(self):
        plain = os.path.join(self.tmp, "plain")
        os.makedirs(plain)
        rc, out = self.install(plain)
        self.assertEqual(rc, 2)
        self.assertEqual(os.listdir(plain), [])


if __name__ == "__main__":
    unittest.main(verbosity=1)
