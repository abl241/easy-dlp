#!/bin/bash
# Double-click this file in Finder to open easy-dlp.
# A Terminal window will open so you can see setup progress on first launch.

set -euo pipefail

cd "$(dirname "$0")"
export PATH="/opt/homebrew/bin:/usr/local/bin:/opt/local/bin:$PATH"

# ZIP downloads often strip the executable bit — fix that automatically.
chmod +x run.sh "Open easy-dlp.command" "Add easy-dlp to Desktop.command" \
  easy-dlp.app/Contents/MacOS/easy-dlp 2>/dev/null || true

# Clear macOS quarantine so Gatekeeper is less likely to block the app.
xattr -dr com.apple.quarantine . 2>/dev/null || true

echo "Starting easy-dlp..."
echo

./run.sh
status=$?

echo
if [[ $status -ne 0 ]]; then
  echo "Something went wrong (exit $status). See messages above."
  echo "Common fix on Mac: brew install python@3.12 python-tk@3.12 ffmpeg"
else
  echo "App closed. You can close this Terminal window."
fi

echo
read -r -p "Press Enter to close…" _
exit "$status"
