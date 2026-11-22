#!/usr/bin/env python3
"""verify-watch: the strict "VERIFY line" grammar and the emit check.

A VERIFY line says how to prove a finished ticket is really live:

    VERIFY: url=https://example.com/page has="some visible text" days=3

This module only DECIDES whether a line is acceptable. It never fetches anything.
  parse_line(line)   -> (dict, None) or (None, reason). Closed grammar, never raises.
  emit_ok(line)      -> (dict, None) or (None, reason). Stricter: for lines a machine proposes.
  suggest(title, done_means, source)  -> a proposed line, or None when the text is not clear-cut.
  emits_today(events) / EMIT_CAP_PER_DAY  -> flood guard for proposed lines.
  killed()           -> True when the kill-switch file exists.

Allowlist: only hosts listed in $HUBKIT_HOME/verify-hosts.txt (one per line, # comments) are ever accepted.
An empty or missing file accepts nothing (fail closed).

CLI:  verify_watch.py check 'VERIFY: url=... has="..."'     exit 0 ok, 1 refused (reason printed)
      verify_watch.py suggest "title text with https://host/page and a \"marker\""
Kill switch: the file $HUBKIT_HOME/state/verify-watch-off, or the env VW_OFF=1.
Env: VW_OFF, HUBKIT_HOME (default ~/.claude/hubkit), VERIFY_TZ (default UTC; an IANA name such as Europe/Paris, for the day cap).
"""
import datetime
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

HOME = Path(os.environ.get("HUBKIT_HOME") or Path.home() / ".claude" / "hubkit")
HOSTS_FILE = Path(os.environ.get("VERIFY_HOSTS") or HOME / "verify-hosts.txt")
KILL_FILE = Path(os.environ.get("VERIFY_KILL") or HOME / "state" / "verify-watch-off")
MAX_LINE = 400
MAX_URL = 300
EMIT_CAP_PER_DAY = 10
DEFAULT_DAYS = 3
LINE_RE = re.compile(r'^VERIFY: url=(\S+) has="([^"]*)"(?: days=(\S+))?$')
_ALWAYS_PASS = frozenset({"html", "<!doctype", "<!doctype html>", "http", "https", "www"})
_URL_RE = re.compile(r"https?://[^\s\"'<>`\\]+", re.I)
_QUOTED_RE = re.compile(r'"([^"\n]{1,200})"')


def load_hosts():
    hosts = set()
    try:
        for ln in HOSTS_FILE.read_text().splitlines():
            ln = ln.split("#", 1)[0].strip()
            if ln:
                hosts.add(ln)
    except OSError:
        pass
    return hosts


def killed():
    return os.environ.get("VW_OFF") == "1" or KILL_FILE.exists()


def _printable(s):
    return all(32 <= ord(c) <= 126 for c in s)


def parse_line(line, hosts=None):
    """-> (dict, None) or (None, reason). Never raises."""
    try:
        if not isinstance(line, str):
            return None, "not text"
        if len(line) > MAX_LINE:
            return None, "line over %d chars" % MAX_LINE
        if "\n" in line or "\r" in line:
            return None, "embedded newline"
        if not _printable(line):
            return None, "non-ASCII or control character"
        if not line.startswith("VERIFY: "):
            return None, "does not start with 'VERIFY: '"
        m = LINE_RE.match(line)
        if not m:
            return None, 'not in the closed grammar (VERIFY: url=<https> has="<marker>" [days=N])'
        url, marker, days_s = m.groups()
        if len(url) > MAX_URL:
            return None, "url over %d chars" % MAX_URL
        if not url.startswith("https://"):
            return None, "url must be https"
        if len(marker) < 4:
            return None, "marker under 4 characters"
        if len(marker) > 80:
            return None, "marker over 80 characters"
        if days_s is None:
            days = DEFAULT_DAYS
        elif days_s.isdigit() and days_s.isascii() and 1 <= int(days_s) <= 7:
            days = int(days_s)
        else:
            return None, "days must be 1 to 7"
        if "#" in url:
            return None, "url fragment not allowed"
        sp = urlsplit(url)
        netloc = sp.netloc
        if "@" in netloc:
            return None, "userinfo in url"
        host = netloc[:-4] if netloc.endswith(":443") else netloc
        if ":" in host or not host or host != host.lower():
            return None, "host must be a lowercase name on port 443"
        hosts = load_hosts() if hosts is None else hosts
        if host not in hosts:
            return None, "host is not on the allowlist"
        path = sp.path or "/"
        if not path.startswith("/"):
            return None, "bad path"
        return {"url": "https://%s%s" % (host, path) + ("?" + sp.query if sp.query else ""), "host": host, "path": path,
                "marker": marker, "days": days, "line": line}, None
    except Exception as e:  # never raise
        return None, "parse error " + type(e).__name__


def emit_ok(line, hosts=None):
    """Stricter than parse_line: no port at all, and a marker that would pass on any page of the site
    (the host, the path, 'html', or text that is only part of the URL) is refused."""
    try:
        p, why = parse_line(line, hosts)
        if why:
            return None, why
        url = LINE_RE.match(line).group(1)
        if ":" in urlsplit(url).netloc:
            return None, "port in url"
        marker = p["marker"]
        low = marker.lower()
        if marker != marker.strip() or low in _ALWAYS_PASS or low == p["host"] or low in (p["path"].lower(), p["path"].strip("/").lower()):
            return None, "marker would pass on any page"
        if low in url.lower():
            return None, "marker is part of the url"
        return p, None
    except Exception as e:
        return None, "emit check error " + type(e).__name__


def no_sources():
    """Sources that never get a proposed line (text from strangers). inbound-mail is always on the list;
    VERIFY_NO_SOURCES (comma list) can only ADD to it, never remove it."""
    return {"inbound-mail"} | {x.strip() for x in (os.environ.get("VERIFY_NO_SOURCES") or "").split(",") if x.strip()}


def suggest(title, done_means="", source="", hosts=None):
    """Pure: no network, no model. A line only when the text names exactly ONE https URL and exactly ONE
    distinct double-quoted string (the marker), and the line passes emit_ok. Anything doubtful is None.
    Never for inbound-mail (text from strangers), plus any source in $VERIFY_NO_SOURCES (comma list, adds only)."""
    try:
        if source in no_sources():
            return None
        text = str(title or "")[:4000]
        if "http" not in text.lower() or "VERIFY:" in str(done_means or "") or killed():
            return None
        urls = {u.rstrip(".,;:!?)]}") for u in _URL_RE.findall(text)}
        if len(urls) != 1:
            return None
        url = next(iter(urls))
        if not url.startswith("https://"):
            return None
        picks = {m.group(1) for m in _QUOTED_RE.finditer(text)}
        if len(picks) != 1:
            return None
        line = 'VERIFY: url=%s has="%s" days=%d' % (url, next(iter(picks)), DEFAULT_DAYS)
        p, _ = emit_ok(line, hosts)
        return line if p else None
    except Exception:
        return None


def _day(t=None):
    tz = None
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(os.environ.get("VERIFY_TZ") or "UTC")
    except Exception:
        tz = datetime.timezone.utc
    t = t or datetime.datetime.now(datetime.timezone.utc)
    return t.astimezone(tz).date().isoformat()


def emits_today(events, today=None):
    """Count note events whose text starts with 'VERIFY: ' written today. Any error -> cap reached (fail closed)."""
    try:
        today, n = today or _day(), 0
        for e in events:
            if e.get("event") == "note" and str(e.get("note") or "").startswith("VERIFY: "):
                t = datetime.datetime.fromisoformat(str(e.get("ts")).replace("Z", "+00:00"))
                if t.tzinfo is None:
                    t = t.replace(tzinfo=datetime.timezone.utc)
                if _day(t) == today:
                    n += 1
        return n
    except Exception:
        return EMIT_CAP_PER_DAY


def may_emit(events, today=None):
    return emits_today(events, today) < EMIT_CAP_PER_DAY


def main(argv):
    if len(argv) == 3 and argv[1] == "check":
        p, why = emit_ok(argv[2])
        print("ok" if p else "refused: " + why)
        return 0 if p else 1
    if len(argv) == 3 and argv[1] == "suggest":
        s = suggest(argv[2])
        print(s or "")
        return 0 if s else 1
    print("usage: verify_watch.py check LINE | suggest TITLE", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
