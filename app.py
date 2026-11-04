#!/usr/bin/env python3
"""switch-alarm: web-controlled morning alarm for a home Linux box.

Single-file app, Python standard library only.

- Scheduler: fires at weekday_time (Mon-Fri) / weekend_time (Sat-Sun) by
  default, or at a per-date override set through the API / web UI.
- Alarm playback loops the chosen track and does NOT stop on its own
  (auto_stop_minutes = 0). Stop it from the web UI or the API.
- Tracks are downloaded with yt-dlp; every downloaded track joins the
  random pool. Playback is either random or one specific track.
- Playback goes through PipeWire/PulseAudio (mpv), with an ALSA retry.
"""
import json
import os
import random
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("ALARM_DATA_DIR", BASE_DIR / "data"))
MUSIC_DIR = DATA_DIR / "music"
CONFIG_PATH = DATA_DIR / "config.json"
TRACKS_PATH = DATA_DIR / "tracks.json"
STATE_PATH = DATA_DIR / "state.json"
PORT = int(os.environ.get("ALARM_PORT", "8767"))
_uid = os.getuid()
AUDIO_ENV = dict(
    os.environ,
    XDG_RUNTIME_DIR=os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{_uid}"),
    DBUS_SESSION_BUS_ADDRESS=os.environ.get(
        "DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{_uid}/bus"
    ),
)

DEFAULT_CONFIG = {
    "enabled": True,
    "weekday_time": "07:00",
    "weekend_time": "09:00",
    "volume": 100,           # alarm (real) playback volume percent: full by default
    "test_volume": 25,       # manual / test playback volume percent: keep it quiet
    "mode": "random",        # "random" | "specific"
    "specific_track_id": None,
    "auto_stop_minutes": 0,  # 0 = never auto-stop (alarm keeps looping)
    "overrides": {},         # "YYYY-MM-DD": {"time": "06:30"|None, "skip": bool,
                             #                "mode": ..., "track_id": ...}
}
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
STORE_LOCK = threading.Lock()


def _load_json(path, fallback):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return fallback


def _save_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


class Store:
    def __init__(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        MUSIC_DIR.mkdir(parents=True, exist_ok=True)
        cfg = dict(DEFAULT_CONFIG)
        cfg.update(_load_json(CONFIG_PATH, {}))
        self.config = cfg
        self.tracks = _load_json(TRACKS_PATH, [])
        self.state = {"last_fired": None, "last_error": None}
        self.state.update(_load_json(STATE_PATH, {}))

    def save_config(self):
        _save_json(CONFIG_PATH, self.config)

    def save_tracks(self):
        _save_json(TRACKS_PATH, self.tracks)

    def save_state(self):
        _save_json(STATE_PATH, self.state)

    def track_by_id(self, tid):
        for t in self.tracks:
            if t["id"] == tid:
                return t
        return None


STORE = Store()
JOBS = {}  # job_id -> dict


def log(msg):
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}", flush=True)


def ytdlp_cmd():
    local = Path.home() / ".local" / "bin" / "yt-dlp"
    if local.exists():
        return [str(local)]
    return ["yt-dlp"]


def set_volume(percent):
    try:
        subprocess.run(
            ["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{int(percent)}%"],
            env=AUDIO_ENV, timeout=10, capture_output=True,
        )
    except Exception as e:
        log(f"set_volume failed: {e}")


class Player:
    """One playback at a time. Alarm playback loops until stopped."""

    def __init__(self):
        self.lock = threading.Lock()
        self.proc = None
        self.info = None  # {kind, track_id, track_title, started_at, auto_stop_at}

    def playing(self):
        with self.lock:
            if self.proc and self.proc.poll() is None:
                return dict(self.info) if self.info else {}
            return None

    def _spawn(self, args):
        return subprocess.Popen(
            args, env=AUDIO_ENV, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    def play_file(self, path, kind, track=None, loop=False, volume=None):
        self.stop()
        if volume is not None:
            set_volume(volume)
        args = ["mpv", "--no-video", "--really-quiet"]
        if loop:
            args.append("--loop-file=inf")
        args.append(str(path))
        proc = self._spawn(args)
        time.sleep(1.5)
        if proc.poll() is not None:
            # PipeWire/Pulse may be unavailable (e.g. no user session yet):
            # retry once through raw ALSA.
            log("mpv exited immediately; retrying with --ao=alsa")
            args.insert(2, "--ao=alsa")
            proc = self._spawn(args)
            time.sleep(1.0)
        auto_stop_at = None
        mins = int(STORE.config.get("auto_stop_minutes") or 0)
        if kind == "alarm" and mins > 0:
            auto_stop_at = (datetime.now() + timedelta(minutes=mins)).isoformat(timespec="seconds")
            threading.Thread(target=self._auto_stop, args=(proc, mins * 60), daemon=True).start()
        with self.lock:
            self.proc = proc
            self.info = {
                "kind": kind,
                "track_id": (track or {}).get("id"),
                "track_title": (track or {}).get("title") or Path(path).name,
                "started_at": datetime.now().isoformat(timespec="seconds"),
                "loop": loop,
                "auto_stop_at": auto_stop_at,
            }
        log(f"playing ({kind}): {self.info['track_title']}")
        return proc.poll() is None

    def _auto_stop(self, proc, seconds):
        time.sleep(seconds)
        with self.lock:
            if self.proc is proc:
                log("auto-stop reached")
                self._stop_locked()

    def stop(self):
        with self.lock:
            return self._stop_locked()

    def _stop_locked(self):
        was = self.proc is not None and self.proc.poll() is None
        if self.proc:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
                try:
                    self.proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
            except Exception:
                pass
        self.proc = None
        self.info = None
        if was:
            log("playback stopped")
        return was

    def play_test_sound(self):
        candidates = [
            "/usr/share/sounds/alsa/Front_Center.wav",
            "/usr/share/sounds/freedesktop/stereo/alarm-clock-elapsed.oga",
        ]
        path = next((c for c in candidates if Path(c).exists()), None)
        if path is None:
            return False
        self.stop()
        set_volume(STORE.config.get("test_volume", 25))
        proc = self._spawn(["paplay", path])
        with self.lock:
            self.proc = proc
            self.info = {
                "kind": "test", "track_id": None,
                "track_title": "テスト音", "started_at": datetime.now().isoformat(timespec="seconds"),
                "loop": False, "auto_stop_at": None,
            }
        return True


PLAYER = Player()


def choose_track(mode=None, track_id=None):
    cfg = STORE.config
    mode = mode or cfg.get("mode", "random")
    tid = track_id or cfg.get("specific_track_id")
    if mode == "specific" and tid:
        t = STORE.track_by_id(tid)
        if t:
            return t
    if STORE.tracks:
        return random.choice(STORE.tracks)
    return None


def fire_alarm(source="schedule", mode=None, track_id=None):
    track = choose_track(mode, track_id)
    if track:
        path = MUSIC_DIR / track["file"]
        ok = PLAYER.play_file(path, "alarm", track=track, loop=True,
                              volume=STORE.config.get("volume", 100))
    else:
        # No tracks yet: fall back to a looping system alarm sound.
        fallback = "/usr/share/sounds/freedesktop/stereo/alarm-clock-elapsed.oga"
        ok = False
        if Path(fallback).exists():
            ok = PLAYER.play_file(fallback, "alarm",
                                  track={"id": None, "title": "システムアラーム音"},
                                  loop=True, volume=STORE.config.get("volume", 100))
    if not ok:
        with STORE_LOCK:
            STORE.state["last_error"] = f"playback failed at {datetime.now().isoformat(timespec='seconds')}"
            STORE.save_state()
    log(f"alarm fired ({source}) ok={ok} track={track and track.get('title')}")
    return ok


# ---------------- downloader ----------------

def download_job(job_id, url):
    job = JOBS[job_id]
    try:
        job["status"] = "fetching info"
        meta_raw = subprocess.run(
            ytdlp_cmd() + ["--no-playlist", "-J", url],
            capture_output=True, text=True, timeout=180,
        )
        if meta_raw.returncode != 0:
            raise RuntimeError((meta_raw.stderr or "yt-dlp failed")[-500:])
        meta = json.loads(meta_raw.stdout)
        vid = meta["id"]
        title = meta.get("title") or vid
        with STORE_LOCK:
            existing = STORE.track_by_id(vid)
        if existing:
            job.update(status="done", title=title, note="already in library")
            return
        job["status"] = "downloading"
        out = subprocess.run(
            ytdlp_cmd() + [
                "--no-playlist", "-f", "bestaudio/best", "-x",
                "--audio-format", "mp3", "--audio-quality", "5",
                "-o", str(MUSIC_DIR / "%(id)s.%(ext)s"), url,
            ],
            capture_output=True, text=True, timeout=1800,
        )
        if out.returncode != 0:
            raise RuntimeError((out.stderr or "download failed")[-500:])
        filename = f"{vid}.mp3"
        if not (MUSIC_DIR / filename).exists():
            cands = sorted(MUSIC_DIR.glob(f"{vid}.*"))
            if not cands:
                raise RuntimeError("downloaded file not found")
            filename = cands[0].name
        duration = meta.get("duration")
        with STORE_LOCK:
            STORE.tracks.append({
                "id": vid, "title": title, "url": url, "file": filename,
                "duration": duration, "uploader": meta.get("uploader"),
                "added_at": datetime.now().isoformat(timespec="seconds"),
            })
            STORE.save_tracks()
        job.update(status="done", title=title)
        log(f"track added: {title} ({vid})")
    except Exception as e:
        job.update(status="error", error=str(e))
        log(f"download job failed: {e}")


# ---------------- scheduler ----------------

def scheduled_for(d: date):
    """Return (time_str or None, override or None) for the given date."""
    cfg = STORE.config
    ov = (cfg.get("overrides") or {}).get(d.isoformat())
    if ov:
        if ov.get("skip"):
            return None, ov
        if ov.get("time"):
            return ov["time"], ov
    if d.weekday() >= 5:
        return cfg.get("weekend_time", "09:00"), None
    return cfg.get("weekday_time", "07:00"), None


def next_alarm(now=None):
    now = now or datetime.now()
    for offset in range(0, 8):
        d = now.date() + timedelta(days=offset)
        t_str, ov = scheduled_for(d)
        if not t_str:
            continue
        hh, mm = map(int, t_str.split(":"))
        when = datetime(d.year, d.month, d.day, hh, mm)
        if when > now:
            return {"date": d.isoformat(), "time": t_str,
                    "datetime": when.isoformat(timespec="minutes"),
                    "override": bool(ov)}
    return None


def scheduler_loop():
    GRACE = timedelta(minutes=180)
    while True:
        try:
            now = datetime.now()
            today = now.date()
            if STORE.config.get("enabled", True):
                with STORE_LOCK:
                    last = STORE.state.get("last_fired")
                t_str, ov = scheduled_for(today)
                if t_str and last != today.isoformat():
                    hh, mm = map(int, t_str.split(":"))
                    sched = datetime(today.year, today.month, today.day, hh, mm)
                    if sched <= now <= sched + GRACE:
                        with STORE_LOCK:
                            STORE.state["last_fired"] = today.isoformat()
                            # one-day override is consumed by firing
                            if ov:
                                (STORE.config.get("overrides") or {}).pop(today.isoformat(), None)
                                STORE.save_config()
                            STORE.save_state()
                        fire_alarm(source="schedule",
                                   mode=(ov or {}).get("mode"),
                                   track_id=(ov or {}).get("track_id"))
        except Exception as e:
            log(f"scheduler error: {e}")
        time.sleep(15)


def status_payload():
    with STORE_LOCK:
        cfg = json.loads(json.dumps(STORE.config))
        tracks = json.loads(json.dumps(STORE.tracks))
        state = dict(STORE.state)
    return {
        "now": datetime.now().isoformat(timespec="seconds"),
        "config": cfg,
        "tracks": tracks,
        "state": state,
        "playing": PLAYER.playing(),
        "next_alarm": next_alarm(),
        "jobs": {k: v for k, v in JOBS.items() if v.get("status") not in ("done",)},
    }


# ---------------- HTTP ----------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/":
            self._send(200, INDEX_HTML, "text/html; charset=utf-8")
        elif u.path == "/api/status":
            self._send(200, status_payload())
        elif u.path.startswith("/api/jobs/"):
            job = JOBS.get(u.path.rsplit("/", 1)[-1])
            self._send(200 if job else 404, job or {"error": "no such job"})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        try:
            body = self._json_body()
        except Exception:
            return self._send(400, {"error": "bad json"})
        try:
            if u.path == "/api/config":
                self._update_config(body)
                self._send(200, status_payload())
            elif u.path == "/api/stop":
                self._send(200, {"stopped": PLAYER.stop()})
            elif u.path == "/api/test-sound":
                self._send(200, {"ok": PLAYER.play_test_sound()})
            elif u.path == "/api/play":
                t = STORE.track_by_id(body.get("track_id") or "")
                if not t:
                    return self._send(404, {"error": "track not found"})
                ok = PLAYER.play_file(MUSIC_DIR / t["file"], "manual", track=t,
                                      loop=False, volume=STORE.config.get("test_volume", 25))
                self._send(200, {"ok": ok})
            elif u.path == "/api/fire":
                ok = fire_alarm(source="api", mode=body.get("mode"),
                                track_id=body.get("track_id"))
                self._send(200, {"ok": ok})
            elif u.path == "/api/tracks":
                url = (body.get("url") or "").strip()
                if not url.startswith("http"):
                    return self._send(400, {"error": "url required"})
                job_id = uuid.uuid4().hex[:12]
                JOBS[job_id] = {"id": job_id, "url": url, "status": "queued",
                                "started_at": datetime.now().isoformat(timespec="seconds")}
                threading.Thread(target=download_job, args=(job_id, url), daemon=True).start()
                self._send(202, JOBS[job_id])
            elif u.path == "/api/override":
                self._set_override(body)
                self._send(200, status_payload())
            else:
                self._send(404, {"error": "not found"})
        except ValueError as e:
            self._send(400, {"error": str(e)})

    def do_DELETE(self):
        u = urllib.parse.urlparse(self.path)
        if u.path.startswith("/api/tracks/"):
            tid = u.path.rsplit("/", 1)[-1]
            with STORE_LOCK:
                t = STORE.track_by_id(tid)
                if not t:
                    return self._send(404, {"error": "track not found"})
                STORE.tracks = [x for x in STORE.tracks if x["id"] != tid]
                if STORE.config.get("specific_track_id") == tid:
                    STORE.config["specific_track_id"] = None
                    STORE.save_config()
                STORE.save_tracks()
            try:
                (MUSIC_DIR / t["file"]).unlink(missing_ok=True)
            except Exception:
                pass
            self._send(200, {"deleted": tid})
        elif u.path.startswith("/api/override/"):
            d = u.path.rsplit("/", 1)[-1]
            with STORE_LOCK:
                (STORE.config.get("overrides") or {}).pop(d, None)
                STORE.save_config()
            self._send(200, status_payload())
        else:
            self._send(404, {"error": "not found"})

    def _update_config(self, body):
        with STORE_LOCK:
            cfg = STORE.config
            for key in ("weekday_time", "weekend_time"):
                if key in body:
                    v = str(body[key])
                    if not TIME_RE.match(v):
                        raise ValueError(f"{key} must be HH:MM")
                    cfg[key] = v
            if "enabled" in body:
                cfg["enabled"] = bool(body["enabled"])
            if "volume" in body:
                cfg["volume"] = max(0, min(100, int(body["volume"])))
            if "test_volume" in body:
                cfg["test_volume"] = max(0, min(100, int(body["test_volume"])))
            if "auto_stop_minutes" in body:
                cfg["auto_stop_minutes"] = max(0, int(body["auto_stop_minutes"]))
            if "mode" in body:
                if body["mode"] not in ("random", "specific"):
                    raise ValueError("mode must be random|specific")
                cfg["mode"] = body["mode"]
            if "specific_track_id" in body:
                cfg["specific_track_id"] = body["specific_track_id"] or None
            STORE.save_config()

    def _set_override(self, body):
        d = str(body.get("date") or "")
        if not DATE_RE.match(d):
            raise ValueError("date must be YYYY-MM-DD")
        ov = {"skip": bool(body.get("skip"))}
        t = body.get("time")
        if t:
            if not TIME_RE.match(str(t)):
                raise ValueError("time must be HH:MM")
            ov["time"] = str(t)
        if body.get("mode") in ("random", "specific"):
            ov["mode"] = body["mode"]
        if body.get("track_id"):
            ov["track_id"] = body["track_id"]
        with STORE_LOCK:
            STORE.config.setdefault("overrides", {})[d] = ov
            STORE.save_config()


INDEX_HTML = r"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>switch-alarm</title>
<style>
body{font-family:system-ui,-apple-system,"Segoe UI",sans-serif;background:#101418;color:#e8edf2;margin:0;padding:16px}
.wrap{max-width:720px;margin:0 auto}
h1{font-size:22px;margin:8px 0 2px}
.sub{color:#9fb0c0;font-size:13px;margin-bottom:16px}
.card{background:#1a2129;border:1px solid #2a3542;border-radius:12px;padding:16px;margin-bottom:14px}
.card h2{font-size:16px;margin:0 0 12px}
.big{font-size:28px;font-weight:700}
button{background:#2f81f7;color:#fff;border:0;border-radius:8px;padding:9px 14px;font-size:14px;cursor:pointer}
button.sub2{background:#37414d} button.danger{background:#d64545} button:disabled{opacity:.5}
input,select{background:#0d1117;color:#e8edf2;border:1px solid #3a4655;border-radius:8px;padding:8px;font-size:14px}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin:8px 0}
label{font-size:13px;color:#b8c6d4}
.track{display:flex;justify-content:space-between;gap:8px;padding:9px 0;border-bottom:1px solid #26303c;align-items:center}
.track:last-child{border-bottom:0}
.ttl{font-size:14px}.meta{color:#8fa1b3;font-size:12px}
.stopbtn{width:100%;padding:16px;font-size:19px;font-weight:700}
.pill{display:inline-block;padding:2px 9px;border-radius:99px;background:#23405e;font-size:12px}
.note{color:#8fa1b3;font-size:12px;line-height:1.6}
a{color:#6db3f2}
</style>
</head>
<body>
<div class="wrap">
<h1>⏰ switch-alarm</h1>
<div class="sub">自宅Linuxのアラーム。止めるまで鳴り続けます。</div>

<div class="card">
  <h2>状態</h2>
  <div id="nextAlarm" class="big">…</div>
  <div id="playState" style="margin-top:8px"></div>
  <div class="row" style="margin-top:12px">
    <button class="danger stopbtn" onclick="stopPlay()">⏹ 停止</button>
  </div>
  <div class="row">
    <button class="sub2" onclick="testSound()">🔊 音響テスト</button>
    <button class="sub2" onclick="fireNow()">▶ 今すぐアラーム再生</button>
    <span id="msg" class="meta"></span>
  </div>
</div>

<div class="card">
  <h2>基本設定</h2>
  <div class="row"><label><input type="checkbox" id="enabled"> 有効</label></div>
  <div class="row">
    <label>平日 <input type="time" id="weekday_time"></label>
    <label>土日 <input type="time" id="weekend_time"></label>
    <label>本番音量 <input type="number" id="volume" min="0" max="100" style="width:70px"> %</label>
    <label>テスト音量 <input type="number" id="test_volume" min="0" max="100" style="width:70px"> %</label>
  </div>
  <div class="note">本番アラームは本番音量（既定100%）で、曲の再生・音響テストはテスト音量で鳴ります。</div>
  <div class="row">
    <label>自動停止 <input type="number" id="auto_stop_minutes" min="0" style="width:70px"> 分（0 = 止めない）</label>
  </div>
  <div class="row">
    <label><input type="radio" name="mode" value="random"> ランダム</label>
    <label><input type="radio" name="mode" value="specific"> 曲指定</label>
    <select id="specific_track_id"></select>
  </div>
  <div class="row"><button onclick="saveConfig()">保存</button></div>
</div>

<div class="card">
  <h2>日付指定（その日だけ変更 / Museへの指示もここに反映されます）</h2>
  <div class="row">
    <input type="date" id="ov_date">
    <input type="time" id="ov_time">
    <label><input type="checkbox" id="ov_skip"> その日は鳴らさない</label>
    <button onclick="saveOverride()">設定</button>
  </div>
  <div id="ovList"></div>
</div>

<div class="card">
  <h2>曲ライブラリ（ランダム再生リスト）</h2>
  <div class="row">
    <input id="new_url" type="url" placeholder="YouTubeなどのURL" style="flex:1;min-width:240px">
    <button onclick="addTrack()">yt-dlpで追加</button>
  </div>
  <div id="jobState" class="meta"></div>
  <div id="tracks"></div>
  <div class="note">追加した曲は自動でランダム再生の候補に入ります。</div>
</div>
</div>

<script>
const $=id=>document.getElementById(id);
let ST=null;
async function api(path,opt){const r=await fetch(path,opt);return r.json();}
function fmtDur(s){if(!s)return"";s=Math.round(s);return Math.floor(s/60)+":"+String(s%60).padStart(2,"0");}
async function refresh(){
  ST=await api("/api/status");
  const na=ST.next_alarm;
  $("nextAlarm").textContent=ST.config.enabled?(na?("次回: "+na.date+" "+na.time+(na.override?"（日付指定）":"")):"次回なし"):"無効化中";
  const p=ST.playing;
  $("playState").innerHTML=p?('<span class="pill">再生中</span> '+p.track_title+' <span class="meta">'+p.kind+" / "+p.started_at+(p.loop?" / ループ":"")+(p.auto_stop_at?" / 自動停止 "+p.auto_stop_at:"")+"</span>"):'<span class="meta">再生していません</span>';
  const c=ST.config;
  $("enabled").checked=c.enabled; $("weekday_time").value=c.weekday_time; $("weekend_time").value=c.weekend_time;
  $("volume").value=c.volume; $("test_volume").value=c.test_volume; $("auto_stop_minutes").value=c.auto_stop_minutes;
  document.querySelectorAll('input[name="mode"]').forEach(r=>r.checked=(r.value===c.mode));
  const sel=$("specific_track_id"); const cur=sel.value;
  sel.innerHTML=ST.tracks.map(t=>'<option value="'+t.id+'">'+t.title+'</option>').join("");
  sel.value=c.specific_track_id||cur||(ST.tracks[0]&&ST.tracks[0].id)||"";
  $("tracks").innerHTML=ST.tracks.length?ST.tracks.map(t=>
    '<div class="track"><div><div class="ttl">'+t.title+'</div><div class="meta">'+fmtDur(t.duration)+(t.uploader?" / "+t.uploader:"")+'</div></div>'+
    '<div class="row" style="margin:0"><button class="sub2" onclick="playTrack(\''+t.id+'\')">再生</button>'+
    '<button class="sub2" onclick="setSpecific(\''+t.id+'\')">アラームに指定</button>'+
    '<button class="danger" onclick="delTrack(\''+t.id+'\')">削除</button></div></div>').join("")
    :'<div class="meta">まだ曲がありません</div>';
  const ovs=Object.entries(c.overrides||{});
  $("ovList").innerHTML=ovs.length?ovs.map(([d,o])=>'<div class="track"><div>'+d+'： '+(o.skip?"鳴らさない":(o.time||"通常時刻"))+'</div><button class="sub2" onclick="delOverride(\''+d+'\')">解除</button></div>').join(""):'<div class="meta">日付指定はありません</div>';
  const jobs=Object.values(ST.jobs||{});
  $("jobState").textContent=jobs.length?("取得中: "+jobs.map(j=>j.status+" "+j.url).join(" / ")):"";
}
function note(m){$("msg").textContent=m; setTimeout(()=>$("msg").textContent="",4000);}
async function post(path,body){const r=await api(path,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body||{})}); await refresh(); return r;}
async function saveConfig(){
  const mode=document.querySelector('input[name="mode"]:checked').value;
  await post("/api/config",{enabled:$("enabled").checked,weekday_time:$("weekday_time").value,weekend_time:$("weekend_time").value,volume:+$("volume").value,test_volume:+$("test_volume").value,auto_stop_minutes:+$("auto_stop_minutes").value,mode:mode,specific_track_id:$("specific_track_id").value||null});
  note("保存しました");
}
async function stopPlay(){await post("/api/stop"); note("止めました");}
async function testSound(){await post("/api/test-sound"); note("テスト音を鳴らしました");}
async function fireNow(){await post("/api/fire",{}); note("アラーム再生を開始しました");}
async function playTrack(id){await post("/api/play",{track_id:id});}
async function setSpecific(id){await post("/api/config",{mode:"specific",specific_track_id:id}); note("アラーム曲に指定しました");}
async function delTrack(id){if(!confirm("この曲を削除しますか？"))return; await api("/api/tracks/"+id,{method:"DELETE"}); await refresh();}
async function saveOverride(){
  const d=$("ov_date").value; if(!d){alert("日付を選んでください");return;}
  await post("/api/override",{date:d,time:$("ov_skip").checked?null:($("ov_time").value||null),skip:$("ov_skip").checked});
  note("日付指定を保存しました");
}
async function delOverride(d){await api("/api/override/"+d,{method:"DELETE"}); await refresh();}
async function addTrack(){
  const url=$("new_url").value.trim(); if(!url)return;
  const r=await post("/api/tracks",{url:url}); $("new_url").value="";
  note("取得を開始しました"); pollJob(r.id);
}
async function pollJob(id){
  if(!id)return;
  const j=await api("/api/jobs/"+id);
  if(j.status==="done"){note("追加しました: "+(j.title||"")); await refresh(); return;}
  if(j.status==="error"){alert("追加に失敗しました: "+j.error); await refresh(); return;}
  $("jobState").textContent="取得中: "+j.status;
  setTimeout(()=>pollJob(id),3000);
}
refresh(); setInterval(refresh,5000);
</script>
</body>
</html>
"""


def main():
    threading.Thread(target=scheduler_loop, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    log(f"switch-alarm listening on :{PORT} (data: {DATA_DIR})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
