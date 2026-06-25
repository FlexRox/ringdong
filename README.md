<p align="center">
  <img src="assets/logo-ringdong.svg" alt="RingDong Logo" width="860" />
</p>

<h1 align="center">RingDong</h1>

<p align="center">
  <b>Standalone Doorbell Web App (HA + Ring-MQTT stack)</b><br/>
  Live View, Unlock, Push, Recording, Event Feed.
</p>

## Status (Final)

Dieses Repository ist der finale Stand der App.

- Push-Benachrichtigungen: ✅
- Unlock über Intercom-MQTT: ✅
- Live-View (Doorbell): ✅
- Browser-Talkback: ⚠️ abhängig vom Ring/go2rtc-Rückkanal

## Stack

- Backend: Flask + Gunicorn
- Messaging: MQTT (paho-mqtt)
- Live: WebRTC Offer Proxy zu go2rtc
- Push: Web Push (VAPID) + Service Worker
- TTS: edge-tts
- Runtime: Docker Compose

## Projektstruktur

- `app/main.py` – API, MQTT-Bridge, Recordings, Push, Auth, TTS
- `app/static/index.html` – UI (Apple-Health-Style, Live-Modal)
- `app/static/sw.js` – Service Worker für Push
- `docker-compose.yml` – Runtime-Konfiguration
- `.env.example` – Konfig-Vorlage

## Setup

```bash
cp .env.example .env
# .env ausfüllen

docker compose up -d --build
```

App: `http://<HOST>:8088`

## Health & Smoke Tests

```bash
curl -s http://<HOST>:8088/health
curl -s -X POST http://<HOST>:8088/api/record -H 'Content-Type: application/json' -d '{"source":"manual"}'
curl -s -X POST http://<HOST>:8088/api/unlock
```

## Home Assistant (iframe)

```yaml
type: iframe
url: https://<DEINE-URL-ODER-IP>:8088
aspect_ratio: 56%
```

## Betriebshinweise

- iOS Push + Mic sind am stabilsten über HTTPS + Home-Screen-App.
- Wenn Ring-Stream nur `recvonly` liefert, ist Browser-Talkback technisch nicht erzwingbar.
- In diesem Fall App-UI nutzt Fallback „Sprechen (Ring App)“.

## Decommission / Entfernen

Wenn du die App komplett entfernen willst:

```bash
# im Projektordner
docker compose down --remove-orphans

# optional Images entfernen
docker image rm ring-doorbell-app-doorbell-app:latest || true
```

Danach Projektordner löschen.

## Lizenz

Private/internal usage.