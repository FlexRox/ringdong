import base64
import json
import os
import queue
import secrets
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from datetime import datetime
from functools import wraps
from pathlib import Path

from flask import Flask, Response, jsonify, redirect, request, send_from_directory, session
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from pywebpush import WebPushException, webpush
import paho.mqtt.client as mqtt
import paho.mqtt.publish as mqtt_publish

app = Flask(__name__, static_folder="static", static_url_path="")
app.secret_key = os.getenv("APP_SECRET", "ringdong-dev-secret-change-me")

# ---- Base ENV ---------------------------------------------------------------
MQTT_HOST = os.getenv("MQTT_HOST", "192.168.10.76")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_USER = os.getenv("MQTT_USER", "")
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD", "")

RING_LOCATION_ID = os.getenv("RING_LOCATION_ID", "558160f8-03ae-48da-bd30-8999e38d0269")
RING_CAMERA_ID = os.getenv("RING_CAMERA_ID", "343ea4dd1515")
RING_INTERCOM_ID = os.getenv("RING_INTERCOM_ID", "343ea4b1121d")

DEFAULT_STREAM = f"{RING_CAMERA_ID}_live"
DEFAULT_RTSP_STREAM_URL = os.getenv("RTSP_STREAM_URL", f"rtsp://192.168.10.76:8554/{DEFAULT_STREAM}")
DEFAULT_GO2RTC_URL = os.getenv("GO2RTC_URL", "http://192.168.10.76:1985")

DEFAULT_SETTINGS_PATH = os.getenv("SETTINGS_PATH", "/data/video/settings.json")
DEFAULT_VIDEO_DIR = os.getenv("VIDEO_DIR", "/data/video")
DEFAULT_TTS_DIR = os.getenv("TTS_DIR", "/data/tts")
DEFAULT_ANNOUNCE_DIR = os.getenv("ANNOUNCE_DIR", "/data/announcements")
DEFAULT_USERS_PATH = os.getenv("USERS_PATH", "/data/video/users.json")
DEFAULT_PUSH_SUBS_PATH = os.getenv("PUSH_SUBSCRIPTIONS_PATH", "/data/video/push_subscriptions.json")
DEFAULT_VAPID_KEYS_PATH = os.getenv("VAPID_KEYS_PATH", "/data/video/vapid_keys.json")
VAPID_SUBJECT = os.getenv("VAPID_SUBJECT", "mailto:admin@ringdong.local")

LATEST_NAME = "latest.mp4"
MAX_INDEX_FILES = int(os.getenv("MAX_INDEX_FILES", "300"))

DEFAULT_SETTINGS = {
    "stream": DEFAULT_STREAM,
    "rtsp_stream_url": DEFAULT_RTSP_STREAM_URL,
    "go2rtc_url": DEFAULT_GO2RTC_URL,
    "record_duration_sec": int(os.getenv("RECORD_DURATION_SEC", "25")),
    "record_cooldown_sec": int(os.getenv("RECORD_COOLDOWN_SEC", "20")),
    "record_on_ding": os.getenv("RECORD_ON_DING", "true").lower() == "true",
    "record_on_motion": os.getenv("RECORD_ON_MOTION", "true").lower() == "true",
    "tts_voice": os.getenv("TTS_VOICE", "de-DE-SeraphinaMultilingualNeural"),
    "tts_rate": os.getenv("TTS_RATE", "+0%"),
    "favorites": ["live", "unlock", "record", "tts"],
    "dashboard": {
        "live_title": "Live Video",
        "devices_title": "Geräte",
        "settings_title": "Einstellungen",
        "events_title": "Protokoll",
    },
}

ALLOWED_AUDIO_EXT = {".mp3", ".wav", ".m4a", ".webm", ".ogg"}

# ---- Runtime state ----------------------------------------------------------
sse_clients = []
sse_lock = threading.Lock()
recent_events = deque(maxlen=80)
record_lock = threading.Lock()
settings_lock = threading.Lock()
push_lock = threading.Lock()
users_lock = threading.Lock()
last_record_ts = 0.0
runtime_settings = {}
vapid_keys = {}

VIDEO_DIR = Path(DEFAULT_VIDEO_DIR)
TTS_DIR = Path(DEFAULT_TTS_DIR)
ANNOUNCE_DIR = Path(DEFAULT_ANNOUNCE_DIR)
SETTINGS_PATH = Path(DEFAULT_SETTINGS_PATH)
USERS_PATH = Path(DEFAULT_USERS_PATH)
PUSH_SUBS_PATH = Path(DEFAULT_PUSH_SUBS_PATH)
VAPID_KEYS_PATH = Path(DEFAULT_VAPID_KEYS_PATH)

for d in (VIDEO_DIR, TTS_DIR, ANNOUNCE_DIR, SETTINGS_PATH.parent, USERS_PATH.parent, PUSH_SUBS_PATH.parent, VAPID_KEYS_PATH.parent):
    d.mkdir(parents=True, exist_ok=True)


def now_hms() -> str:
    return datetime.now().strftime("%H:%M:%S")


def public_video_url(name: str) -> str:
    return f"/video/{name}"


def public_tts_url(name: str) -> str:
    return f"/tts/{name}"


def public_announce_url(name: str) -> str:
    return f"/announce/{name}"


def emit_event(event_type: str, label: str, url: str = "") -> None:
    evt = {
        "type": event_type,
        "label": label,
        "time": now_hms(),
        "url": url,
    }
    recent_events.appendleft(evt)
    with sse_lock:
        dead = []
        for q in sse_clients:
            try:
                q.put_nowait(evt)
            except Exception:
                dead.append(q)
        for q in dead:
            if q in sse_clients:
                sse_clients.remove(q)

    if event_type in ("ding", "motion", "unlock"):
        fanout_push(title=f"RingDong • {event_type.upper()}", body=label, url=url or "/")


def as_int(value, fallback: int, min_v: int, max_v: int) -> int:
    try:
        n = int(value)
        return max(min_v, min(max_v, n))
    except Exception:
        return fallback


def as_bool(value, fallback: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("1", "true", "yes", "on"):
            return True
        if v in ("0", "false", "no", "off"):
            return False
    return fallback


def merge_settings(base: dict, incoming: dict) -> dict:
    out = dict(base)
    out["stream"] = str(incoming.get("stream", out["stream"])).strip() or out["stream"]
    out["rtsp_stream_url"] = str(incoming.get("rtsp_stream_url", out["rtsp_stream_url"])).strip() or out["rtsp_stream_url"]
    out["go2rtc_url"] = str(incoming.get("go2rtc_url", out["go2rtc_url"])).strip().rstrip("/") or out["go2rtc_url"]

    out["record_duration_sec"] = as_int(incoming.get("record_duration_sec", out["record_duration_sec"]), out["record_duration_sec"], 3, 120)
    out["record_cooldown_sec"] = as_int(incoming.get("record_cooldown_sec", out["record_cooldown_sec"]), out["record_cooldown_sec"], 0, 300)
    out["record_on_ding"] = as_bool(incoming.get("record_on_ding", out["record_on_ding"]), out["record_on_ding"])
    out["record_on_motion"] = as_bool(incoming.get("record_on_motion", out["record_on_motion"]), out["record_on_motion"])

    out["tts_voice"] = str(incoming.get("tts_voice", out["tts_voice"])).strip() or out["tts_voice"]
    out["tts_rate"] = str(incoming.get("tts_rate", out["tts_rate"])).strip() or out["tts_rate"]

    fav_in = incoming.get("favorites", out.get("favorites", []))
    if isinstance(fav_in, list):
        clean = []
        for x in fav_in:
            key = str(x).strip().lower()
            if key and key not in clean:
                clean.append(key)
        out["favorites"] = clean[:12]
    else:
        out["favorites"] = list(out.get("favorites", []))

    db = dict(out.get("dashboard", {}))
    db_in = incoming.get("dashboard", {}) if isinstance(incoming.get("dashboard", {}), dict) else {}
    db["live_title"] = str(db_in.get("live_title", db.get("live_title", "Live Video"))).strip() or "Live Video"
    db["devices_title"] = str(db_in.get("devices_title", db.get("devices_title", "Geräte"))).strip() or "Geräte"
    db["settings_title"] = str(db_in.get("settings_title", db.get("settings_title", "Einstellungen"))).strip() or "Einstellungen"
    db["events_title"] = str(db_in.get("events_title", db.get("events_title", "Protokoll"))).strip() or "Protokoll"
    out["dashboard"] = db

    return out


def save_settings(data: dict) -> None:
    SETTINGS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_settings() -> dict:
    if SETTINGS_PATH.exists():
        try:
            raw = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                merged = merge_settings(DEFAULT_SETTINGS, raw)
                return merged
        except Exception:
            pass
    merged = merge_settings(DEFAULT_SETTINGS, {})
    save_settings(merged)
    return merged


def load_users() -> dict:
    if USERS_PATH.exists():
        try:
            raw = json.loads(USERS_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return raw
        except Exception:
            pass

    default_user = os.getenv("APP_DEFAULT_USER", "admin")
    default_password = os.getenv("APP_DEFAULT_PASSWORD", "ringdong-please-change")
    data = {
        "users": {
            default_user: {
                "password_hash": generate_password_hash(default_password),
                "role": "admin",
            }
        }
    }
    USERS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


def save_users(data: dict) -> None:
    USERS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def user_exists(username: str) -> bool:
    users = load_users().get("users", {})
    return username in users


def verify_user(username: str, password: str) -> bool:
    users = load_users().get("users", {})
    row = users.get(username)
    if not row:
        return False
    return check_password_hash(str(row.get("password_hash", "")), password)


def user_role(username: str) -> str:
    users = load_users().get("users", {})
    row = users.get(username, {})
    return str(row.get("role", "user"))


def create_or_update_user(username: str, password: str, role: str = "user") -> None:
    with users_lock:
        data = load_users()
        users = data.setdefault("users", {})
        users[username] = {
            "password_hash": generate_password_hash(password),
            "role": role,
        }
        save_users(data)


def require_login(fn):
    @wraps(fn)
    def _wrapper(*args, **kwargs):
        if not session.get("user"):
            return jsonify({"ok": False, "error": "auth required"}), 401
        return fn(*args, **kwargs)

    return _wrapper


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64u_to_bytes(value: str) -> bytes:
    pad = "=" * ((4 - len(value) % 4) % 4)
    return base64.urlsafe_b64decode(value + pad)


def ensure_vapid_keys() -> dict:
    if VAPID_KEYS_PATH.exists():
        try:
            raw = json.loads(VAPID_KEYS_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and raw.get("public_key") and raw.get("private_key_pem"):
                return raw
        except Exception:
            pass

    private_key = ec.generate_private_key(ec.SECP256R1())
    public_key = private_key.public_key()
    pn = public_key.public_numbers()
    pub_bytes = b"\x04" + pn.x.to_bytes(32, "big") + pn.y.to_bytes(32, "big")

    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")

    data = {
        "public_key": _b64u(pub_bytes),
        "private_key_pem": private_pem,
    }
    VAPID_KEYS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


def load_push_subscriptions() -> dict:
    if PUSH_SUBS_PATH.exists():
        try:
            raw = json.loads(PUSH_SUBS_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return raw
        except Exception:
            pass
    data = {"subscriptions": {}}
    PUSH_SUBS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


def save_push_subscriptions(data: dict) -> None:
    PUSH_SUBS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def add_subscription(username: str, subscription: dict) -> None:
    endpoint = str(subscription.get("endpoint", "")).strip()
    if not endpoint:
        raise ValueError("missing endpoint")

    keys = subscription.get("keys", {})
    if not isinstance(keys, dict) or not keys.get("p256dh") or not keys.get("auth"):
        raise ValueError("invalid keys")

    with push_lock:
        db = load_push_subscriptions()
        users = db.setdefault("subscriptions", {})
        arr = users.setdefault(username, [])
        arr = [s for s in arr if s.get("endpoint") != endpoint]
        arr.append({"endpoint": endpoint, "keys": {"p256dh": keys.get("p256dh"), "auth": keys.get("auth")}})
        users[username] = arr
        save_push_subscriptions(db)


def remove_subscription(username: str, endpoint: str) -> None:
    with push_lock:
        db = load_push_subscriptions()
        users = db.setdefault("subscriptions", {})
        arr = users.get(username, [])
        users[username] = [s for s in arr if s.get("endpoint") != endpoint]
        save_push_subscriptions(db)


def send_push_to_user(username: str, title: str, body: str, url: str = "/") -> tuple[int, int]:
    db = load_push_subscriptions()
    subs = list(db.get("subscriptions", {}).get(username, []))
    if not subs:
        return (0, 0)

    ok_count = 0
    removed = 0
    payload = json.dumps({"title": title, "body": body, "url": url}, ensure_ascii=False)

    for sub in subs:
        try:
            webpush(
                subscription_info=sub,
                data=payload,
                vapid_private_key=vapid_keys.get("private_key_pem", ""),
                vapid_claims={"sub": VAPID_SUBJECT},
                ttl=60,
            )
            ok_count += 1
        except WebPushException as e:
            code = getattr(getattr(e, "response", None), "status_code", None)
            if code in (404, 410):
                endpoint = str(sub.get("endpoint", ""))
                if endpoint:
                    remove_subscription(username, endpoint)
                    removed += 1
        except Exception:
            pass

    return (ok_count, removed)


def fanout_push(title: str, body: str, url: str = "/") -> None:
    def _job():
        db = load_push_subscriptions()
        users = list(db.get("subscriptions", {}).keys())
        for username in users:
            send_push_to_user(username, title, body, url)

    threading.Thread(target=_job, daemon=True).start()


def list_announcements() -> list[dict]:
    rows = []
    for p in sorted(ANNOUNCE_DIR.glob("*"), key=lambda x: x.stat().st_mtime, reverse=True):
        if p.is_file() and p.suffix.lower() in ALLOWED_AUDIO_EXT:
            st = p.stat()
            rows.append(
                {
                    "name": p.name,
                    "url": public_announce_url(p.name),
                    "size": st.st_size,
                    "mtime": int(st.st_mtime),
                }
            )
    return rows


def build_index_html() -> None:
    files = sorted(VIDEO_DIR.glob("ring_*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
    rows = []
    for p in files[:MAX_INDEX_FILES]:
        mtime = datetime.fromtimestamp(p.stat().st_mtime).strftime("%d.%m.%Y %H:%M:%S")
        rows.append(f'<li><a href="/video/{p.name}" target="_blank" rel="noopener">{p.name}</a> <small>({mtime})</small></li>')

    html = f"""<!doctype html>
<html><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">
<title>Doorbell Recordings</title>
<style>body{{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#111;color:#eee;padding:16px}}a{{color:#8cc4ff}}</style>
</head><body>
<h2>Doorbell Recordings</h2>
<p><a href=\"/video/{LATEST_NAME}\" target=\"_blank\" rel=\"noopener\">▶ latest.mp4 öffnen</a></p>
<ul>{''.join(rows)}</ul>
</body></html>"""
    (VIDEO_DIR / "index.html").write_text(html, encoding="utf-8")


def record_clip(source: str) -> tuple[bool, str]:
    global last_record_ts
    cfg = runtime_settings

    with record_lock:
        now = time.time()
        cooldown = as_int(cfg.get("record_cooldown_sec", 20), 20, 0, 300)
        elapsed = now - last_record_ts
        if elapsed < cooldown:
            remaining = max(1, int(cooldown - elapsed))
            return False, f"cooldown:{remaining}"
        last_record_ts = now

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_name = f"ring_{ts}_{source}.mp4"
    out_path = VIDEO_DIR / out_name

    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-rtsp_transport",
        "tcp",
        "-t",
        str(as_int(cfg.get("record_duration_sec", 25), 25, 3, 120)),
        "-i",
        str(cfg.get("rtsp_stream_url", DEFAULT_RTSP_STREAM_URL)),
        "-c",
        "copy",
        str(out_path),
    ]

    try:
        subprocess.run(cmd, check=True)
        latest = VIDEO_DIR / LATEST_NAME
        shutil.copy2(out_path, latest)
        build_index_html()
        emit_event("clip", f"Aufnahme bereit ({source})", public_video_url(out_name))
        return True, out_name
    except Exception as e:
        emit_event("snapshot", f"Aufnahme fehlgeschlagen ({source}): {e}")
        return False, str(e)


def maybe_record_in_background(source: str) -> None:
    def _job():
        record_clip(source)

    t = threading.Thread(target=_job, daemon=True)
    t.start()


def synthesize_tts(text: str, out_dir: Path | None = None, prefix: str = "tts") -> tuple[bool, str]:
    cfg = runtime_settings
    clean = " ".join((text or "").split()).strip()
    if not clean:
        return False, "empty"
    clean = clean[:450]

    target_dir = out_dir or TTS_DIR
    target_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_name = f"{prefix}_{ts}.mp3"
    out_path = target_dir / out_name

    cmd = [
        "edge-tts",
        "--voice",
        str(cfg.get("tts_voice", DEFAULT_SETTINGS["tts_voice"])),
        "--rate",
        str(cfg.get("tts_rate", DEFAULT_SETTINGS["tts_rate"])),
        "--text",
        clean,
        "--write-media",
        str(out_path),
    ]
    try:
        subprocess.run(cmd, check=True)
        return True, out_name
    except Exception as e:
        return False, str(e)


def unlock_door() -> bool:
    topic = f"ring/{RING_LOCATION_ID}/intercom/{RING_INTERCOM_ID}/lock/command"
    payload = "UNLOCK"
    result = mqtt_publish.single(
        topic,
        payload=payload,
        hostname=MQTT_HOST,
        port=MQTT_PORT,
        auth={"username": MQTT_USER, "password": MQTT_PASSWORD} if MQTT_USER else None,
    )
    return result is None


# ---- MQTT subscriber --------------------------------------------------------
def on_connect(client, userdata, flags, rc, properties=None):
    client.subscribe("ring/+/camera/+/ding/state")
    client.subscribe("ring/+/camera/+/motion/state")
    client.subscribe("ring/+/intercom/+/lock/command")
    emit_event("snapshot", f"MQTT verbunden (rc={rc})")


def on_message(client, userdata, msg):
    topic = msg.topic
    payload = msg.payload.decode(errors="ignore").strip()

    latest_url = public_video_url(LATEST_NAME)

    if "/ding/" in topic and payload.lower() in ("ding", "on"):
        emit_event("ding", "Es klingelt", latest_url)
        if as_bool(runtime_settings.get("record_on_ding", True), True):
            maybe_record_in_background("ding")
        return

    if "/motion/" in topic and payload.upper() == "ON":
        emit_event("motion", "Bewegung erkannt", latest_url)
        if as_bool(runtime_settings.get("record_on_motion", True), True):
            maybe_record_in_background("motion")
        return

    if "/lock/command" in topic and payload.upper() in ("UNLOCK", "ON"):
        emit_event("unlock", "Tür geöffnet", latest_url)


def mqtt_loop() -> None:
    client = mqtt.Client(client_id="doorbell-app")
    if MQTT_USER:
        client.username_pw_set(MQTT_USER, MQTT_PASSWORD)
    client.on_connect = on_connect
    client.on_message = on_message

    while True:
        try:
            client.connect(MQTT_HOST, MQTT_PORT, 60)
            client.loop_forever()
        except Exception as e:
            emit_event("snapshot", f"MQTT getrennt: {e}")
            time.sleep(3)


# ---- HTTP routes ------------------------------------------------------------
@app.before_request
def auth_guard():
    path = request.path or "/"
    public_paths = {
        "/health",
        "/api/auth/login",
        "/api/auth/status",
        "/manifest.webmanifest",
        "/sw.js",
        "/favicon.ico",
        "/apple-touch-icon.png",
    }

    if path in public_paths or path.startswith("/assets/") or path.startswith("/icons/"):
        return None

    if path == "/login":
        if session.get("user"):
            return redirect("/")
        return None

    if session.get("user"):
        return None

    if path.startswith("/api/"):
        return jsonify({"ok": False, "error": "auth required"}), 401

    return redirect("/login")


@app.get("/login")
def login_page():
    return app.send_static_file("login.html")


@app.post("/api/auth/login")
def api_auth_login():
    payload = request.get_json(silent=True) or {}
    username = str(payload.get("username", "")).strip()
    password = str(payload.get("password", ""))

    if not username or not password:
        return jsonify({"ok": False, "error": "missing credentials"}), 400

    if not verify_user(username, password):
        return jsonify({"ok": False, "error": "invalid credentials"}), 401

    session["user"] = username
    session["role"] = user_role(username)
    emit_event("snapshot", f"Login: {username}")
    return jsonify({"ok": True, "user": username, "role": session.get("role")})


@app.post("/api/auth/logout")
@require_login
def api_auth_logout():
    user = session.get("user", "")
    session.clear()
    emit_event("snapshot", f"Logout: {user}")
    return jsonify({"ok": True})


@app.get("/api/auth/status")
def api_auth_status():
    return jsonify({
        "ok": True,
        "authenticated": bool(session.get("user")),
        "user": session.get("user"),
        "role": session.get("role"),
    })


@app.post("/api/auth/users")
@require_login
def api_auth_create_user():
    if session.get("role") != "admin":
        return jsonify({"ok": False, "error": "admin required"}), 403

    payload = request.get_json(silent=True) or {}
    username = str(payload.get("username", "")).strip()
    password = str(payload.get("password", ""))
    role = str(payload.get("role", "user")).strip() or "user"

    if len(username) < 3 or len(password) < 8:
        return jsonify({"ok": False, "error": "username>=3 and password>=8 required"}), 400

    create_or_update_user(username, password, role=role)
    return jsonify({"ok": True, "user": username, "role": role})


@app.get("/manifest.webmanifest")
def manifest_file():
    return app.send_static_file("manifest.webmanifest")


@app.get("/sw.js")
def service_worker():
    resp = app.send_static_file("sw.js")
    resp.headers["Service-Worker-Allowed"] = "/"
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/health")
def health():
    return jsonify({"status": "ok"})


@app.get("/")
def index():
    return app.send_static_file("index.html")


@app.get("/events")
def events():
    client_q = queue.Queue(maxsize=200)
    with sse_lock:
        sse_clients.append(client_q)

    def stream():
        for evt in list(recent_events)[:25][::-1]:
            yield f"event: {evt['type']}\ndata: {json.dumps(evt, ensure_ascii=False)}\n\n"

        try:
            while True:
                try:
                    evt = client_q.get(timeout=25)
                    yield f"event: {evt['type']}\ndata: {json.dumps(evt, ensure_ascii=False)}\n\n"
                except queue.Empty:
                    yield ": keepalive\n\n"
        finally:
            with sse_lock:
                if client_q in sse_clients:
                    sse_clients.remove(client_q)

    return Response(stream(), mimetype="text/event-stream", headers={"Cache-Control": "no-cache", "Access-Control-Allow-Origin": "*"})


@app.get("/api/settings")
def api_settings_get():
    return jsonify(runtime_settings)


@app.put("/api/settings")
def api_settings_put():
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "invalid json"}), 400

    with settings_lock:
        merged = merge_settings(runtime_settings, payload)
        runtime_settings.clear()
        runtime_settings.update(merged)
        save_settings(runtime_settings)

    emit_event("snapshot", "Einstellungen gespeichert")
    return jsonify({"ok": True, "settings": runtime_settings})


@app.post("/api/unlock")
def api_unlock():
    try:
        ok = unlock_door()
    except Exception as e:
        emit_event("snapshot", f"Unlock Fehler: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

    if ok:
        emit_event("unlock", "Tür öffnen gesendet", public_video_url(LATEST_NAME))
        return jsonify({"ok": True})
    return jsonify({"ok": False}), 500


@app.post("/api/record")
def api_record():
    source = request.json.get("source", "manual") if request.is_json else "manual"
    ok, info = record_clip(source)

    if not ok and info.startswith("cooldown:"):
        remaining = info.split(":", 1)[1]
        return jsonify({
            "ok": True,
            "cooldown": True,
            "info": f"Cooldown aktiv ({remaining}s)",
            "url": public_video_url(LATEST_NAME),
        })

    return jsonify({"ok": ok, "info": info})


@app.post("/api/tts")
def api_tts():
    text = ""
    if request.is_json:
        text = str(request.json.get("text", ""))
    else:
        text = str(request.form.get("text", ""))

    ok, info = synthesize_tts(text, out_dir=TTS_DIR, prefix="tts")
    if not ok:
        return jsonify({"ok": False, "error": info}), 400

    url = public_tts_url(info)
    emit_event("snapshot", f"TTS erzeugt: {text[:70]}", url)
    return jsonify({"ok": True, "url": url, "file": info})


@app.get("/api/announcements")
def api_announcements_list():
    return jsonify({"items": list_announcements()})


@app.post("/api/announcements/upload")
def api_announcements_upload():
    if "audio" not in request.files:
        return jsonify({"ok": False, "error": "missing file field 'audio'"}), 400

    file = request.files["audio"]
    raw_name = secure_filename(file.filename or "announcement.webm")
    ext = Path(raw_name).suffix.lower() or ".webm"
    if ext not in ALLOWED_AUDIO_EXT:
        return jsonify({"ok": False, "error": f"unsupported type {ext}"}), 400

    stem = Path(raw_name).stem or "announcement"
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_name = f"ann_{ts}_{stem[:32]}{ext}"
    out_path = ANNOUNCE_DIR / out_name
    file.save(out_path)

    emit_event("snapshot", "Ansage hochgeladen", public_announce_url(out_name))
    return jsonify({"ok": True, "name": out_name, "url": public_announce_url(out_name)})


@app.post("/api/announcements/tts")
def api_announcements_tts():
    payload = request.get_json(silent=True) or {}
    text = str(payload.get("text", "")).strip()
    if not text:
        return jsonify({"ok": False, "error": "empty text"}), 400

    ok, info = synthesize_tts(text, out_dir=ANNOUNCE_DIR, prefix="ann_tts")
    if not ok:
        return jsonify({"ok": False, "error": info}), 400

    url = public_announce_url(info)
    emit_event("snapshot", "Ansage per TTS erzeugt", url)
    return jsonify({"ok": True, "name": info, "url": url})


@app.post("/api/announcements/play")
def api_announcements_play():
    payload = request.get_json(silent=True) or {}
    name = secure_filename(str(payload.get("name", "")))
    if not name:
        return jsonify({"ok": False, "error": "missing name"}), 400

    path = ANNOUNCE_DIR / name
    if not path.exists() or not path.is_file():
        return jsonify({"ok": False, "error": "not found"}), 404

    url = public_announce_url(name)
    emit_event("snapshot", f"Ansage abgespielt: {name}", url)
    return jsonify({"ok": True, "url": url, "name": name})


@app.delete("/api/announcements/<path:name>")
def api_announcements_delete(name: str):
    safe = secure_filename(name)
    if not safe:
        return jsonify({"ok": False, "error": "invalid name"}), 400

    path = ANNOUNCE_DIR / safe
    if not path.exists() or not path.is_file():
        return jsonify({"ok": False, "error": "not found"}), 404

    path.unlink(missing_ok=True)
    emit_event("snapshot", f"Ansage gelöscht: {safe}")
    return jsonify({"ok": True})


@app.get("/api/push/public_key")
def api_push_public_key():
    return jsonify({
        "ok": True,
        "public_key": vapid_keys.get("public_key", ""),
        "hint": "iPhone Push funktioniert nur als installierte Web-App (Safari -> Zum Home-Bildschirm) und über HTTPS.",
    })


@app.post("/api/push/subscribe")
def api_push_subscribe():
    payload = request.get_json(silent=True) or {}
    sub = payload.get("subscription")
    if not isinstance(sub, dict):
        return jsonify({"ok": False, "error": "missing subscription"}), 400

    try:
        add_subscription(str(session.get("user", "")), sub)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.post("/api/push/unsubscribe")
def api_push_unsubscribe():
    payload = request.get_json(silent=True) or {}
    endpoint = str(payload.get("endpoint", "")).strip()
    if not endpoint:
        return jsonify({"ok": False, "error": "missing endpoint"}), 400

    remove_subscription(str(session.get("user", "")), endpoint)
    return jsonify({"ok": True})


@app.post("/api/push/test")
def api_push_test():
    user = str(session.get("user", ""))
    ok_count, removed = send_push_to_user(
        user,
        title="RingDong Test",
        body="Push ist aktiv. Wenn du das siehst, ist Safari-PWA bereit.",
        url="/",
    )
    return jsonify({"ok": True, "sent": ok_count, "removed": removed})


@app.get("/api/push/subscriptions")
def api_push_subscriptions():
    user = str(session.get("user", ""))
    db = load_push_subscriptions()
    items = db.get("subscriptions", {}).get(user, [])
    return jsonify({"ok": True, "count": len(items)})


@app.get("/api/config")
def api_config():
    cfg = runtime_settings
    return jsonify(
        {
            "stream": cfg.get("stream", DEFAULT_STREAM),
            "record_duration": cfg.get("record_duration_sec", 25),
            "record_cooldown": cfg.get("record_cooldown_sec", 20),
            "dashboard": cfg.get("dashboard", {}),
            "features": {
                "live_via_proxy": True,
                "tts": True,
                "unlock": True,
                "recording": True,
                "announcements": True,
                "settings": True,
                "auth": True,
                "push": True,
                "pwa": True,
            },
        }
    )


@app.post("/api/live/offer")
def api_live_offer():
    payload = request.get_json(silent=True) or {}
    sdp = payload.get("sdp")
    offer_type = payload.get("type", "offer")
    stream = payload.get("stream") or runtime_settings.get("stream", DEFAULT_STREAM)

    if not sdp:
        return jsonify({"ok": False, "error": "missing sdp"}), 400

    go2rtc_url = str(runtime_settings.get("go2rtc_url", DEFAULT_GO2RTC_URL)).rstrip("/")
    target_url = f"{go2rtc_url}/api/webrtc?src={urllib.parse.quote(str(stream), safe='')}"
    body = json.dumps({"type": offer_type, "sdp": sdp}).encode("utf-8")
    req = urllib.request.Request(
        target_url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )

    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            answer = json.loads(raw)
            return jsonify({"ok": True, "answer": answer})
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else str(e)
        emit_event("snapshot", f"Live-Proxy HTTPError {e.code}: {detail[:180]}")
        return jsonify({"ok": False, "error": f"go2rtc http {e.code}", "detail": detail[:500]}), 502
    except Exception as e:
        detail = f"{type(e).__name__}: {repr(e)}"
        emit_event("snapshot", f"Live-Proxy Fehler: {detail}")
        return jsonify({"ok": False, "error": detail}), 502


@app.get("/video/<path:name>")
def video_file(name: str):
    return send_from_directory(VIDEO_DIR, name)


@app.get("/tts/<path:name>")
def tts_file(name: str):
    return send_from_directory(TTS_DIR, name)


@app.get("/announce/<path:name>")
def announce_file(name: str):
    return send_from_directory(ANNOUNCE_DIR, name)


@app.get("/recordings")
def recordings_index():
    idx = VIDEO_DIR / "index.html"
    if not idx.exists():
        build_index_html()
    return send_from_directory(VIDEO_DIR, "index.html")


# ---- Runtime bootstrap ------------------------------------------------------
_runtime_started = False


def start_runtime_once():
    global _runtime_started
    if _runtime_started:
        return

    _runtime_started = True

    loaded = load_settings()
    runtime_settings.clear()
    runtime_settings.update(loaded)

    _ = load_users()
    _ = load_push_subscriptions()
    vapid_keys.clear()
    vapid_keys.update(ensure_vapid_keys())

    build_index_html()
    threading.Thread(target=mqtt_loop, daemon=True).start()


start_runtime_once()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8088)
