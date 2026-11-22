#!/usr/bin/env python3
"""UserPromptSubmit hook: prints a short ledger status block on every prompt.

Output (plain text on stdout; Claude Code adds a UserPromptSubmit hook's
stdout to the prompt's context):

    <ledger-status>
    Now: 5:22pm, Wed 9/23
    Open: 3 · Needs you: 1
    IDLE
      · NEEDS-OPERATOR L-0002: pick a name (owner: hub)
    </ledger-status>

The banner and surface lines come from the same ledger_lib functions the
`ledger banner` and `ledger surface` commands use, so they cannot drift.
The "Now:" line is the machine's local clock; it is there so the AI states
real times instead of guessing them.

Fails open, always: if the ledger is missing, empty or broken, the clock line
still prints and the hook exits 0. A status hook must never block a session.

Optional add-on lines (for example an "idle floor" reminder: no workers running
for 15 minutes while tickets are open): put a banner_extras.py next to this
hook, or point HUBKIT_BANNER_EXTRAS at a .py file. Its
prompt_lines(session_id, cwd) returns a list of strings, which print after the
ledger lines. The add-on is independent: a missing file, any error (even
sys.exit) or a non-list result adds nothing, and the clock and ledger lines
still print. It must return quickly; it runs on every prompt.

Wire it in your settings.json (the installer prints the exact snippet):
  {"hooks": {"UserPromptSubmit": [{"hooks": [
     {"type": "command", "command": "python3 $HUBKIT_HOME/ledger/ledger-banner-hook.py"}]}]}}
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent


def now_line() -> str:
    """"Now: 5:22pm, Wed 9/23": local time, lowercase am/pm, no leading zeros
    (built by hand so it cannot regain a zero on a platform without %-d)."""
    # HUBKIT_NOW_ISO (ISO 8601, no zone) overrides "now" for deterministic tests.
    override = os.environ.get("HUBKIT_NOW_ISO")
    now = datetime.fromisoformat(override) if override else datetime.now()
    hour12 = now.hour % 12 or 12
    ampm = "am" if now.hour < 12 else "pm"
    return f"Now: {hour12}:{now.minute:02d}{ampm}, {now.strftime('%a')} {now.month}/{now.day}"


def clock_lines() -> list[str]:
    try:
        return [now_line()]
    except Exception:
        return []


def ledger_content_lines() -> list[str]:
    try:
        sys.path.insert(0, str(HERE))
        from ledger_lib import LEDGER_PATH, banner_lines, fold, read_events, surface_lines

        if not LEDGER_PATH.exists():
            return []
        records = fold(read_events())
    except Exception:
        return []
    if not records:
        return []
    lines = list(banner_lines(records))
    lines.extend(f"  · {line}" for line in surface_lines(records))
    return lines


def read_payload() -> dict:
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def extra_lines(payload: dict) -> list[str]:
    """Lines from the optional add-on (see the module docstring); [] on any failure."""
    try:
        path = Path(os.environ.get("HUBKIT_BANNER_EXTRAS") or HERE / "banner_extras.py")
        if not path.is_file():
            return []
        spec = importlib.util.spec_from_file_location("hubkit_banner_extras", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        out = mod.prompt_lines(payload.get("session_id"), payload.get("cwd"))
        if not isinstance(out, list):
            return []
        return [str(x) for x in out]
    except BaseException:  # incl. SystemExit: an add-on must never set this hook's exit code
        return []


def main() -> int:
    payload = read_payload()
    clock, content = clock_lines(), ledger_content_lines()
    extras = extra_lines(payload)
    if not clock and not content and not extras:
        return 0
    print("\n".join(["<ledger-status>", *clock, *content, *extras, "</ledger-status>"]))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)  # never block a session
