"""
Remote Management Console — LOCAL APP (run this on EACH PC, same network).

LAN-DIRECT model (no relay, no tunnel, no cloud):
  * This app binds to 0.0.0.0:8000 so other PCs on the SAME network can reach it.
  * It serves the web console on http://localhost:8000/ and ALSO embeds the
    little WebSocket router that used to live in relay.py — but just for THIS
    one machine (its own agent).
  * This PC's own agent connects to it over loopback (ws://127.0.0.1:8000/ws/agent)
    and registers with a fresh session password.
  * To control a partner you enter the partner's IP ADDRESS + their password.
    Your browser connects directly to  ws://<partner-ip>:8000/ws/dashboard .

So there is no shared server: every PC is both controllable (share your IP +
password) and a controller (enter a partner's IP + password).

Run:
    python local_app.py
Then open http://localhost:8000/ and read your IP + password on the page.
"""

import asyncio
import json
import os
import socket
import sys
import threading
from typing import Set

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
AGENT_DIR = os.path.join(BASE_DIR, "..", "agent")

# Reuse the Agent class from ../agent/agent.py
sys.path.insert(0, AGENT_DIR)
import agent as agent_module  # noqa: E402

APP_PORT = int(os.environ.get("PORT", "8000"))
# THIS machine's agent connects to THIS app over loopback.
AGENT_WS = f"ws://127.0.0.1:{APP_PORT}/ws/agent"


def lan_ip() -> str:
    """Best-effort primary LAN IPv4 of this machine (for partners to dial)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # No packet is actually sent; this just selects the outbound interface.
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:  # noqa: BLE001
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:  # noqa: BLE001
            return "127.0.0.1"
    finally:
        s.close()


# One agent instance for THIS machine; runs in a background thread and connects
# back to our own /ws/agent endpoint over loopback.
_agent = agent_module.Agent(AGENT_WS, "")


def _run_agent() -> None:
    asyncio.run(_agent.run())


threading.Thread(target=_run_agent, daemon=True).start()

local_app = FastAPI(title="Remote Management — Local App (LAN)")


# --------------------------------------------------------------------------- #
#  Embedded single-host router (formerly relay.py), scoped to THIS machine.    #
#  Exactly one local agent + any number of attached dashboards (usually one).  #
# --------------------------------------------------------------------------- #
class Hub:
    def __init__(self) -> None:
        self.agent_ws: WebSocket | None = None
        self.agent_password: str = ""
        self.agent_meta: dict = {}
        self.dashboards: Set[WebSocket] = set()

    async def broadcast(self, message: dict) -> None:
        dead = []
        for d in list(self.dashboards):
            try:
                await d.send_text(json.dumps(message))
            except Exception:  # noqa: BLE001
                dead.append(d)
        for d in dead:
            self.dashboards.discard(d)


hub = Hub()


@local_app.middleware("http")
async def no_cache(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return resp


@local_app.get("/api/my-device")
async def my_device() -> JSONResponse:
    """THIS machine's own address + credentials (what a partner types in)."""
    return JSONResponse({
        "address": lan_ip(),
        "device_id": _agent.device_id,
        "password": _agent.password,
        "online": hub.agent_ws is not None,
        "hostname": socket.gethostname(),
        "port": APP_PORT,
    })


@local_app.get("/api/config")
async def config() -> JSONResponse:
    """How the dashboard builds a direct WebSocket URL to a partner's IP."""
    return JSONResponse({"port": APP_PORT, "path": "/ws/dashboard"})


@local_app.get("/")
async def index() -> FileResponse:
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@local_app.websocket("/ws/agent")
async def ws_agent(ws: WebSocket) -> None:
    """THIS machine's own agent registers here (over loopback)."""
    await ws.accept()
    try:
        first = json.loads(await ws.receive_text())
        if first.get("type") != "register":
            await ws.close(code=4001)
            return

        hub.agent_ws = ws
        hub.agent_password = str(first.get("password", ""))
        hub.agent_meta = {
            "device_id": first.get("device_id"),
            "hostname": first.get("hostname"),
            "platform": first.get("platform"),
            "username": first.get("username"),
            "virtual": bool(first.get("virtual")),
        }
        print(f"[+] local agent online: {first.get('hostname')} ({first.get('device_id')})")

        # Forward everything the agent emits to every attached dashboard.
        while True:
            msg = json.loads(await ws.receive_text())
            await hub.broadcast(msg)

    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001
        print(f"[!] agent error: {exc}")
    finally:
        if hub.agent_ws is ws:
            hub.agent_ws = None
            await hub.broadcast({"type": "device_offline"})
            print("[-] local agent offline")


@local_app.websocket("/ws/dashboard")
async def ws_dashboard(ws: WebSocket) -> None:
    """A dashboard (yours or a partner's, over the LAN) attaches here."""
    await ws.accept()
    try:
        first = json.loads(await ws.receive_text())
        if first.get("type") != "connect":
            await ws.send_text(json.dumps(
                {"type": "connect_result", "ok": False, "error": "Bad request."}))
            await ws.close(code=4001)
            return

        password = str(first.get("password", ""))

        if hub.agent_ws is None:
            await ws.send_text(json.dumps(
                {"type": "connect_result", "ok": False,
                 "error": "This PC's agent is not running yet — try again in a moment."}))
            await ws.close()
            return
        if hub.agent_password != password:
            await ws.send_text(json.dumps(
                {"type": "connect_result", "ok": False, "error": "Wrong password."}))
            await ws.close()
            return

        hub.dashboards.add(ws)
        await ws.send_text(json.dumps({
            "type": "connect_result", "ok": True,
            "device": {**hub.agent_meta, "address": lan_ip()},
        }))
        print("[>] dashboard attached")

        while True:
            msg = json.loads(await ws.receive_text())
            if msg.get("type") == "command":
                if hub.agent_ws is not None:
                    await hub.agent_ws.send_text(json.dumps({
                        "type": "command",
                        "action": msg.get("action"),
                        "params": msg.get("params", {}),
                    }))
                else:
                    await ws.send_text(json.dumps({"type": "device_offline"}))

    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001
        print(f"[!] dashboard error: {exc}")
    finally:
        hub.dashboards.discard(ws)


local_app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


if __name__ == "__main__":
    import uvicorn
    ip = lan_ip()
    print("=" * 60)
    print("  REMOTE MANAGEMENT — LOCAL APP (LAN direct)")
    print(f"  This PC IP : {ip}")
    print(f"  Password   : {_agent.password}")
    print(f"  Open       : http://localhost:{APP_PORT}/")
    print(f"  Partners on the SAME network connect to: {ip} + the password")
    print("=" * 60)
    # host=0.0.0.0 so other PCs on the LAN can reach this machine.
    uvicorn.run(local_app, host="0.0.0.0", port=APP_PORT,
                ws_max_size=64 * 1024 * 1024)
