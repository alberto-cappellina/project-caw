#!/bin/sh
# Install (or remove) the weekly schedule as a user LaunchAgent on macOS.
#
#   automation/install.sh render      # print the plist with the paths of THIS machine
#   automation/install.sh install     # write it to ~/Library/LaunchAgents and load it
#   automation/install.sh run-now     # start a test run now
#   automation/install.sh uninstall   # unload it and remove it
#
# The versioned file is only a template (local.caw.weekly.plist.template). Absolute paths
# are not stored in the repository: they are computed here from the project location.
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
PROJECT="$(cd "$HERE/.." && pwd)"
TEMPLATE="$HERE/local.caw.weekly.plist.template"
PLIST="$HOME/Library/LaunchAgents/local.caw.weekly.plist"

render() {
  sed "s|__PROJECT_DIR__|$PROJECT|g" "$TEMPLATE"
}

case "${1:-install}" in
  render)
    render
    ;;
  install)
    mkdir -p "$HOME/Library/LaunchAgents" "$PROJECT/logs"
    render > "$PLIST"
    plutil -lint "$PLIST" >/dev/null
    launchctl bootout "gui/$(id -u)/local.caw.weekly" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$PLIST"
    echo "Installed: $PLIST"
    launchctl print "gui/$(id -u)/local.caw.weekly" | grep -E "state|program" || true
    ;;
  uninstall)
    launchctl bootout "gui/$(id -u)/local.caw.weekly" 2>/dev/null || true
    rm -f "$PLIST"
    echo "Removed."
    ;;
  run-now)
    launchctl kickstart -k "gui/$(id -u)/local.caw.weekly"
    echo "Started. Log: $PROJECT/logs/run.log"
    ;;
  *) echo "usage: $0 [render|install|uninstall|run-now]"; exit 1 ;;
esac
