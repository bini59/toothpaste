#!/bin/sh
# Linux counterpart of launchd/install.sh, which hands over to this one on Linux (every caller runs launchd/install.sh):
# install (first run) or restart the dashboard service and the tick timer as systemd user units of this checkout, then
# wait for /api/state. Idempotent. Settings, TP_LOG_DIR and TP_HOST follow the same rules as the launchd side.
set -eu
DIR="$(cd "$(dirname "$0")/.." && pwd)"
LOG_DIR="${TP_LOG_DIR:-$HOME/tp-work/logs}"
case "$LOG_DIR" in /*) ;; *) echo "TP_LOG_DIR must be an absolute path (systemd does not expand ~): $LOG_DIR" >&2; exit 1 ;; esac
# shortcut: systemd reads % in unit files as a specifier; a checkout or log path with % is refused, escape it if one shows up
case "$DIR$LOG_DIR$HOME" in *%*) echo "paths with % are not supported in systemd units: $DIR $LOG_DIR" >&2; exit 1 ;; esac

if ! command -v systemctl >/dev/null 2>&1 || ! systemctl --user show-environment >/dev/null 2>&1; then
  echo "systemctl --user를 쓸 수 없어요 (systemd 사용자 세션이 없어요: WSL이나 컨테이너, 또는 su로 바꾼 셸). 그 사용자로 직접 로그인해서 실행하세요" >&2
  exit 1
fi

TP_PYTHON=$(command -v python3) || { echo "python3이 없어요" >&2; exit 1; }
echo "systemd units use $TP_PYTHON"
PORT=$(cd "$DIR" && PYTHONPATH="$DIR" "$TP_PYTHON" -c 'from ticket_pilot.core import settings; print(int(settings()["dashboard_port"]))') || {
  echo "could not read the dashboard_port setting from data/tp.db (see the error above)" >&2; exit 1; }
HOST=$(cd "$DIR" && PYTHONPATH="$DIR" "$TP_PYTHON" -c 'from ticket_pilot.core import settings; print("0.0.0.0" if settings()["lan_share"]["enabled"] else "127.0.0.1")') || {
  echo "could not read the lan_share setting from data/tp.db (see the error above)" >&2; exit 1; }
echo "dashboard listens on $HOST:$PORT"

UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$DIR/data" "$LOG_DIR" "$UNIT_DIR"
for unit in ticket-pilot-dashboard.service ticket-pilot-tick.service ticket-pilot-tick.timer; do
  sed -e "s#__TP_DIR__#$DIR#g" -e "s#__TP_PYTHON__#$TP_PYTHON#g" -e "s#__TP_LOG_DIR__#$LOG_DIR#g" -e "s#__TP_HOME__#$HOME#g" -e "s#__TP_PORT__#$PORT#g" -e "s#__TP_HOST__#$HOST#g" "$DIR/systemd/$unit" > "$UNIT_DIR/$unit.new"
  mv "$UNIT_DIR/$unit.new" "$UNIT_DIR/$unit"
done
systemctl --user daemon-reload
systemctl --user enable --now ticket-pilot-tick.timer
systemctl --user enable ticket-pilot-dashboard.service
# restart, not just start: a rerun (tp update, 로컬망 공유) must load the new code and settings
systemctl --user restart ticket-pilot-dashboard.service

if [ "$(loginctl show-user "$(id -un)" -p Linger --value 2>/dev/null || true)" != yes ]; then
  echo "주의: linger가 꺼져 있어 로그아웃하면 대시보드와 tick이 멈춰요. 계속 돌리려면: sudo loginctl enable-linger $(id -un)"
fi

for _ in $(seq 30); do
  curl -fs -o /dev/null --max-time 5 http://127.0.0.1:$PORT/api/state && { echo "dashboard up (systemd --user)"; exit 0; }
  sleep 1
done
echo "dashboard did not answer http://127.0.0.1:$PORT/api/state; see $LOG_DIR/dashboard.log (journalctl --user -u ticket-pilot-dashboard)" >&2
exit 1
