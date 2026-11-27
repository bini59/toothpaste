#!/bin/sh
# Sourced by the host service installers after they select TP_PYTHON and LOG_DIR.

read_dashboard_settings() {
  # tp setup may save a different port when the default is taken.
  PORT=$(cd "$DIR" && PYTHONPATH="$DIR" "$TP_PYTHON" -c 'from ticket_pilot.core import settings; print(int(settings()["dashboard_port"]))') || {
    echo "could not read the dashboard_port setting from data/tp.db (see the error above)" >&2
    return 1
  }
  # 로컬망 공유 (TP-152): listen on every interface only while sharing is on.
  HOST=$(cd "$DIR" && PYTHONPATH="$DIR" "$TP_PYTHON" -c 'from ticket_pilot.core import settings; print("0.0.0.0" if settings()["lan_share"]["enabled"] else "127.0.0.1")') || {
    echo "could not read the lan_share setting from data/tp.db (see the error above)" >&2
    return 1
  }
  echo "dashboard listens on $HOST:$PORT"
}

render_service_template() {
  sed -e "s#__TP_DIR__#$DIR#g" -e "s#__TP_PYTHON__#$TP_PYTHON#g" \
      -e "s#__TP_LOG_DIR__#$LOG_DIR#g" -e "s#__TP_HOME__#$HOME#g" \
      -e "s#__TP_PORT__#$PORT#g" -e "s#__TP_HOST__#$HOST#g" "$1"
}

wait_for_dashboard() {
  dashboard_url="http://127.0.0.1:$PORT/api/state"
  for _ in $(seq 30); do
    if curl -fs -o /dev/null --max-time 5 "$dashboard_url"; then
      echo "dashboard up ($1)"
      return 0
    fi
    sleep 1
  done
  echo "dashboard did not answer $dashboard_url; see $2" >&2
  return 1
}
