# ringdong

Open Source Ring App with Home Assistant Integration.

## Ring Doorbell App (Standalone)

Sauberes, eigenständiges Container-Projekt für:

- Live-View (WebRTC über go2rtc)
- Tür öffnen (MQTT publish `UNLOCK`)
- Talk-to-Speech (Browser TTS)
- Event-Feed (SSE)
- Aufzeichnungen (ffmpeg RTSP clip) + klickbare Recording-Links

Kurz: Das Ding, das man *eigentlich* von Anfang an bauen wollte, statt YAML-Tape-Art in drei Containern.

## Architektur

- `app/main.py`:
  - MQTT Subscriber (`ding`, `motion`, `unlock`)
  - SSE endpoint `/events`
  - REST endpoints:
    - `POST /api/unlock`
    - `POST /api/record`
    - `GET /api/config`
  - Recording pipeline via ffmpeg nach `/data/video`
  - Recording index `/recordings` und `/video/<file>`
- `app/static/index.html`:
  - Live, Unlock, Talk-Hinweis, TTS, Eventliste mit Link auf Recording

## Voraussetzungen

- Ring-MQTT / MQTT Broker erreichbar
- RTSP Stream verfügbar (z. B. `rtsp://<host>:8554/<camera>_live`)
- go2rtc API erreichbar (z. B. `http://<host>:1985`)
- Docker + Docker Compose

## Start

```bash
cp .env.example .env
# .env anpassen

docker compose up -d --build
```

App: `http://<host>:8088`

Health:

```bash
curl -s http://<host>:8088/health
```

## API Kurztest

```bash
# manuelle Aufnahme
curl -s -X POST http://<host>:8088/api/record -H 'Content-Type: application/json' -d '{"source":"manual"}'

# Tür öffnen
curl -s -X POST http://<host>:8088/api/unlock

# SSE stream
curl -N http://<host>:8088/events
```

## HA Einbindung (optional)

```yaml
type: iframe
url: http://192.168.10.76:8088
aspect_ratio: 56%
```
