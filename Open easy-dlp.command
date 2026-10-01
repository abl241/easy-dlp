#!/bin/bash
# Finder launcher: keep setup/errors visible; close only this terminal on success.
set -euo pipefail
cd "$(dirname "$0")"
export PATH="/opt/homebrew/bin:/usr/local/bin:/opt/local/bin:$PATH"
chmod +x run.sh "Open easy-dlp.command" "Add easy-dlp to Desktop.command" \
  easy-dlp.app/Contents/MacOS/easy-dlp 2>/dev/null || true
xattr -dr com.apple.quarantine . 2>/dev/null || true
launch_tty="$(tty 2>/dev/null || true)"
echo "Starting easy-dlp..."
if ./run.sh; then
  if [[ "${TERM_PROGRAM:-}" == Apple_Terminal && "$launch_tty" == /dev/ttys* ]]; then
    nohup osascript scripts/close-launch-terminal.applescript "$launch_tty" >/dev/null 2>&1 &
  fi
  exit 0
else
  status=$?
  echo "Something went wrong (exit $status). See messages above."
  echo "Common fix on Mac: brew install python@3.12 python-tk@3.12 ffmpeg"
  read -r -p "Press Enter to close…" _ || true
  exit "$status"
fi
