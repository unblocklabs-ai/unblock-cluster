#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME_DIR="${DATA_GRAPH_RUNTIME_DIR:-"$ROOT/local-data/runtime"}"
PID_FILE="$RUNTIME_DIR/data-graph.pid"
LOG_FILE="$RUNTIME_DIR/data-graph.log"

mkdir -p "$RUNTIME_DIR"

case "${1:-status}" in
  start)
    if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
      echo "Data Graph is already running: $(cat "$PID_FILE")"
      exit 0
    fi
    cd "$ROOT"
    nohup npm run serve >"$LOG_FILE" 2>&1 &
    echo "$!" >"$PID_FILE"
    echo "Started Data Graph: $(cat "$PID_FILE")"
    echo "Logs: $LOG_FILE"
    ;;
  stop)
    if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
      kill "$(cat "$PID_FILE")"
      rm -f "$PID_FILE"
      echo "Stopped Data Graph"
    else
      rm -f "$PID_FILE"
      echo "Data Graph is not running"
    fi
    ;;
  restart)
    "$0" stop
    "$0" start
    ;;
  logs)
    touch "$LOG_FILE"
    tail -f "$LOG_FILE"
    ;;
  status)
    if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
      echo "running $(cat "$PID_FILE")"
    else
      echo "stopped"
    fi
    ;;
  *)
    echo "Usage: $0 {start|stop|restart|status|logs}" >&2
    exit 2
    ;;
esac
