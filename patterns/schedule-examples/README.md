# schedule-examples

You have a job; you want it to run on a timer, and to be told if it stops.

## What this is

Six files to copy and edit. Nothing here runs by itself and nothing is
installed for you. They are the scheduling half of the `deadman` pattern:

| file | what it is |
|---|---|
| `launchd-job.plist.example` | macOS: run a job nightly, then record it succeeded |
| `launchd-deadman-check.plist.example` | macOS: the watcher, every 15 minutes |
| `crontab.example.txt` | the same two things as cron lines (Linux or macOS) |
| `crontab.loop.example.txt` | the four cron lines that run the work loop (Linux, macOS, or WSL2 on Windows) |
| `windows-keepalive.task.xml` | Windows: a logon task that keeps WSL2 Ubuntu running so cron fires (untested on a real PC) |
| `notify-windows.example.sh` | WSL2: show a Windows toast when an alarm fires (untested on a real PC) |

Use launchd or cron, not both. Any other scheduler works the same way: the job
ends with a "beat" that only runs on success, and a separate entry runs the
check.

## What you need

- the `deadman` pattern installed (`~/.claude/hubkit/deadman/deadman.py`)
- macOS with launchd, or anything with cron

## Two rules that make it work

1. **The beat goes after `&&`, never after `;`.** `job && beat` beats only
   when the job exits 0. `job ; beat` beats even when the job failed, which
   hides the very failure you wanted to see.
2. **The watcher is its own entry.** If the check lives inside the job, a
   job that never starts can never report itself.

## macOS (launchd)

1. Copy the two `.plist.example` files into `~/Library/LaunchAgents/`, dropping
   `.example`, and rename them to your own labels.
2. In each, replace `__HOME__` with your home folder (launchd does not
   expand `~`). In the job file also replace `YOUR_JOB_COMMAND`, `my-job` and
   `25h`.
3. Beat the job once by hand so it is registered:

       python3 ~/.claude/hubkit/deadman/deadman.py beat my-job --max-age 25h

4. For a message when an alarm fires, set `HUBKIT_NOTIFY_CMD` in the watcher file's
   `EnvironmentVariables` to your own script (full path). launchd does not pass your
   shell's environment, so without that key alarms land only in `state/alarms.log`.
   Delete the key if you do not want a message.
5. Load them:

       launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/YOUR-LABEL.plist

   Check with `launchctl list | grep YOUR-LABEL`. To stop one:
   `launchctl bootout gui/$(id -u)/YOUR-LABEL`.

## Cron

1. `crontab -e`, then paste the lines from `crontab.example.txt`.
2. Replace `YOUR_JOB_COMMAND`, `my-job` and `25h`. Use full paths for your own
   commands; cron's PATH is very short.
3. Beat the job once by hand, as in step 3 above.
4. For a message when an alarm fires, put `HUBKIT_NOTIFY_CMD=/full/path/to/script`
   on its own line above the jobs.

## Test it

    bash test.sh            # 19 tests in a scratch HOME (about 1 second)
    bash test.sh --mutate   # also breaks each example and checks a test fails

The tests parse the plists (and run `plutil -lint` on a Mac), and run the
cron command for real: a job that succeeds leaves a beat, one that fails
does not.

## Lift just this piece

Copy the file you want. They are only useful alongside `deadman.py`.
