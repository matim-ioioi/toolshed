#!/bin/bash

#region фильтр фоновых задач Stop-события
HOOK_STDIN_STRIPPED="$(cat | tr -d '[:space:]')"
if printf '%s' "$HOOK_STDIN_STRIPPED" | grep -qF '"hook_event_name":"Stop"'; then
  if printf '%s' "$HOOK_STDIN_STRIPPED" | grep -qF '"background_tasks":[{'; then
    exit 0
  fi
fi
#endregion фильтр фоновых задач Stop-события

#region определение операционной системы
OS="$(uname -s 2>/dev/null)"
#endregion определение операционной системы

#region уведомление на macOS
notify_macos() {
  if command -v osascript >/dev/null 2>&1; then
    osascript -e 'display notification "Claude Code ждёт действия" with title "Toolshed | Claude Code" sound name "Glass"' \
      >/dev/null 2>&1 || true
  fi
}
#endregion уведомление на macOS

#region уведомление на Linux
notify_linux() {
  if command -v notify-send >/dev/null 2>&1; then
    notify-send "Toolshed | Claude Code" "Claude Code ждёт действия" >/dev/null 2>&1 || true
  fi
}
#endregion уведомление на Linux

#region звук на Linux (первый найденный плеер)
sound_linux() {
  if command -v paplay >/dev/null 2>&1; then
    ( paplay /usr/share/sounds/freedesktop/stereo/message.oga >/dev/null 2>&1 & )
    return
  fi
  if command -v aplay >/dev/null 2>&1; then
    ( aplay -q /usr/share/sounds/alsa/Front_Center.wav >/dev/null 2>&1 & )
    return
  fi
  if command -v pw-play >/dev/null 2>&1; then
    ( pw-play /usr/share/sounds/freedesktop/stereo/message.oga >/dev/null 2>&1 & )
    return
  fi
}
#endregion звук на Linux (первый найденный плеер)

#region привлечение внимания на Linux (urgency hint)
attention_linux() {
  if command -v notify-send >/dev/null 2>&1; then
    notify-send -u critical "Toolshed | Claude Code" "Claude Code ждёт действия" >/dev/null 2>&1 || true
  fi
}
#endregion привлечение внимания на Linux (urgency hint)

#region уведомление на Windows (через powershell.exe)
notify_windows() {
  if command -v powershell.exe >/dev/null 2>&1; then
    ( powershell.exe -NoProfile -NonInteractive -Command \
        'Add-Type -AssemblyName System.Windows.Forms; $ni = New-Object System.Windows.Forms.NotifyIcon; $ni.Icon = [System.Drawing.SystemIcons]::Information; $ni.Visible = $true; $ni.ShowBalloonTip(5000, "Toolshed | Claude Code", "Claude Code ждёт действия", [System.Windows.Forms.ToolTipIcon]::Info); Start-Sleep -Milliseconds 200; $ni.Dispose()' \
        >/dev/null 2>&1 || \
      powershell.exe -NoProfile -NonInteractive -Command \
        '[console]::beep(800,300)' \
        >/dev/null 2>&1 || true ) &
  fi
}
#endregion уведомление на Windows (через powershell.exe)

#region звук на Windows
sound_windows() {
  if command -v powershell.exe >/dev/null 2>&1; then
    ( powershell.exe -NoProfile -NonInteractive -Command \
        '[System.Media.SystemSounds]::Asterisk.Play()' \
        >/dev/null 2>&1 || \
      powershell.exe -NoProfile -NonInteractive -Command \
        '[console]::beep(800,300)' \
        >/dev/null 2>&1 || true ) &
  fi
}
#endregion звук на Windows

#region диспетчер по платформе
case "$OS" in
  Darwin)
    notify_macos
    ;;
  Linux)
    notify_linux
    sound_linux
    attention_linux
    ;;
  MINGW*|MSYS*|CYGWIN*)
    notify_windows
    sound_windows
    ;;
  *)
    ;;
esac
#endregion диспетчер по платформе

exit 0
