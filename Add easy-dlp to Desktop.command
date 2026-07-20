#!/bin/bash
# Creates a Desktop shortcut (alias) to the easy-dlp app — double-click once.

set -euo pipefail

cd "$(dirname "$0")"
PROJECT_ROOT="$(pwd)"
APP="$PROJECT_ROOT/easy-dlp.app"
DESKTOP="${HOME}/Desktop"
ALIAS="$DESKTOP/easy-dlp"

chmod +x run.sh "Open easy-dlp.command" "Add easy-dlp to Desktop.command" \
  easy-dlp.app/Contents/MacOS/easy-dlp 2>/dev/null || true
xattr -dr com.apple.quarantine easy-dlp.app 2>/dev/null || true

if [[ ! -d "$APP" ]]; then
  osascript -e 'display dialog "Could not find easy-dlp.app in this folder." buttons {"OK"} default button "OK" with title "easy-dlp" with icon stop' >/dev/null
  exit 1
fi

# Remove an old Desktop alias/app with the same name so we can recreate it.
rm -rf "$ALIAS" "$ALIAS.app" 2>/dev/null || true

osascript <<EOF
tell application "Finder"
  make alias file to POSIX file "$APP" at desktop
  set name of result to "easy-dlp"
end tell
EOF

osascript -e 'display dialog "Done! Look for \"easy-dlp\" on your Desktop.\n\nDouble-click it any time to open the app.\n\n(You can also drag it to your Dock.)" buttons {"OK"} default button "OK" with title "easy-dlp"' >/dev/null

echo "Desktop shortcut created: $ALIAS"
read -r -p "Press Enter to close…" _
