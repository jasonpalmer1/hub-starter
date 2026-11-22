#!/usr/bin/env python3
"""PreToolUse hook (matcher Agent|Task): refuse to start a second worker on a ticket
a live worker already holds. Uses dup-worker-check.py, which sits next to this file.

To override on purpose (a verifier or a second opinion) start a line of the prompt with
"DUP-OK:" and a reason, e.g. "DUP-OK: independent verification of the first worker".
Merely mentioning the word elsewhere does nothing. Fails OPEN: a missing checker, a crash, bad input or a check that
takes more than 3 seconds never blocks the call.

DUP_CHECK_BIN overrides the checker path (used by the tests).
"""
import json
import os
import re
import subprocess
import sys

# A deliberate override: a line that STARTS (column 0) with DUP-OK: and gives a reason, outside any
# code fence. A reason that is only the <placeholder> from the refusal text does not count.
OVERRIDE_RX = re.compile(r"^DUP-OK:[ \t]*(?!<)\S", re.M)
FENCE_OPEN_RX = re.compile(r"^[ \t]*(`{3,}|~{3,})")
FENCE_CLOSE_RX = re.compile(r"^[ \t]*(`{3,}|~{3,})[ \t\r]*$")


def outside_fences(text):
    """The text with every fenced code block removed, the way Markdown reads them: a fence opens
    with 3 or more backticks or tildes and closes only on a line of the SAME character, at least as
    long, with nothing else on it. A shorter or different fence inside a block is just content, and a
    block never closed runs to the end."""
    out, fence = [], None
    for line in text.split("\n"):
        if fence is None:
            m = FENCE_OPEN_RX.match(line)
            if m:
                fence = m.group(1)
            else:
                out.append(line)
        else:
            m = FENCE_CLOSE_RX.match(line)
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence):
                fence = None
    return "\n".join(out)


def main():
    try:
        data = json.load(sys.stdin)
        if data.get("tool_name") not in ("Agent", "Task"):
            return 0
        tin = data.get("tool_input") or {}
        prompt = str(tin.get("prompt") or "")
        if OVERRIDE_RX.search(outside_fences(prompt)):
            return 0
        checker = os.environ.get("DUP_CHECK_BIN") or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "dup-worker-check.py")
        r = subprocess.run(
            [sys.executable, checker],
            input=json.dumps({"description": tin.get("description", ""), "prompt": prompt}),
            capture_output=True, text=True, timeout=3)
        if r.returncode == 3 and r.stdout.strip():
            print("Duplicate worker: " + r.stdout.strip() + "\n"
                  "Do not start a second worker on the same ticket; wait for the live one or "
                  "message it. If this is deliberate (a verifier, a second opinion), start a line of the prompt "
                  "with \"DUP-OK: <reason>\" and retry.", file=sys.stderr)
            return 2
    except Exception:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
