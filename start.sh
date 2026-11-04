#!/bin/sh
# Start switch-alarm in the background (idempotent).
set -e
cd "$(dirname "$0")"
mkdir -p data
if [ -f data/alarm.pid ] && kill -0 "$(cat data/alarm.pid)" 2>/dev/null; then
  echo "already running (pid $(cat data/alarm.pid))"
  exit 0
fi
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=/run/user/$(id -u)/bus}"
nohup sh -c 'while true; do python3 "$(dirname "$0")/app.py" >> "$(dirname "$0")/data/alarm.log" 2>&1; sleep 5; done' >/dev/null 2>&1 &
echo $! > data/alarm.pid
echo "started (pid $!)"
