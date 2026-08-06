"""
Remote Management Console — LAN launcher (run this on EACH PC).

LAN-direct model: no relay, no tunnel, no cloud. This just:
  1. Frees port 8000 if a stale copy is stuck.
  2. Starts local_app.py bound to 0.0.0.0:8000 (reachable by PCs on the same
     network) — it serves the console AND the direct control endpoint.
  3. Opens http://localhost:8000/ and prints THIS PC's LAN IP + how to connect.

To control a partner: open the console, enter the partner's IP + password.
Both PCs must be on the SAME network (same Wi-Fi / router / LAN).

Stop with Ctrl+C or just close the window.
"""

import os
import socket
import subprocess
import sys
import time
import webbrowser

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
APP_PORT = int(os.environ.get("PORT", "8000"))


def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:  # noqa: BLE001
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:  # noqa: BLE001
            return "127.0.0.1"
    finally:
        s.close()


def free_port(port: int) -> None:
    """Kill any process currently LISTENING on `port` (leftover from a crash)."""
    try:
        import psutil
    except ImportError:
        return
    for conn in psutil.net_connections(kind="inet"):
        if (conn.laddr and conn.laddr.port == port
                and conn.status == psutil.CONN_LISTEN and conn.pid):
            try:
                psutil.Process(conn.pid).kill()
                print(f"  freed port {port} (killed stale PID {conn.pid})")
            except Exception:  # noqa: BLE001
                pass


def wait_for_port(port: int, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.3)
    return False


def main() -> None:
    ip = lan_ip()
    print("=" * 60)
    print("  REMOTE MANAGEMENT — starting (LAN direct)")
    print("=" * 60)

    print("[1/2] Freeing stale port ...")
    free_port(APP_PORT)

    print(f"[2/2] Starting local app on 0.0.0.0:{APP_PORT} ...")
    env = dict(os.environ, PORT=str(APP_PORT))
    proc = subprocess.Popen(
        [sys.executable, os.path.join(BASE_DIR, "local_app.py")],
        cwd=BASE_DIR, env=env,
    )

    if wait_for_port(APP_PORT):
        try:
            webbrowser.open(f"http://localhost:{APP_PORT}/")
        except Exception:  # noqa: BLE001
            pass

    print("")
    print("=" * 60)
    print("  READY")
    print(f"  Your console : http://localhost:{APP_PORT}/")
    print(f"  This PC's IP : {ip}   (give this + your password to a partner)")
    print(f"  Control someone: open the console, enter THEIR IP + password.")
    print("  Both PCs must be on the SAME network. Close this window to stop.")
    print("=" * 60)

    try:
        proc.wait()
    except KeyboardInterrupt:
        pass
    finally:
        if proc.poll() is None:
            proc.terminate()


if __name__ == "__main__":
    main()
