# failure-register

Jobs fail. One failure is noise; the same failure three times is a problem nobody has looked at.
`failure-register` reads a plain log of failures and writes one page that lists only the repeats.
It is **read-only**: it never opens a ticket, sends a message or edits anything except its own two files.
A person (or a later piece of your setup) decides what to do with the page.

## Use

Have each job record its failures (and, optionally, its passes):

    python3 record_failure.py --job nightly --step build --reason "build timed out after 300s" --exit 124 || true
    python3 record_failure.py --job nightly --step build --ok || true

Then run the register from cron or a timer (every 30 minutes is plenty):

    python3 failure_register.py            # writes state/failure-register.md and state/failure-register.tick
    python3 failure_register.py --explain build     # the raw lines behind a signature or id
    python3 failure_register.py --since 2026-10-01 --until 2026-10-07   # replay a window; no heartbeat written

Input is `$HUBKIT_HOME/state/failures.jsonl`, one JSON object per line:
`{"ts": "2026-10-03T01:00:09Z", "job": "nightly", "step": "build", "reason": "timed out", "exit": 124}`.
`ts` may be ISO text or epoch seconds. `reason`, `exit` and `text` are optional. `{"ok": true}` marks a pass.
Rotated files (`failures.jsonl.1`, `failures.jsonl.2.gz`) are read too, and a line copied into two files counts once.

## The rule

- **Signature** = `job | step | reason class`. Times, process ids, hashes, ids, durations, numbers and absolute paths
  are removed first, so the same failure on different days groups together.
- **Reason class** (one per record; if several reasons appear the first match wins): exit code (`exit-N`, or `timeout` for 124),
  then `http-404`, `refused`, `timeout`, `stale`, `skipped`, else `unknown`. A failure with no reason is kept as `unknown`, not dropped.
- **Incident** = failures of one signature within 10 minutes (retries) count once.
- **Repeat** = 3 or more incidents in the window (default 7 days). One or two stay silent: they are listed under "Below threshold" only.
- **CAUSE column**: type the cause into the register file by hand; it survives re-runs. Write `by-design: why` to mark an
  accepted failure. Add `@YYYY-MM-DD` (the day you fixed it) and any failure after that day flips the row to **reopened**.
- **Status**: `open`, `caused`, `by-design`, `reopened`, or `passed-since (still no CAUSE)`.
- **Heartbeat**: every normal run rewrites `failure-register.tick` with the time, even when there is nothing to list.
  Check it with `deadman` so a register that stopped running is noticed.

## What it will not do

- Open a ticket, card or message. The test suite checks that nothing outside the register, the tick and `events.jsonl` changes.
- Copy credentials or absolute paths into the page. API keys, bearer strings, key=value pairs, long mixed tokens and
  paths are replaced with `<secret>` / `<path>` in the register, in `--explain` output, in the CAUSE cell, and by `record_failure.py` before it writes.
- Trust the log: corrupt lines, binary garbage, lines with no usable time and non-object JSON are skipped and counted in a `WARNING` line.

## Optional: failing scheduled jobs

    launchctl list > /tmp/jobs.txt
    python3 failure_register.py --launchctl-file /tmp/jobs.txt --prefix com.yourname.

Any job whose label starts with a `--prefix` and whose last exit is non-zero is listed under "Standing non-zero exits",
whatever its count. With no `--prefix` nothing is flagged.

## State and events

- `$HUBKIT_HOME/state/failures.jsonl` in; `failure-register.md`, `failure-register.tick` and `events.jsonl` out (one `failure.repeat` line per open repeat).
- Python 3.9, standard library only.

## Test

    bash patterns/failure-register/test.sh            # unit tests in a scratch HOME
    bash patterns/failure-register/test.sh --mutate   # breaks each rule in a temp copy; the tests must notice

`fixtures/replay-failures.txt` is a small invented log (a build that times out three times, a deploy that fails twice,
a sync with no reason) the tests replay to prove 3 flags and 2 does not.
