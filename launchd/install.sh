#!/bin/sh
# Install (first run) or restart the dashboard and tick launchd agents for this checkout, then wait for /api/state.
# Safe to rerun. tp setup, tp update and LAN sharing all use this entry point.
set -eu
DIR="$(cd "$(dirname "$0")/.." && pwd)"
# Linux (TP-162): install systemd user units instead.
[ "$(uname)" = Darwin ] || exec /bin/sh "$DIR/systemd/install.sh"
DOMAIN="gui/$(id -u)"
# launchd itself opens the log files, and TCC blocks it from ~/Desktop (and Documents, Downloads) after a reboot, so
# keep them out of there. Override with TP_LOG_DIR (absolute path) on every run.
LOG_DIR="${TP_LOG_DIR:-$HOME/tp-work/logs}"
case "$LOG_DIR" in /*) ;; *) echo "TP_LOG_DIR must be an absolute path (launchd does not expand ~): $LOG_DIR" >&2; exit 1 ;; esac

# The old Docker dashboard wrote data/tp.db over a bind mount that does not pass file locks to the VM,
# which corrupted the DB (TP-8). Remove it so restart: unless-stopped cannot bring it back.
if command -v docker >/dev/null 2>&1 && docker inspect 32_ticket_pilot >/dev/null 2>&1; then
  docker rm -f 32_ticket_pilot
fi

# tp tick replaces the Orca automations of this checkout: turn those off first, or both dispatch the same tickets.
if command -v orca >/dev/null 2>&1; then
  ids=$(orca automations list --json 2>/dev/null | TP_DIR="$DIR" python3 -c '
import json, os, sys
try:
    r = json.load(sys.stdin)["result"]
except Exception:
    sys.exit(0)
for a in (r.get("automations", r) if isinstance(r, dict) else r):
    if a.get("enabled") and ((a.get("precheck") or {}).get("command") or "").startswith(os.environ["TP_DIR"] + "/bin/tp precheck-"):
        print(a["id"])
' || true)
  for id in $ids; do
    orca automations edit "$id" --disabled --json >/dev/null && echo "disabled Orca automation $id (tp tick takes over)"
  done
fi

# The agents' local network permission belongs to the launchd job's python, and every agent a tick starts inherits it.
# Homebrew python is ad-hoc signed, so each upgrade looks like a new app to macOS and LAN access fails with
# "No route to host" (10/26). python.org's build is Developer ID signed and keeps the permission; prefer it.
TP_PYTHON=/Library/Frameworks/Python.framework/Versions/Current/bin/python3
[ -x "$TP_PYTHON" ] || TP_PYTHON=/opt/homebrew/bin/python3
echo "launchd agents use $TP_PYTHON"
. "$DIR/scripts/service-install.sh"
read_dashboard_settings

mkdir -p "$DIR/data" "$LOG_DIR" "$HOME/Library/LaunchAgents"
install_agent() {
  label=$1
  plist="$HOME/Library/LaunchAgents/$label.plist"
  render_service_template "$DIR/launchd/$label.plist" > "$plist.new"
  if cmp -s "$plist.new" "$plist" && launchctl print "$DOMAIN/$label" >/dev/null 2>&1; then
    rm "$plist.new"
    launchctl kickstart -k "$DOMAIN/$label"
  else
    mv "$plist.new" "$plist"
    launchctl bootout "$DOMAIN/$label" 2>/dev/null && sleep 2 || true
    launchctl bootstrap "$DOMAIN" "$plist"
  fi
}
# TP-131 renamed the agents from com.bini59.ticket-pilot.*: unload and remove this checkout's old ones, or the old and
# the new tick both dispatch the same tickets. Old agents of another checkout are left alone.
for old in "$HOME"/Library/LaunchAgents/com.bini59.ticket-pilot.*.plist; do
  [ -f "$old" ] && [ "$(plutil -extract WorkingDirectory raw -o - "$old" 2>/dev/null)" = "$DIR" ] || continue
  label=$(basename "$old" .plist)
  launchctl bootout "$DOMAIN/$label" 2>/dev/null || true  # like kickstart -k: the tick's agents run in their own sessions
  rm "$old"
  echo "removed old launchd agent $label"
done
# kickstart -k on the tick kills a running tick; the agents it started run in their own sessions and survive
install_agent com.ticket-pilot.tick
install_agent com.ticket-pilot.dashboard

wait_for_dashboard "$DOMAIN" "$LOG_DIR/dashboard.log"
