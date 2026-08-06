# Remote Management Console

A client–server **remote administration tool** for a Computer Networks (MMT)
project. A central server relays commands between a web dashboard and one or
more *agents* running on managed PCs.

Features per managed PC:

| Feature            | Implementation                          |
|--------------------|-----------------------------------------|
| App / process list | `psutil` (list + kill by PID)           |
| Live screen        | `mss` + `Pillow` → JPEG stream          |
| Live webcam        | `OpenCV` → JPEG stream                  |
| Keylogger          | `pynput` keystroke capture              |
| Remote control     | `pynput` mouse + keyboard (interactive screen) |
| Shutdown / Restart | `shutdown` command (Windows & Linux)    |

Transport is **WebSocket** (JSON messages), so it works over LAN or the
internet and passes through most NAT (the agent dials *out* to the server).

**Access model (UltraViewer / TeamViewer style):** every agent generates its
own **Device ID** (stable 9-digit number) and a **Password** (random, changes
each restart) and prints them. A dashboard must enter a device's ID + password
to attach to it — there is no shared token and no global list of machines, so
you can only reach a device whose credentials you were given.

---

## ⚠️ Authorized use only

This is a real remote-monitoring tool. Screen, webcam, and keystroke capture
are powerful and, used without consent, illegal in most jurisdictions.

- Run the **agent only on machines you own or are explicitly authorized to
  manage** (your own lab PCs, a course demo between your two machines, etc.).
- The agent is intentionally **transparent**: it prints a banner, runs in a
  visible console window, and has no stealth, persistence, or evasion. Keep it
  that way.
- Use a strong `RMC_TOKEN` and, for anything beyond localhost, put the server
  behind TLS (e.g. a reverse proxy) so the token and streams are encrypted.

---

## Architecture

```
  Browser dashboard ──WS──►  Server (FastAPI relay)  ──WS──►  Agent (managed PC)
   /ws/dashboard   ◄──WS──   routes by agent_id       ◄──WS──   /ws/agent
```

- **server/** — FastAPI relay + static web dashboard. Holds no data; it just
  authenticates and forwards JSON.
- **agent/** — Python program that runs on each managed PC.

## Setup

```bash
pip install -r requirements.txt
```

> Windows note: `opencv-python` and `pynput` install as normal wheels. The
> webcam feature needs a camera; screen capture works headless-free.

## Run

**1. Start the server** (on the controller machine):

```bash
cd server
set RMC_TOKEN=my-strong-token
python -m uvicorn server:server_app --host 0.0.0.0 --port 8000
```

Open the dashboard at <http://localhost:8000/> and enter the token.

**2. Start an agent** (on each managed PC):

```bash
cd agent
set RMC_SERVER=ws://SERVER_IP:8000/ws/agent
set RMC_TOKEN=my-strong-token
python agent.py
```

(PowerShell uses `$env:RMC_TOKEN="..."` instead of `set`.)

The PC appears in the dashboard sidebar. Click it, then use the tabs.

## Protocol (JSON over WebSocket)

Agent → server on connect:
```json
{"type":"register","token":"...","agent_id":"host-mac","hostname":"...","platform":"...","username":"..."}
```

Dashboard → server:
```json
{"type":"command","agent_id":"host-mac","action":"screen_start","params":{}}
```

Agent → server → dashboard (data):
```json
{"type":"data","agent_id":"host-mac","channel":"screen","payload":{"image":"<base64 jpeg>"}}
```

Actions: `list_processes`, `kill_process` (`{pid}`), `screen_start`/`screen_stop`,
`webcam_start`/`webcam_stop`, `keylog_start`/`keylog_stop`, `shutdown`, `restart`,
and remote control — `mouse_move` (`{x,y}` as 0–1 fractions), `mouse_click`
(`{x,y,button,double}`), `mouse_scroll` (`{x,y,dy}`), `key_press` (`{key}`).

Data channels: `processes`, `screen`, `webcam`, `keylog`, `power`, `error`.

## Project layout

```
MMT/
├── requirements.txt
├── README.md
├── server/
│   ├── server.py            # FastAPI relay + dashboard host
│   └── static/
│       ├── index.html       # dashboard UI
│       ├── style.css
│       └── app.js
└── agent/
    └── agent.py             # runs on the managed PC
```

## Possible extensions

- File browser / upload-download
- Audio streaming
- Multi-monitor selection for screen capture
- TLS + per-agent tokens instead of one shared token
- Remote mouse/keyboard control (turn the screen view interactive)
