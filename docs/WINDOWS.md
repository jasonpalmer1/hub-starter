# Run the kit on Windows

Windows 11 only. Last verified on a real Windows 11 PC: NOT YET.

The kit is made for Linux and macOS. On Windows you run it inside WSL2 (Windows Subsystem for Linux: a small
Ubuntu that lives inside Windows). Everything below happens inside Ubuntu. Do not run the kit from PowerShell,
Command Prompt or Git Bash; the installer stops there on purpose.

Each step says what you should SEE when it worked. If you do not see it, stop and read "Stuck?" near the end.

## Before the call

Answer these 5 questions by message before anyone helps you. They decide whether this route can work at all.

1. Is it Windows 11 (not 10)? Press the Windows key, type `winver`, press Enter. The box says the version.
2. Is it your own PC, and are you allowed to turn on virtualization? Open Task Manager, Performance, CPU. The
   line "Virtualization" should say Enabled.
3. Can you open Terminal as administrator? Right-click Start, choose "Terminal (Admin)", and say yes to the prompt.
4. Does the PC stay awake, or does it sleep? (The scheduled jobs only run while it is awake.)
5. Will you use Claude Code in a terminal, or only the Claude desktop app? The kit's helpers and hooks live in
   the terminal version.

## Step 1. Install WSL2 and Ubuntu

Open Terminal as administrator (right-click Start, "Terminal (Admin)") and run:

    wsl --install

You should SEE progress text, then a request to restart. Restart the PC.

## Step 2. First start of Ubuntu

After the restart, Ubuntu opens by itself (or open Start, type `Ubuntu`, press Enter). It asks you to make a
username and a password. Pick any simple username with lowercase letters. The password does not show while you
type; that is normal. You should SEE a prompt that ends with a dollar sign inside a black window.

## Step 3. Check it is WSL2 and the default

In Terminal (not the Ubuntu window), run:

    wsl -l -v

You should SEE a row for Ubuntu with a star next to its name and VERSION 2. If the star is on another name
(for example docker-desktop), run:

    wsl --set-default Ubuntu

If VERSION says 1, run:

    wsl --set-version Ubuntu 2

## Step 4. Check Python

Open Ubuntu (type `wsl` in Terminal and press Enter). From here on, every command runs in Ubuntu. Run:

    python3 --version

You should SEE Python 3.9 or newer (for example `Python 3.12.3`). If it is older or missing:

    sudo apt update
    sudo apt install -y python3

## Step 5. Install Claude Code and sign in

In Ubuntu:

    curl -fsSL https://claude.ai/install.sh | bash

Close the Ubuntu window, open it again with `wsl`, then run:

    claude

You should SEE a sign-in message with a link. If no browser opens by itself, copy the link into your normal
Windows browser, sign in, and paste the code back into Ubuntu. Install help: https://code.claude.com/docs

## Step 6. Get the kit into Ubuntu's own folder

The kit must sit in Ubuntu's own home folder (`~`), never under `/mnt/c` (that is your Windows drive; it is slow
and breaks file permissions).

Path A, a tarball file someone sent you (use this first). Save it in your Windows Downloads folder, then in Ubuntu:

    cd /mnt/c/Users
    ls

You should SEE one folder with your Windows name. Copy the file to Ubuntu's home folder, replacing NAME with that
folder (the file name may differ):

    cp NAME/Downloads/hub-starter.tar.gz ~
    cd ~
    tar -xzf hub-starter.tar.gz
    ls

You should SEE a folder named `hub-starter`.

Path B, you already have a GitHub account with access to the repo:

    sudo apt install -y gh
    gh auth login
    gh repo clone OWNER/hub-starter ~/hub-starter

OWNER is the account that shared the repo with you.

## Step 7. Install the kit

    cd ~/hub-starter
    bash install.sh --dry-run

You should SEE a list of what would be copied and nothing written. If it looks fine:

    bash install.sh --all --wire-settings

You should SEE it copy files and say a backup of `~/.claude` was saved. Then check:

    python3 ~/.claude/hubkit/doctor/doctor.py

You should SEE a list of PASS lines. A fresh install may show one FAIL about hooks. If you used
`--wire-settings` the installer already added them; if it still shows, open the file it names:

    nano ~/.claude/settings.json

Paste the snippet the installer printed into the right place, press Ctrl+O, Enter, then Ctrl+X to save and quit.

## Which window

- Always open Ubuntu with `wsl`, then type `claude`. The Windows Claude app and PowerShell do not see this hub.
- Put your projects in `~/work` (Ubuntu's folder), not in a Windows folder.
- Edit kit files only inside Ubuntu (`nano`), never with a Windows editor on a `/mnt/c` copy.
- Never put the kit under `/mnt/c`.
- Windows paths look like `C:\hubkit-keepalive.xml`. Ubuntu paths look like `/mnt/c/...` or `~/work`. Each step
  in this guide says which window it belongs to.

## Power

Keep the PC plugged in, and set screen and sleep to "Never" (Settings, System, Power). A sleeping PC skips every
scheduled job; this is the same honest limit as in `docs/ALWAYS-RUNNING.md`.

## Keep it running

Untested on a real Windows 11 PC. Ubuntu inside WSL2 stops when nothing is using it, and the kit's scheduled jobs
(the loop) run from Ubuntu's own cron. So a small Windows task keeps Ubuntu awake, and cron runs the jobs. Do these
in order.

1. Ubuntu side first: copy the task file to the root of the Windows C drive.

       cp ~/hub-starter/patterns/schedule-examples/windows-keepalive.task.xml /mnt/c/hubkit-keepalive.xml

   Then, in Terminal (not Ubuntu), import it:

       schtasks /create /xml C:\hubkit-keepalive.xml /tn hubkit-keepalive

   You should SEE "SUCCESS: The scheduled task hubkit-keepalive has successfully been created." If it says the XML
   is malformed, open the file in Notepad, change UTF-8 to UTF-16 on the first line, choose Save As, set Encoding to
   UTF-16 LE, and run the command again. If it says access is denied, open Terminal as administrator once.

2. Ubuntu side again: check cron is there.

       systemctl status cron

   You should SEE "active (running)". If it says the unit is missing:

       sudo apt install -y cron

3. Put the four loop lines into cron.

       crontab -e

   Paste the four lines from `patterns/schedule-examples/crontab.loop.example.txt` (the same four as in
   `docs/ALWAYS-RUNNING.md`), save, and exit. Check:

       crontab -l

4. Forced-alarm test (does an alarm reach you?). Alarms are always written to
   `~/.claude/hubkit/state/alarms.log`. For a Windows pop-up as well, add this line above the four cron lines:

       HUBKIT_NOTIFY_CMD=/full/path/to/hub-starter/patterns/schedule-examples/notify-windows.example.sh

   Then stop the tick job for a while (comment its line out with `#`), wait for the watcher to raise an alarm,
   and see whether a toast appears. If no toast appears, that is fine: alarms are only in the log, so run
   `python3 ~/.claude/hubkit/doctor/doctor.py` once a day.

5. Fallback if the task does not start Ubuntu: make a shortcut instead. In Windows, press Win+R, type
   `shell:startup`, and in that folder create a shortcut to `wsl.exe --exec sleep infinity` with Run set to
   Minimized. A small taskbar button stays open; that is expected.

Check it from Ubuntu after 12 minutes with no Ubuntu window open:

    python3 ~/.claude/hubkit/doctor/doctor.py

You should SEE a PASS line for loop-tick.

## Stuck?

| What you see | What to do |
|---|---|
| `wsl --install` says virtualization is off, or Ubuntu will not start | Restart, open the BIOS or UEFI setup, turn on Virtualization (called SVM on AMD, VT-x on Intel), then run `wsl --install` again |
| "Exec format error" | The distro is WSL1. Run `wsl --set-version Ubuntu 2` in Terminal |
| No browser opens on sign-in | Copy the link Claude prints into your Windows browser, sign in, paste the code back |
| Commands are very slow, or files look odd | You are working under `/mnt/c`. Move the folder to `~` and work from there |

For more on the kit itself see `docs/START-HERE.md` and `docs/TROUBLESHOOTING.md`.
