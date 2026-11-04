#!/bin/sh
# Stop the switch-alarm daemon started by start.sh.
cd "$(dirname "$0")"
if [ -f data/alarm.pid ]; then
  PID="$(cat data/alarm.pid)"
  pkill -P "$PID" 2>/dev/null || true
  kill "$PID" 2>/dev/null || true
  pkill -f "app.py" 2>/dev/null || true
  rm -f data/alarm.pid
  echo "stopped"
else
  echo "no pid file"
fi
