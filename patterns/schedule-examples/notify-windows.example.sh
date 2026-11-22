#!/usr/bin/env bash
# EXAMPLE: show a Windows toast for a deadman alarm, from WSL2. UNTESTED on a real Windows 11 PC.
# Use: HUBKIT_NOTIFY_CMD=/full/path/to/notify-windows.example.sh   (deadman adds TITLE and REASON as arguments)
#
# The alarm text is passed to PowerShell as DATA (base64 in environment variables, shared with Windows
# through WSLENV), never pasted into the PowerShell code. So quotes, backticks, $(...) and newlines in a
# title or reason cannot run anything. The script never fails the caller: no powershell.exe means one
# line on stderr and exit 0; it also gives up after 15 seconds (deadman waits 20).
title="${1:-hubkit alarm}"
reason="${2:-}"

if ! command -v powershell.exe >/dev/null 2>&1; then
  echo "notify-windows: powershell.exe not found (not running under WSL2?), no toast shown" >&2
  exit 0
fi

b64() { printf '%s' "$1" | base64 | tr -d '\n'; }

ps_code='
$t = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($env:HK_TITLE_B64))
$r = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($env:HK_REASON_B64))
[void][Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
$x = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("ToastText02")
$n = $x.GetElementsByTagName("text")
[void]$n.Item(0).AppendChild($x.CreateTextNode($t))
[void]$n.Item(1).AppendChild($x.CreateTextNode($r))
$toast = New-Object Windows.UI.Notifications.ToastNotification $x
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("hubkit").Show($toast)
'
encoded="$(printf '%s' "$ps_code" | iconv -f UTF-8 -t UTF-16LE | base64 | tr -d '\n')"

run=(powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand "$encoded")
if command -v timeout >/dev/null 2>&1; then run=(timeout 15 "${run[@]}"); fi

HK_TITLE_B64="$(b64 "$title")" HK_REASON_B64="$(b64 "$reason")" \
  WSLENV="${WSLENV:+$WSLENV:}HK_TITLE_B64:HK_REASON_B64" \
  "${run[@]}" >/dev/null 2>&1 \
  || echo "notify-windows: toast command failed (alarm is still in the log)" >&2
exit 0
