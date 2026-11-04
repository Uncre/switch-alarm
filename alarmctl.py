#!/usr/bin/env python3
"""Tiny CLI for the switch-alarm API.

Usage:
  ALARM_URL=http://127.0.0.1:8767 python3 alarmctl.py status
  python3 alarmctl.py stop
  python3 alarmctl.py test
  python3 alarmctl.py fire [--track ID]
  python3 alarmctl.py set --weekday 07:00 --weekend 09:00 --volume 60 \
      --mode random|specific [--track ID] [--auto-stop 0] [--off]
  python3 alarmctl.py override --date 2026-11-05 --time 06:30 [--track ID]
  python3 alarmctl.py override --date 2026-11-05 --skip
  python3 alarmctl.py clear-override --date 2026-11-05
  python3 alarmctl.py add --url https://www.youtube.com/watch?v=...
"""
import argparse
import json
import os
import sys
import urllib.request

BASE = os.environ.get("ALARM_URL", "http://127.0.0.1:8767")


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or b"{}")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("stop")
    sub.add_parser("test")
    f = sub.add_parser("fire"); f.add_argument("--track")
    s = sub.add_parser("set")
    s.add_argument("--weekday"); s.add_argument("--weekend")
    s.add_argument("--volume", type=int); s.add_argument("--test-volume", type=int); s.add_argument("--mode")
    s.add_argument("--track"); s.add_argument("--auto-stop", type=int)
    s.add_argument("--off", action="store_true"); s.add_argument("--on", action="store_true")
    o = sub.add_parser("override")
    o.add_argument("--date", required=True); o.add_argument("--time")
    o.add_argument("--track"); o.add_argument("--mode"); o.add_argument("--skip", action="store_true")
    c = sub.add_parser("clear-override"); c.add_argument("--date", required=True)
    a = sub.add_parser("add"); a.add_argument("--url", required=True)
    args = ap.parse_args()

    if args.cmd == "status":
        out = call("GET", "/api/status")
    elif args.cmd == "stop":
        out = call("POST", "/api/stop", {})
    elif args.cmd == "test":
        out = call("POST", "/api/test-sound", {})
    elif args.cmd == "fire":
        out = call("POST", "/api/fire", {"track_id": args.track} if args.track else {})
    elif args.cmd == "set":
        body = {}
        if args.weekday: body["weekday_time"] = args.weekday
        if args.weekend: body["weekend_time"] = args.weekend
        if args.volume is not None: body["volume"] = args.volume
        if args.test_volume is not None: body["test_volume"] = args.test_volume
        if args.mode: body["mode"] = args.mode
        if args.track: body["specific_track_id"] = args.track
        if args.auto_stop is not None: body["auto_stop_minutes"] = args.auto_stop
        if args.off: body["enabled"] = False
        if args.on: body["enabled"] = True
        out = call("POST", "/api/config", body)
    elif args.cmd == "override":
        out = call("POST", "/api/override", {
            "date": args.date, "time": args.time, "skip": args.skip,
            "mode": args.mode, "track_id": args.track})
    elif args.cmd == "clear-override":
        out = call("DELETE", f"/api/override/{args.date}")
    elif args.cmd == "add":
        out = call("POST", "/api/tracks", {"url": args.url})
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
