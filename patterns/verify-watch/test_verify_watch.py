#!/usr/bin/env python3
"""Tests for verify_watch.py (grammar, allowlist, emit check, suggestion, day cap). Run: python3 test_verify_watch.py"""
import datetime
import importlib.util
import os
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp()
HOSTS = os.path.join(TMP, "hosts.txt")
with open(HOSTS, "w") as f:
    f.write("# allowlist\nexample.com   # main site\nshop.example.org\n")
os.environ["VERIFY_HOSTS"] = HOSTS
os.environ["VERIFY_KILL"] = os.path.join(TMP, "off")
spec = importlib.util.spec_from_file_location("verify_watch", os.path.join(HERE, "verify_watch.py"))
V = importlib.util.module_from_spec(spec)
spec.loader.exec_module(V)

GOOD = 'VERIFY: url=https://example.com/pricing has="Free plan included" days=3'


class T(unittest.TestCase):
    def test_good_line_parses(self):
        p, why = V.parse_line(GOOD)
        self.assertIsNone(why)
        self.assertEqual((p["host"], p["path"], p["days"]), ("example.com", "/pricing", 3))

    def test_days_default_and_range(self):
        self.assertEqual(V.parse_line('VERIFY: url=https://example.com/ has="abcd"')[0]["days"], 3)
        for d in ("0", "8", "x", "-1", "3.5"):
            self.assertIsNotNone(V.parse_line('VERIFY: url=https://example.com/ has="abcd" days=%s' % d)[1], d)

    def test_hostile_urls_refused(self):
        bad = ["http://example.com/", "https://evil.test/", "https://user@example.com/", "https://example.com:8443/",
               "https://EXAMPLE.com/", "https://example.com/#frag", "https://example.com.evil.test/"]
        for u in bad:
            line = 'VERIFY: url=%s has="abcd"' % u
            self.assertIsNotNone(V.parse_line(line)[1], u)

    def test_marker_length_and_chars(self):
        self.assertIsNotNone(V.parse_line('VERIFY: url=https://example.com/ has="abc"')[1])
        self.assertIsNotNone(V.parse_line('VERIFY: url=https://example.com/ has="%s"' % ("x" * 81))[1])
        self.assertIsNotNone(V.parse_line('VERIFY: url=https://example.com/ has="café menu"')[1])
        self.assertIsNotNone(V.parse_line(GOOD + "\nVERIFY: x")[1])

    def test_not_text_and_oversize(self):
        for bad in (None, 5, "", "x" * 700, "VERIFY: run=ls"):
            self.assertIsNotNone(V.parse_line(bad)[1])

    def test_empty_allowlist_fails_closed(self):
        self.assertIsNotNone(V.parse_line(GOOD, hosts=set())[1])

    def test_emit_refuses_marker_that_passes_everywhere(self):
        for m in ("html", "example.com", "pricing", "https", "<!doctype html>"):
            self.assertIsNotNone(V.emit_ok('VERIFY: url=https://example.com/pricing has="%s"' % m)[1], m)
        self.assertIsNone(V.emit_ok(GOOD)[1])

    def test_emit_refuses_marker_inside_url(self):
        self.assertIsNotNone(V.emit_ok('VERIFY: url=https://example.com/pricing-page has="pricing-pa"')[1])

    def test_suggest_needs_exactly_one_url_and_one_marker(self):
        t = 'Check https://example.com/pricing shows "Free plan included"'
        self.assertEqual(V.suggest(t), GOOD)
        self.assertIsNone(V.suggest('see https://example.com/a and https://example.com/b "Free plan"'))
        self.assertIsNone(V.suggest('see https://example.com/a "Free plan" "Other text"'))
        self.assertIsNone(V.suggest("see https://example.com/a with no quote"))
        self.assertIsNone(V.suggest('see http://example.com/a "Free plan"'))
        self.assertIsNone(V.suggest('see https://evil.test/a "Free plan"'))

    def test_suggest_skips_stranger_source_and_existing_line_and_kill(self):
        t = 'Check https://example.com/pricing shows "Free plan included"'
        self.assertIsNone(V.suggest(t, source="inbound-mail"))
        self.assertIsNone(V.suggest(t, done_means=GOOD))
        open(V.KILL_FILE, "w").close()
        try:
            self.assertIsNone(V.suggest(t))
        finally:
            os.remove(V.KILL_FILE)

    def test_day_cap_counts_today_only_and_fails_closed(self):
        now = datetime.datetime(2030, 1, 5, 12, 0, tzinfo=datetime.timezone.utc)
        ev = [{"event": "note", "note": GOOD, "ts": now.isoformat()}] * 10
        ev += [{"event": "note", "note": GOOD, "ts": (now - datetime.timedelta(days=1)).isoformat()}]
        ev += [{"event": "note", "note": "plain", "ts": now.isoformat()}]
        self.assertEqual(V.emits_today(ev, "2030-01-05"), 10)
        self.assertFalse(V.may_emit(ev, "2030-01-05"))
        self.assertTrue(V.may_emit(ev[:3], "2030-01-05"))
        self.assertEqual(V.emits_today([{"event": "note", "note": GOOD, "ts": "garbage"}], "2030-01-05"), V.EMIT_CAP_PER_DAY)


class GuardLimits(unittest.TestCase):
    def line(self, url_len, marker_len=4, days=True):
        url = "https://example.com/" + "a" * (url_len - len("https://example.com/"))
        return 'VERIFY: url=%s has="%s"%s' % (url, "m" * marker_len, " days=3" if days else "")

    def test_line_cap_is_400_and_url_cap_is_300(self):
        self.assertIsNone(V.parse_line(self.line(300, 4))[1])                 # url exactly 300: fine
        self.assertIn("url over 300", V.parse_line(self.line(301, 4))[1])
        ok = self.line(290, 80)                                               # 26 + 290 + 80 = 396
        self.assertLessEqual(len(ok), 400)
        self.assertIsNone(V.parse_line(ok)[1])
        long = self.line(295, 80)                                             # 401: url is fine, the line is not
        self.assertEqual(len(long), 401)
        self.assertIn("line over 400", V.parse_line(long)[1])
        self.assertEqual((V.MAX_LINE, V.MAX_URL), (400, 300))

    def test_env_kill_switch_vw_off(self):
        t = 'Check https://example.com/pricing shows "Free plan included"'
        self.assertEqual(V.suggest(t), GOOD)
        os.environ["VW_OFF"] = "1"
        try:
            self.assertTrue(V.killed())
            self.assertIsNone(V.suggest(t))
        finally:
            del os.environ["VW_OFF"]
        os.environ["VW_OFF"] = "0"                                            # only the exact value 1 kills
        try:
            self.assertFalse(V.killed())
        finally:
            del os.environ["VW_OFF"]

    def test_stranger_source_protection_cannot_be_env_removed(self):
        t = 'Check https://example.com/pricing shows "Free plan included"'
        for val in ("", "something-else", "robot,alarm"):
            os.environ["VERIFY_NO_SOURCES"] = val
            try:
                self.assertIsNone(V.suggest(t, source="inbound-mail"), val)
            finally:
                del os.environ["VERIFY_NO_SOURCES"]
        os.environ["VERIFY_NO_SOURCES"] = "robot"
        try:
            self.assertIsNone(V.suggest(t, source="robot"))                   # env can ADD a source
            self.assertEqual(V.suggest(t, source="alarm"), GOOD)
        finally:
            del os.environ["VERIFY_NO_SOURCES"]

    def test_day_cap_naive_timestamp_counts_as_utc(self):
        ev = [{"event": "note", "note": GOOD, "ts": "2030-01-05T12:00:00"}] * 10
        import time
        old_tz = os.environ.get("TZ")
        os.environ["TZ"] = "Pacific/Kiritimati"          # a machine zone far from UTC: a naive stamp must still mean UTC
        time.tzset()
        try:
            self.assertEqual(V.emits_today(ev, "2030-01-05"), 10)
        finally:
            if old_tz is None:
                del os.environ["TZ"]
            else:
                os.environ["TZ"] = old_tz
            time.tzset()


if __name__ == "__main__":
    unittest.main()
