#!/usr/bin/env bash
# Runs the schedule-examples tests in a fresh scratch HOME, alone, with only the deadman file beside it.
#   bash test.sh            run the tests
#   bash test.sh --mutate   also break each example in a temp copy and check the tests notice
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT
export HOME="$SCRATCH/home"; mkdir -p "$HOME"
unset HUBKIT_HOME EXAMPLES_DIR
export PYTHONDONTWRITEBYTECODE=1

command -v python3 >/dev/null || { echo "FAIL: python3 not found"; exit 1; }
python3 -W ignore "$HERE/test_schedule_examples.py" || { echo "FAIL: schedule-examples tests"; exit 1; }
echo "schedule-examples: tests PASS"

[ "${1:-}" = "--mutate" ] || exit 0

# Each mutation edits ONE line of an example in a temp copy; the suite must then FAIL.
rc=0
mutate() {  # name, example file, old text, new text
  local name="$1" file="$2" old="$3" new="$4" dir="$SCRATCH/mut-$1"
  mkdir -p "$dir"
  cp "$HERE"/*.example "$HERE"/*.txt "$HERE"/*.xml "$HERE"/notify-windows.example.sh "$HERE"/README.md "$dir"/
  python3 - "$dir/$file" "$old" "$new" <<'PY' || { echo "mutation $name: could not apply (text changed?)"; rc=1; return; }
import sys
p, old, new = sys.argv[1:4]
s = open(p).read()
if old not in s:
    sys.exit(1)
open(p, "w").write(s.replace(old, new, 1))
PY
  if EXAMPLES_DIR="$dir" python3 -W ignore "$HERE/test_schedule_examples.py" >/dev/null 2>&1; then
    echo "mutation $name: SURVIVED (tests did not notice)"; rc=1
  else
    echo "mutation $name: killed"
  fi
}
mutate cron-semicolon         crontab.example.txt ' && python3 $HOME' '; python3 $HOME'
mutate cron-watcher-beats     crontab.example.txt 'check --all >>' 'beat x --max-age 1h >>'
mutate cron-bad-time-field    crontab.example.txt '0 3 * * * YOUR' '0 3 * * YOUR'
mutate cron-notify-unexplained crontab.example.txt 'HUBKIT_NOTIFY_CMD=/full/path/to/your-notify-script' 'nothing here'
mutate cron-job-never-beats   crontab.example.txt 'beat my-job' 'list my-job'
mutate plist-watcher-interval launchd-deadman-check.plist.example '<integer>900</integer>' '<integer>9</integer>'
mutate plist-same-label       launchd-deadman-check.plist.example '<string>com.example.hubkit.deadman-check</string>' '<string>com.example.hubkit.my-job</string>'
mutate plist-job-semicolon    launchd-job.plist.example ' &amp;&amp; /usr/bin/python3' ' ; /usr/bin/python3'
mutate plist-no-schedule      launchd-job.plist.example '<key>StartCalendarInterval</key>' '<key>RunAtLoad</key>'
mutate plist-hardcoded-home   launchd-job.plist.example '<string>__HOME__/.claude/hubkit/state/my-job.log</string>' '<string>/var/x/.claude/hubkit/state/my-job.log</string>'
mutate plist-notify-key-gone launchd-deadman-check.plist.example '<key>HUBKIT_NOTIFY_CMD</key>' '<key>SOMETHING_ELSE</key>'
mutate plist-notify-unexplained README.md "set \`HUBKIT_NOTIFY_CMD\` in the watcher file's
   \`EnvironmentVariables\`" "set it somewhere"
mutate plist-real-label   launchd-job.plist.example '<string>com.example.hubkit.my-job</string>' '<string>com.acme.my-job</string>'
mutate plist-broken-xml       launchd-job.plist.example '</dict>
</plist>' '</plist>'
mutate plist-watcher-beats    launchd-deadman-check.plist.example '<string>check</string>' '<string>beat</string>'
[ $rc -eq 0 ] && echo "schedule-examples: mutation check PASS" || echo "schedule-examples: mutation check FAIL"
exit $rc
