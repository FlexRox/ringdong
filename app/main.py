import json
import os
import queue
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from datetime import datetime
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory
import paho.mqtt.client as mqtt
import paho.mqtt.publish as mqtt_publish

app = Flask(__name__, static_folder="static", static_url_path="")

# ---- Config via ENV --------------------------------------------------------
MQTT_HOST = os.getenv("MQTT_HOST", "192.168.10.76")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_USER = os.getenv("MQTT_USER", "")
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD", "")

RING_LOCATION_ID = os.getenv("RING_LOCATION_ID", "558160f8-03ae-48da-bd30-8999e38d0269")
RING_CAMERA_ID = os.getenv("RING_CAMERA_ID", "343ea4dd1515")
RING_INTERCOM_ID = os.getenv("RING_INTERCOM_ID", "343ea4b1121d")

RTSP_STREAM_URL = os.getenv("RTSP_STREAM_URL", f"rtsp://192.168.10.76:8554/{RING_CAMERA_ID}_live")
GO2RTC_URL = os.getenv("GO2RTC_URL", "http://192.168.10.76:1985")

RECORD_DURATION_SEC = int(os.getenv("RECORD_DURATION_SEC", "25"))
RECORD_COOLDOWN_SEC = int(os.getenv("RECORD_COOLDOWN_SEC", "20"))
RECORD_ON_DING = os.getenv("RECORD_ON_DING", "true").lower() == "true"
RECORD_ON_MOTION = os.getenv("RECORD_ON_MOTION", "true").lower() == "true"

VIDEO_DIR = Path(os.getenv("VIDEO_DIR", "/data/video"))
TTS_DIR = Path(os.getenv("TTS_DIR", "/data/tts"))
TTS_VOICE = os.getenv("TTS_VOICE", "de-DE-SeraphinaMultilingualNeural")
TTS_RATE = os.getenv("TTS_RATE", "+0%")
LATEST_NAME = "latest.mp4"
MAX_INDEX_FILES = int(os.getenv("MAX_INDEX_FILES", "300"))

# ---- Runtime state ---------------------------------------------------------
sse_clients = []
sse_lock = threading.Lock()
recent_events = deque(maxlen=50)
record_lock = threading.Lock()
last_record_ts = 0.0

VIDEO_DIR.mkdir(parents=True, exist_ok=True)
TTS_DIR.mkdir(parents=True, exist_ok=True)


def now_hms() -> str:
    return datetime.now().strftime("%H:%M:%S")


def public_video_url(name: str) -> str:
    return f"/video/{name}"


def public_tts_url(name: str) -> str:
    return f"/tts/{name}"


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
    with record_lock:
        now = time.time()
        elapsed = now - last_record_ts
        if elapsed < RECORD_COOLDOWN_SEC:
            remaining = max(1, int(RECORD_COOLDOWN_SEC - elapsed))
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
        str(RECORD_DURATION_SEC),
        "-i",
        RTSP_STREAM_URL,
        "-c",
        "copy",
        str(out_path),
    ]

    try:
        subprocess.run(cmd, check=True)
        latest = VIDEO_DIR / LATEST_NAME
        shutil.copy2(out_path, latest)
        build_index_html()
        emit_event("clip", f"🎬 Aufnahme bereit ({source})", public_video_url(out_name))
        return True, out_name
    except Exception as e:
        emit_event("snapshot", f"❌ Aufnahme fehlgeschlagen ({source}): {e}")
        return False, str(e)


def maybe_record_in_background(source: str) -> None:
    def _job():
        record_clip(source)

    t = threading.Thread(target=_job, daemon=True)
    t.start()


def synthesize_tts(text: str) -> tuple[bool, str]:
    clean = " ".join((text or "").split()).strip()
    if not clean:
        return False, "empty"
    clean = clean[:350]

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_name = f"tts_{ts}.mp3"
    out_path = TTS_DIR / out_name

    cmd = [
        "edge-tts",
        "--voice", TTS_VOICE,
        "--rate", TTS_RATE,
        "--text", clean,
        "--write-media", str(out_path),
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


# ---- MQTT subscriber -------------------------------------------------------
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
        emit_event("ding", "🔔 Es klingelt!", latest_url)
        if RECORD_ON_DING:
            maybe_record_in_background("ding")
        return

    if "/motion/" in topic and payload.upper() == "ON":
        emit_event("motion", "🚶 Bewegung erkannt", latest_url)
        if RECORD_ON_MOTION:
            maybe_record_in_background("motion")
        return

    if "/lock/command" in topic and payload.upper() in ("UNLOCK", "ON"):
        emit_event("unlock", "🔓 Tür geöffnet", latest_url)


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


# ---- HTTP routes -----------------------------------------------------------
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
        # backlog on connect
        for evt in list(recent_events)[:20][::-1]:
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


@app.post("/api/unlock")
def api_unlock():
    try:
        ok = unlock_door()
    except Exception as e:
        emit_event("snapshot", f"❌ Unlock Fehler: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

    if ok:
        emit_event("unlock", "🔓 Tür geöffnet", public_video_url(LATEST_NAME))
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

    ok, info = synthesize_tts(text)
    if not ok:
        return jsonify({"ok": False, "error": info}), 400

    url = public_tts_url(info)
    emit_event("snapshot", f"🔊 TTS erzeugt: {text[:70]}", url)
    return jsonify({"ok": True, "url": url, "file": info})


@app.get("/api/config")
def api_config():
    return jsonify(
        {
            "stream": f"{RING_CAMERA_ID}_live",
            "record_duration": RECORD_DURATION_SEC,
            "features": {
                "live_via_proxy": True,
                "tts": True,
                "unlock": True,
                "recording": True,
            },
        }
    )


@app.post("/api/live/offer")
def api_live_offer():
    payload = request.get_json(silent=True) or {}
    sdp = payload.get("sdp")
    offer_type = payload.get("type", "offer")
    stream = payload.get("stream") or f"{RING_CAMERA_ID}_live"

    if not sdp:
        return jsonify({"ok": False, "error": "missing sdp"}), 400

    target_url = f"{GO2RTC_URL}/api/webrtc?src={urllib.parse.quote(stream, safe='')}"
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
        emit_event("snapshot", f"❌ Live-Proxy HTTPError {e.code}: {detail[:160]}")
        return jsonify({"ok": False, "error": f"go2rtc http {e.code}", "detail": detail[:500]}), 502
    except Exception as e:
        detail = f"{type(e).__name__}: {repr(e)}"
        emit_event("snapshot", f"❌ Live-Proxy Fehler: {detail}")
        return jsonify({"ok": False, "error": detail}), 502


@app.get("/video/<path:name>")
def video_file(name: str):
    return send_from_directory(VIDEO_DIR, name)


@app.get("/tts/<path:name>")
def tts_file(name: str):
    return send_from_directory(TTS_DIR, name)


@app.get("/recordings")
def recordings_index():
    idx = VIDEO_DIR / "index.html"
    if not idx.exists():
        build_index_html()
    return send_from_directory(VIDEO_DIR, "index.html")


# ---- Runtime bootstrap -----------------------------------------------------
_runtime_started = False


def start_runtime_once():
    global _runtime_started
    if _runtime_started:
        return
    _runtime_started = True
    build_index_html()
    threading.Thread(target=mqtt_loop, daemon=True).start()


start_runtime_once()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8088)
