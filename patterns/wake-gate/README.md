# wake-gate

A script that finds a problem should not message you every time. `wake-gate` is the one gate they all
go through. It answers **WAKE** (tell the operator now) or **LOG** (write it down, stay quiet).

## The rule

A finding is `{kind, severity, summary, source}`. The key is `kind|source` (plus `subject` if given).

| finding | verdict |
|---|---|
| severity `crit`, key not seen in the last 24 h | WAKE |
| a dead lane that still holds operator asks (`--dead-lane-asks`, or kind `dead-lane-holding-operator-asks`), key not seen in 24 h | WAKE |
| the same key again inside 24 h | LOG |
| severity `warn`, `info`, empty or unknown | LOG, always |
| the key woke more than 24 h ago | WAKE again |

## Use

    python3 wake_gate.py --kind disk-full --source nightly --severity crit --summary "disk 99 percent"
    # prints "WAKE disk-full|nightly" and exits 0, or "LOG <key> (reason)" and exits 1

From Python: `import wake_gate; wake_gate.gate({"kind": ..., "severity": ..., "summary": ..., "source": ...})`.
The `work-loop` pattern sends its starvation finding through this gate when the two are installed side by side.

## State and events

- `$HUBKIT_HOME/state/wake-seen.json`: `{key: epoch of last WAKE}`, written atomically, entries older than 7 days dropped.
- `$HUBKIT_HOME/state/events.jsonl`: one `wake.decision` line per call (verdict, key, reason, severity, source).

## Failure behaviour

- Memory file cannot be written: the verdict is **LOG** (a wake that cannot be remembered would repeat on every call) and the
  finding is printed on stderr as `UNRECORDED ...`, so a crit is never silently lost.
- `wake-seen.json` corrupt: bad entries are ignored (a crit may wake once more), a note goes to stderr, the next WAKE rewrites it.
- Newlines and control characters in a key are flattened to single spaces, so one finding cannot forge two lines.
- Clock moved backwards: a stored time in the future still blocks while it is within 24 h of now; a stamp further out is treated as bogus.

## Test

    bash patterns/wake-gate/test.sh            # unit tests in a scratch HOME
    bash patterns/wake-gate/test.sh --mutate   # breaks each rule in a temp copy; the tests must notice
