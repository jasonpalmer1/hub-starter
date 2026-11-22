#!/usr/bin/env python3
"""SubagentStop hook: a worker may not finish until its report file exists.

Claude Code runs this when a subagent (a "worker") tries to stop. Exit 0 lets it stop.
Exit 2 refuses and prints a reason that goes back to the worker, so it can fix it.

Where things live (all under $HUBKIT_HOME, default ~/.claude/hubkit):
  reports/            where workers write reports
  reports/workers/    per-worker fallback report: <agent_id>.md
  state/              this hook's log, strike counter, bad-brief log

How a stop is judged:
  1. Not a typed worker and no transcript found       -> allow.
  2. The brief has a line that is exactly "report: none"  -> allow.
  3. The per-worker fallback report was created this run and has content -> allow.
  4. Otherwise the brief must name an exact report path (a .md file under a
     "reports" folder, written as ~/..., $HOME/..., $HUBKIT_HOME/... or absolute).
     That ONE file must have been CREATED after the worker started and hold real
     text. Never "any recent file": a stale or unrelated file proves nothing.
  5. A brief that names no path is refused once; the second stop is let through
     (a worker cannot rewrite its own brief, so refusing forever only loops) and the
     brief is appended to state/report-gate-badbriefs.log so you can fix the dispatcher.

Failure rules: empty or unparseable input fails open (exit 0). Any other internal
error fails closed (exit 2), so a bug here can never quietly turn the gate off.

Payload fields Claude Code sends on SubagentStop: session_id, transcript_path, cwd,
hook_event_name, agent_id, agent_type, last_assistant_message. The brief and the true
start time are read from the worker's own transcript:
  ~/.claude/projects/*/<session_id>/subagents/agent-<agent_id>.jsonl
(first line = the opening message and its ISO timestamp).

Birth time: a report must be CREATED during the run, not just touched, so appending to
an old file does not count. st_birthtime exists on macOS; on Linux the check falls back
to the modified time.
"""
import datetime
import fcntl
import glob
import json
import os
import re
import sys
import time


def home_dir() -> str:
    return os.path.expanduser(os.environ.get("HUBKIT_HOME") or "~/.claude/hubkit")


def state_dir() -> str:
    return os.path.join(home_dir(), "state")


def auto_dir() -> str:
    return os.path.join(home_dir(), "reports", "workers")


def projects_root() -> str:
    return os.path.expanduser(os.environ.get("REPORT_GATE_PROJECTS_ROOT") or "~/.claude/projects")


STATE_TTL_S = 86400
CLOCK_SKEW_MS = 2000

# A line that is exactly "report: none" (trailing period allowed). Text inside triple
# quotes is ignored, so pasted third-party text cannot exempt a worker.
EXEMPT_RX = re.compile(r"^\s*report\s*:\s*none\.?\s*$", re.I | re.M)

# An absolute-looking .md path with a "reports" folder in it.
REPORT_PATH_RX = re.compile(
    r"(?<![\w.\-/~$])(?:~/|\$HOME/|\$\{HOME\}/|\$HUBKIT_HOME/|\$\{HUBKIT_HOME\}/|/)"
    r"(?:[\w.\-]+/)*reports/(?:[\w.\-]+/)*[\w.\-]+\.md"
)
# Words that usually sit just before the deliverable path (not before a cited doc).
CUE_RX = re.compile(
    r"(write|writes|writing|report\s+to|save|output|deliverable|land|file it|"
    r"produce|report\s+path|report\s*:)",
    re.I,
)
CUE_LOOKBACK = 240
# A first line that reads "Report: <path>" names the deliverable outright; it beats every other rule.
LINE1_RE = re.compile(r"\A\s*report\s*:\s*(\S+\.md)\s*$", re.I)
HANDBACK_MAX_REFUSALS = 3  # pre-handback refusals per worker before it is let through (a loop-break)


def _is_exempt(text: str) -> bool:
    stripped = re.sub(r'"""[\s\S]*?"""', "", text)
    stripped = re.sub(r"'''[\s\S]*?'''", "", stripped)
    return bool(EXEMPT_RX.search(stripped))


def log(line: str) -> None:
    try:
        ts = datetime.datetime.now().isoformat(timespec="seconds")
        os.makedirs(state_dir(), exist_ok=True)
        with open(os.path.join(state_dir(), "report-gate.log"), "a") as f:
            f.write(f"{ts} {line}\n")
    except Exception:
        pass


def log_pass(line: str) -> None:
    """Allowed stops are only logged when REPORT_GATE_LOG_ALL=1; blocks always log."""
    if os.environ.get("REPORT_GATE_LOG_ALL") == "1":
        log(line)


def flatten_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return ""


def find_subagent_transcript(session_id: str, agent_id: str):
    if not re.fullmatch(r"[\w\-]+", session_id or "") or not re.fullmatch(r"[\w\-]+", agent_id or ""):
        return None
    pattern = os.path.join(projects_root(), "*", session_id, "subagents", f"agent-{agent_id}.jsonl")
    matches = glob.glob(pattern)
    return matches[0] if matches else None


def read_prompt_and_start(transcript_path: str):
    """First line of the worker's transcript: (brief text, start time in ms). Either may be None."""
    try:
        with open(transcript_path, "r") as f:
            first_line = f.readline()
        rec = json.loads(first_line)
        prompt_text = flatten_text((rec.get("message") or {}).get("content"))
        ts = rec.get("timestamp")
        start_ms = None
        if ts:
            dt = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
            start_ms = dt.timestamp() * 1000
        return prompt_text, start_ms
    except Exception:
        return None, None


def expand_path(p: str) -> str:
    hk = home_dir()
    p = p.replace("${HUBKIT_HOME}", hk).replace("$HUBKIT_HOME", hk)
    p = p.replace("${HOME}", os.path.expanduser("~")).replace("$HOME", os.path.expanduser("~"))
    return os.path.expanduser(p)


def select_report_path(text: str):
    """Pick the brief's DELIVERABLE path, not a background doc it only cites.
    Returns (path or None, rule). Prefer the LAST path with a writing cue before it,
    else the LAST path: briefs cite background first and name the deliverable last."""
    first = (text or "").lstrip().split("\n", 1)[0]
    m1 = LINE1_RE.match(first)
    if m1 and REPORT_PATH_RX.fullmatch(m1.group(1)):
        return m1.group(1), "line1"
    matches = list(REPORT_PATH_RX.finditer(text))
    if not matches:
        return None, "none"
    cued = [
        m for m in matches
        if CUE_RX.search(text[max(0, m.start() - CUE_LOOKBACK):m.start()])
    ]
    if cued:
        return cued[-1].group(0), "cued"
    return matches[-1].group(0), "last"


def auto_report_path(agent_id: str) -> str:
    safe = re.sub(r"[^\w\-]", "_", agent_id or "unknown-agent")
    return os.path.join(auto_dir(), f"{safe}.md")


def created_this_run(path: str, start_ms) -> bool:
    """True only if the file exists and was born and last written at or after the start."""
    try:
        st = os.stat(path)
    except OSError:
        return False
    if start_ms is None:
        return True
    floor = start_ms - CLOCK_SKEW_MS
    birth_ms = getattr(st, "st_birthtime", st.st_mtime) * 1000
    return birth_ms >= floor and st.st_mtime * 1000 >= floor


def _has_real_content(path: str) -> bool:
    """A zero-byte or whitespace-only file is not a report."""
    try:
        with open(path, "r", errors="ignore") as f:
            return bool(f.read().strip())
    except Exception:
        return False


def bump_noreport_strike(agent_id: str) -> int:
    """How many times this worker has been refused for a brief with no report path.
    Returns 1 on any state failure, which blocks: a broken counter must not open the gate."""
    if not agent_id:
        return 1
    now = time.time()
    try:
        os.makedirs(state_dir(), exist_ok=True)
        with open(os.path.join(state_dir(), "report-gate-state.json"), "a+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                f.seek(0)
                raw = f.read()
                try:
                    st = json.loads(raw) if raw.strip() else {}
                except Exception:
                    st = {}
                if not isinstance(st, dict):
                    st = {}
                st = {k: v for k, v in st.items()
                      if isinstance(v, dict) and now - v.get("ts", 0) < STATE_TTL_S}
                entry = st.get(agent_id) or {"n": 0}
                entry["n"] = int(entry.get("n", 0)) + 1
                entry["ts"] = now
                st[agent_id] = entry
                f.seek(0)
                f.truncate()
                f.write(json.dumps(st))
                return entry["n"]
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
    except Exception as e:
        log(f"ERROR strike-counter failed for id={agent_id}, treating as first: {e}")
        return 1


def record_bad_brief(agent_type: str, agent_id: str, prompt_text) -> None:
    try:
        ts = datetime.datetime.now().isoformat(timespec="seconds")
        head = (prompt_text or "")[:400].replace("\n", " ")
        with open(os.path.join(state_dir(), "report-gate-badbriefs.log"), "a") as f:
            f.write(f"{ts} agent={agent_type} id={agent_id} brief_head={head!r}\n")
    except Exception:
        pass


def _gate_main(raw: str, pre: bool = False) -> int:
    """pre=True is the PreToolUse hook on SubagentHandback: the same judgment, but it refuses BEFORE the
    worker hands back, and lets it through after HANDBACK_MAX_REFUSALS refusals so it can never loop."""
    if not pre:
        return _judge(raw)
    try:
        d = json.loads(raw) if raw and raw.strip() else {}
    except Exception:
        return 0
    if not isinstance(d, dict) or d.get("tool_name") != "SubagentHandback" or not d.get("agent_id"):
        return 0
    rc = _judge(raw)
    if rc != 2:
        return 0
    n = bump_noreport_strike("pre:" + d["agent_id"])
    if n >= HANDBACK_MAX_REFUSALS:
        log(f"LOOP-BREAK id={d['agent_id']} reason=handback-refused-{n}-times action=allowed-through")
        return 0
    return 2


def _judge(raw: str) -> int:
    if not raw or not raw.strip():
        log("ERROR empty stdin, failing open")
        return 0
    try:
        data = json.loads(raw)
    except Exception as e:
        log(f"ERROR could not parse stdin, failing open: {e}")
        return 0

    try:
        session_id = data.get("session_id") or ""
        agent_id = data.get("agent_id") or ""
        agent_type = data.get("agent_type") or ""
        display_name = agent_type or agent_id or "unknown-agent"

        prompt_text, start_ms = None, None
        tpath = find_subagent_transcript(session_id, agent_id) if session_id and agent_id else None
        if tpath:
            prompt_text, start_ms = read_prompt_and_start(tpath)

        # Not a typed worker and nothing to judge: let it stop.
        if not agent_type and not tpath:
            log_pass(f"ALLOW-NO-TYPE id={agent_id}")
            return 0

        if prompt_text and _is_exempt(prompt_text):
            log_pass(f"EXEMPT agent={display_name} id={agent_id}")
            return 0

        # The per-worker fallback report counts if it was created this run. Offered only
        # with a real agent id and a known start time, else one worker's file could pass another.
        auto = auto_report_path(agent_id) if (agent_id and start_ms is not None) else None
        if auto and created_this_run(auto, start_ms) and _has_real_content(auto):
            log_pass(f"PASS agent={display_name} id={agent_id} report={auto} pick=auto")
            return 0
        if auto:
            try:
                os.makedirs(auto_dir(), exist_ok=True)
            except Exception:
                pass
        auto_hint = auto or "a NEW file under the reports folder"

        picked, pick_rule = select_report_path(prompt_text or "")
        if not picked:
            if not agent_id:
                record_bad_brief(display_name, agent_id, prompt_text)
                log(f"LOOP-BREAK agent={display_name} id= reason=no-agent-id action=allowed-through")
                return 0
            strike = bump_noreport_strike(agent_id)
            if strike >= 2:
                record_bad_brief(display_name, agent_id, prompt_text)
                log(f"LOOP-BREAK agent={display_name} id={agent_id} "
                    f"reason=no-report-path-in-brief strike={strike} action=allowed-through")
                return 0
            log(f"BLOCK agent={display_name} id={agent_id} reason=no-report-path-in-brief strike={strike}")
            print(
                "Brief must name its report path (a .md file under a reports folder) "
                "or say report: none. This is your ONE refusal: write a short "
                f"report (what you did, where your output is) to {auto_hint} and name "
                "that path in your final message, then stop. The next stop will "
                "be allowed through.",
                file=sys.stderr,
            )
            return 2

        report_path = expand_path(picked)

        if created_this_run(report_path, start_ms) and _has_real_content(report_path):
            log_pass(f"PASS agent={display_name} id={agent_id} report={report_path} pick={pick_rule}")
            return 0

        if created_this_run(report_path, start_ms):
            log(f"BLOCK agent={display_name} id={agent_id} report={report_path} reason=empty-report")
            print(
                f"Worker's report file {report_path} exists but is empty (or whitespace-only) "
                "- write real content, then stop.",
                file=sys.stderr,
            )
            return 2

        if os.path.exists(report_path):
            # Existed before this run: it cannot prove this worker's work and may belong to
            # someone else. Never tell the worker to edit it.
            log(f"BLOCK agent={display_name} id={agent_id} report={report_path} reason=preexisting")
            print(
                f"The report path picked from your brief ({report_path}) already "
                "existed before you started, so it cannot prove your work. Do NOT "
                "edit or append to it - it may belong to someone else. Write a short "
                f"report (what you did, where your output is) to {auto_hint}, name that "
                "path in your final message, then stop.",
                file=sys.stderr,
            )
            return 2

        log(f"BLOCK agent={display_name} id={agent_id} report={report_path} reason=missing")
        print(
            f"Worker finished without its report file {report_path} - write it, then "
            f"stop. If that path is not your deliverable, write your report to {auto_hint} "
            "instead and never edit a file you were only told to read.",
            file=sys.stderr,
        )
        return 2

    except Exception as e:
        log(f"ERROR internal failure, failing closed: {e}")
        print("report-gate: internal error, failing closed - see state/report-gate.log", file=sys.stderr)
        return 2


def release_ticket_locks(raw: str) -> None:
    """Free the ticket locks the finishing worker owned (patterns/ticket-lock). Fail-open: a missing
    library or any error does nothing, and can never change the gate's exit code."""
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        for d in (os.path.join(here, "..", "ticket-lock"), os.path.join(home_dir(), "ticket-lock")):
            if os.path.isfile(os.path.join(d, "ticket_lock.py")):
                sys.path.insert(0, d)
                break
        else:
            return
        import ticket_lock
        d = json.loads(raw)
        sid, aid = d.get("session_id") or "", d.get("agent_id") or ""
        tp = find_subagent_transcript(sid, aid) if sid and aid else None
        if tp:
            pt, _ = read_prompt_and_start(tp)
            ticket_lock.release_and_audit(pt, tp, aid)
    except Exception:
        pass


def main() -> int:
    """Run the gate, then (only when the worker is really finishing, rc 0) release its ticket locks."""
    raw = sys.stdin.read()
    pre = False
    try:
        pre = json.loads(raw).get("hook_event_name") == "PreToolUse"
    except Exception:
        pass
    rc = _gate_main(raw, pre)
    if rc == 0 and not pre:
        release_ticket_locks(raw)
    return rc


if __name__ == "__main__":
    sys.exit(main())
