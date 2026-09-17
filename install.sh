#!/bin/bash
# Install the Lengau status poller as a macOS LaunchAgent (every 10 min).
# Only for an always-on Mac that is NOT a clone pushed to GitHub - the
# GitHub Actions workflow in .github/workflows/poll.yml is the normal poller.
# Run this once:   bash ~/src/chpc-status/install.sh
# Remove it with:  bash ~/src/chpc-status/install.sh --uninstall

set -euo pipefail

LABEL="com.nchang.lengau-status"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
# Seconds between polls. 600 = every 10 min. The page's own history bar cannot
# be trusted as a time axis (see README), so the polling interval IS the
# resolution of the record - don't set this much higher than you care about.
INTERVAL="${INTERVAL:-600}"

if [[ "${1:-}" == "--uninstall" ]]; then
  launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  echo "removed $LABEL (your data in $HERE/data is untouched)"
  exit 0
fi

PYTHON="$(command -v python3 || true)"
if [[ -z "$PYTHON" ]]; then
  echo "python3 not found on PATH" >&2
  exit 1
fi
# Prefer the system python3: it is always present and never rebuilt by conda,
# so the agent keeps working when environments change.
[[ -x /usr/bin/python3 ]] && PYTHON=/usr/bin/python3

mkdir -p "$HERE/data" "$HOME/Library/LaunchAgents"

cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>              <string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON</string>
    <string>$HERE/lengau_status.py</string>
  </array>
  <key>WorkingDirectory</key>   <string>$HERE</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>LENGAU_INTERVAL</key>  <string>$INTERVAL</string>
  </dict>
  <key>StartInterval</key>      <integer>$INTERVAL</integer>
  <key>RunAtLoad</key>          <true/>
  <key>StandardOutPath</key>    <string>$HERE/data/agent.out.log</string>
  <key>StandardErrorPath</key>  <string>$HERE/data/agent.err.log</string>
  <key>ProcessType</key>        <string>Background</string>
</dict>
</plist>
PLISTEOF

launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID" "$PLIST"
launchctl kickstart -p "gui/$UID/$LABEL" >/dev/null 2>&1 || true

echo "installed $LABEL"
echo "  python : $PYTHON"
echo "  runs   : every $((INTERVAL / 60)) min, and once at login"
echo "  data   : $HERE/data/lengau_observations.csv"
echo "  logs   : $HERE/data/agent.{out,err}.log"
echo
echo "check it:  launchctl print gui/$UID/$LABEL | head -20"
echo "report  :  python3 $HERE/lengau_status.py --report"
