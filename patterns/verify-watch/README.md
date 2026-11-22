# verify-watch

A closed "VERIFY line" that says how to prove a finished item is really live on a page.

## What it fixes

An item gets marked done, the page never changed, nobody notices. A VERIFY line names one
https page and one piece of text that must be on it. A checker of your own can then fetch the
page every day and reopen the item when the text is gone. This pattern is the part that decides
which lines are allowed. It never fetches anything itself.

    VERIFY: url=https://example.com/pricing has="Free plan included" days=3

## What it refuses

- anything but https, a port, a user name in the URL, an upper-case host, a `#fragment`
- a host that is not in your allowlist (`$HUBKIT_HOME/verify-hosts.txt`; a missing file refuses everything)
- a marker under 4 or over 80 characters, or any non-ASCII or control character
- (for proposed lines) a marker that would pass on any page: the host, the path, `html`, or text that is only part of the URL
- more than 10 proposed lines per day (fails closed if the day cannot be worked out)

## What you need

- python3, 3.9 or newer; nothing else

## Install

    ./install.sh --only verify-watch
    echo "example.com" >> ~/.claude/hubkit/verify-hosts.txt

## Use it

    python3 ~/.claude/hubkit/verify-watch/verify_watch.py check 'VERIFY: url=https://example.com/pricing has="Free plan included"'
    python3 ~/.claude/hubkit/verify-watch/verify_watch.py suggest 'Check https://example.com/pricing shows "Free plan included"'

`suggest` only answers when the text holds exactly one https URL and exactly one quoted string.
Kill switch: create `$HUBKIT_HOME/state/verify-watch-off` and no line is suggested.

## Test

    bash patterns/verify-watch/test.sh            # 15 tests
    bash patterns/verify-watch/test.sh --mutate   # breaks each guard, tests must notice

## Differences from the private hub

| Private hub does | Kit does | Why |
|---|---|---|
| Line cap 400, url cap 300 | Same (restored after the first gate) | |
| Kill switch: the off file or `VW_OFF=1` | Same (restored) | |
| Stranger mail never gets a proposed line | Same; `VERIFY_NO_SOURCES` can only add sources, never remove `inbound-mail` | |
| Fetches the page on a timer, reopens items, writes the ledger | Not ported: this module only decides whether a line is acceptable | Fetching and reopening need the private ledger. Do the fetch with your own job, and only for lines that pass `parse_line`. |
| Day cap in the operator's time zone | `VERIFY_TZ`, default UTC | |
