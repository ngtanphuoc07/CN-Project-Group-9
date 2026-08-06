"""
Remote Management Console — Agent (runs on the MANAGED PC).

This program connects out to the server over a WebSocket, registers itself,
and then executes commands relayed from the control dashboard:

    list_processes / kill_process
    screen_start   / screen_stop      (live screen streaming)
    webcam_start   / webcam_stop      (live webcam streaming)
    keylog_start   / keylog_stop      (keystroke capture)
    mouse_move / mouse_click / mouse_scroll / key_press  (remote control)
    shutdown       / restart

TRANSPARENCY / AUTHORIZED USE
-----------------------------
This agent is meant for machines you OWN or are AUTHORIZED to manage
(lab management, a Computer Networks course project, remote support).
It prints a visible banner on start and does NOT hide itself, install
persistence, or evade detection. Do not deploy it covertly.

Run:
    pip install -r requirements.txt
    set RMC_SERVER=ws://<server-ip>:8000/ws/agent
    set RMC_TOKEN=your-secret
    python agent.py
"""

import asyncio
import base64
import getpass
import hashlib
import io
import json
import os
import platform
import queue
import random
import socket
import string
import threading
import time
import uuid

import cv2
import mss
import psutil
import websockets
from PIL import Image
from pynput import keyboard, mouse
from pynput.keyboard import Controller as KeyboardController, Key
from pynput.mouse import Button, Controller as MouseController


def make_device_id() -> str:
    """A stable 9-digit ID derived from this machine (same across restarts)."""
    raw = f"{socket.gethostname()}-{uuid.getnode()}"
    digest = int(hashlib.sha256(raw.encode()).hexdigest(), 16)
    return str(digest % 900_000_000 + 100_000_000)


def make_password() -> str:
    """A fresh 6-character session password (changes each time the agent starts)."""
    alphabet = string.ascii_lowercase + string.digits
    return "".join(random.choices(alphabet, k=6))


class Agent:
    def __init__(self, server_url: str, token: str) -> None:
        self.server_url = server_url
        self.token = token
        # Allow a fixed identity via env (useful to simulate a "partner" PC
        # on the same machine for testing). Otherwise auto-generate.
        self.device_id = os.environ.get("RMC_DEVICE_ID") or make_device_id()
        self.password = os.environ.get("RMC_PASSWORD") or make_password()
        # A "virtual" account is a demo identity (e.g. for solo testing). The
        # dashboard shows a notice that it can be viewed but not truly controlled.
        self.virtual = bool(os.environ.get("RMC_VIRTUAL"))
        # Discrete one-off messages (processes, keylog, power, error).
        self.outbox: "queue.Queue[dict]" = queue.Queue()
        # Latest frame per live-stream channel. Newer frames overwrite older
        # ones so latency never accumulates when the network is slower than
        # capture (this is what keeps the screen/webcam feeling "live").
        self.latest: dict[str, dict] = {}
        self.latest_lock = threading.Lock()

        # streaming switches, toggled by dashboard commands
        self.screen_on = threading.Event()
        self.webcam_on = threading.Event()
        self.keylog_on = threading.Event()

        self._keybuf: list[str] = []
        self._keylock = threading.Lock()
        self._icon_cache: dict[str, str] = {}  # exe path -> base64 PNG (or "")
        self._proc_cache: dict = {}  # pid -> psutil.Process, kept so CPU% needs no sleep
        self._exe_cache: dict = {}   # pid -> exe path (slow to fetch, cached per pid)
        # locks so the heavy process scans run in a background thread without
        # blocking the event loop (keeps streaming smooth) and never overlap
        self._apps_lock = threading.Lock()
        self._procs_lock = threading.Lock()

        # remote-control (mouse + keyboard) actuators
        self.mouse = MouseController()
        self.keyboard = KeyboardController()
        self._screen_geom = None  # (left, top, width, height) of primary monitor

    # Map dashboard key names (JS KeyboardEvent.key) -> pynput special keys.
    SPECIAL_KEYS = {
        "Enter": Key.enter, "Backspace": Key.backspace, "Tab": Key.tab,
        "Escape": Key.esc, "Delete": Key.delete, " ": Key.space,
        "ArrowUp": Key.up, "ArrowDown": Key.down, "ArrowLeft": Key.left,
        "ArrowRight": Key.right, "Home": Key.home, "End": Key.end,
        "PageUp": Key.page_up, "PageDown": Key.page_down,
        "CapsLock": Key.caps_lock, "Shift": Key.shift, "Control": Key.ctrl,
        "Alt": Key.alt, "Meta": Key.cmd,
    }

    # ------------------------------------------------------------------ #
    #  Outgoing helpers                                                  #
    # ------------------------------------------------------------------ #
    def enqueue(self, channel: str, payload: dict) -> None:
        self.outbox.put({"type": "data", "channel": channel, "payload": payload})

    def enqueue_stream(self, channel: str, payload: dict) -> None:
        # Only the freshest frame per channel is kept.
        with self.latest_lock:
            self.latest[channel] = payload

    # ------------------------------------------------------------------ #
    #  Feature: process list                                             #
    # ------------------------------------------------------------------ #
    def _extract_icon(self, exe_path: str, size: int = 32):
        """Return a base64 PNG of the exe's icon (cached). '' if unavailable."""
        if not exe_path:
            return ""
        if exe_path in self._icon_cache:
            return self._icon_cache[exe_path]
        b64 = ""
        try:
            import win32con
            import win32gui
            import win32ui

            large, small = win32gui.ExtractIconEx(exe_path, 0)
            handles = list(large) + list(small)
            if handles:
                hicon = handles[0]
                screen = win32gui.GetDC(0)
                hdc = win32ui.CreateDCFromHandle(screen)
                hbmp = win32ui.CreateBitmap()
                hbmp.CreateCompatibleBitmap(hdc, size, size)
                mem = hdc.CreateCompatibleDC()
                mem.SelectObject(hbmp)
                win32gui.DrawIconEx(mem.GetSafeHdc(), 0, 0, hicon, size, size, 0, None, win32con.DI_NORMAL)
                info = hbmp.GetInfo()
                bits = hbmp.GetBitmapBits(True)
                img = Image.frombuffer("RGBA", (info["bmWidth"], info["bmHeight"]),
                                       bits, "raw", "BGRA", 0, 1)
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                b64 = base64.b64encode(buf.getvalue()).decode()
                win32gui.DeleteObject(hbmp.GetHandle())
                mem.DeleteDC()
                hdc.DeleteDC()
                win32gui.ReleaseDC(0, screen)
            for h in handles:
                try:
                    win32gui.DestroyIcon(h)
                except Exception:
                    pass
        except Exception:
            b64 = ""
        self._icon_cache[exe_path] = b64
        return b64

    def list_processes(self):
        procs = []
        for p in psutil.process_iter(["pid", "name", "username", "memory_percent", "exe"]):
            try:
                info = p.info
                procs.append({
                    "pid": info["pid"],
                    "name": info.get("name") or "?",
                    "username": info.get("username") or "",
                    "mem": round(info.get("memory_percent") or 0.0, 1),
                    "exe": info.get("exe") or "",
                })
            except Exception:
                continue
        procs.sort(key=lambda x: x["mem"], reverse=True)
        procs = procs[:250]

        # Extract an icon per UNIQUE exe (cached across refreshes).
        icons = {}
        for pr in procs:
            exe = pr["exe"]
            if exe and exe not in icons:
                ic = self._extract_icon(exe)
                if ic:
                    icons[exe] = ic
        return procs, icons

    def kill_process(self, pid) -> None:
        try:
            p = psutil.Process(int(pid))
            p.terminate()
            try:
                p.wait(timeout=1.5)   # make sure it's gone before we recount
            except Exception:
                pass
        except Exception as exc:  # noqa: BLE001
            self.enqueue("error", {"where": "kill_process", "message": str(exc)})
            return
        if self._procs_lock.acquire(blocking=False):
            try:
                procs, icons = self.list_processes()
                self.enqueue("processes", {"list": procs, "icons": icons, "killed": int(pid)})
            finally:
                self._procs_lock.release()

    def _emit_processes(self) -> None:
        if not self._procs_lock.acquire(blocking=False):
            return   # a scan is already running
        try:
            procs, icons = self.list_processes()
            self.enqueue("processes", {"list": procs, "icons": icons})
        finally:
            self._procs_lock.release()

    def _emit_apps(self) -> None:
        if not self._apps_lock.acquire(blocking=False):
            return
        try:
            res = self.list_apps()
            res["op"] = "list"
            self.enqueue("apps", res)
        finally:
            self._apps_lock.release()

    # ------------------------------------------------------------------ #
    #  Feature: Applications (grouped, categorized)                      #
    # ------------------------------------------------------------------ #
    HIGH_CPU = 10.0  # % of total CPU to count as "high CPU"

    def list_apps(self) -> dict:
        # 1) map REAL open application windows -> pid. We only count top-level,
        #    non-tool, non-cloaked, sized, titled windows (the Alt-Tab set), so
        #    apps that merely keep a hidden/background window are NOT "running".
        windows = {}
        try:
            import ctypes
            import win32con
            import win32gui
            import win32process

            def _is_app_window(hwnd) -> bool:
                if not win32gui.IsWindowVisible(hwnd):
                    return False
                if win32gui.GetWindow(hwnd, win32con.GW_OWNER) != 0:
                    return False  # owned window (dialog/tooltip), not a main window
                ex = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
                if ex & win32con.WS_EX_TOOLWINDOW:
                    return False
                try:  # exclude cloaked (hidden UWP / virtual-desktop) windows
                    cloaked = ctypes.c_int(0)
                    ctypes.windll.dwmapi.DwmGetWindowAttribute(
                        hwnd, 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
                    if cloaked.value != 0:
                        return False
                except Exception:
                    pass
                try:
                    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
                    if (right - left) < 2 or (bottom - top) < 2:
                        return False
                except Exception:
                    return False
                return bool(win32gui.GetWindowText(hwnd))

            def _cb(hwnd, _):
                try:
                    if _is_app_window(hwnd):
                        _, pid = win32process.GetWindowThreadProcessId(hwnd)
                        windows.setdefault(pid, win32gui.GetWindowText(hwnd))
                except Exception:
                    pass
                return True

            win32gui.EnumWindows(_cb, None)
        except Exception:
            pass

        # 2) scan processes. We reuse cached psutil.Process objects across calls
        #    so cpu_percent() gives a real delta since the previous call — NO
        #    blocking sleep, which keeps the whole app light and responsive.
        ncpu = psutil.cpu_count() or 1
        groups = {}
        seen = set()
        # process_iter with only pid+name is cheap; exe is fetched once per pid
        for meta in psutil.process_iter(["pid", "name"]):
            info = meta.info
            pid = info["pid"]
            seen.add(pid)
            name = info.get("name") or "?"
            if pid == 0 or name.lower() in ("system idle process", "idle"):
                continue
            proc = self._proc_cache.get(pid)
            if proc is None:
                proc = meta
                self._proc_cache[pid] = proc
                try:
                    proc.cpu_percent(None)  # prime new process (first read is 0)
                except Exception:
                    pass
            exe = self._exe_cache.get(pid)
            if exe is None:  # exe path is expensive to query — do it once per pid
                try:
                    exe = proc.exe() or ""
                except Exception:
                    exe = ""
                self._exe_cache[pid] = exe
            try:
                cpu = (proc.cpu_percent(None) or 0.0) / ncpu
                mem = proc.memory_percent()
            except Exception:
                continue
            key = (exe or name).lower()
            g = groups.setdefault(key, {"name": name, "exe": exe, "cpu": 0.0,
                                        "mem": 0.0, "pids": [], "title": "", "win": False})
            g["cpu"] += cpu
            g["mem"] += mem
            g["pids"].append(info["pid"])
            if info["pid"] in windows:
                g["win"] = True
                if not g["title"]:
                    g["title"] = windows[info["pid"]]

        # drop dead pids from the caches so they don't grow unbounded
        for dead in [pid for pid in self._proc_cache if pid not in seen]:
            self._proc_cache.pop(dead, None)
            self._exe_cache.pop(dead, None)

        running, high_cpu, background, icons = [], [], [], {}
        for g in groups.values():
            app = {"name": g["name"], "exe": g["exe"], "title": g["title"],
                   "cpu": round(g["cpu"], 1), "mem": round(g["mem"], 1),
                   "pids": g["pids"], "count": len(g["pids"])}
            if g["exe"] and g["exe"] not in icons:
                ic = self._extract_icon(g["exe"])
                if ic:
                    icons[g["exe"]] = ic
            if g["cpu"] >= self.HIGH_CPU:
                high_cpu.append(app)
            elif g["win"]:
                running.append(app)
            else:
                background.append(app)

        running.sort(key=lambda a: -a["mem"])
        high_cpu.sort(key=lambda a: -a["cpu"])
        background.sort(key=lambda a: -a["mem"])
        background = background[:40]  # keep the background list manageable

        return {
            "running": running, "high_cpu": high_cpu, "background": background,
            "counts": {"running": len(running), "high_cpu": len(high_cpu),
                       "background": len(background)},
            "icons": icons,
        }

    def stop_app(self, pids) -> None:
        # Non-blocking: ask each process to close and return immediately. The
        # dashboard removes the row optimistically and the periodic refresh
        # confirms — so there is no delay or freeze when closing an app.
        stopped = 0
        for pid in pids or []:
            try:
                psutil.Process(int(pid)).terminate()
                stopped += 1
            except Exception:
                pass
        self.enqueue("apps", {"op": "action", "action": "stop", "ok": True, "count": stopped})

    def start_app(self, exe: str, name: str = "") -> None:
        try:
            if exe and os.path.exists(exe):
                if platform.system() == "Windows":
                    os.startfile(exe)  # noqa: S606
                else:
                    import subprocess
                    subprocess.Popen([exe])
                self.enqueue("apps", {"op": "action", "action": "start", "ok": True, "name": name or exe})
            else:
                self.enqueue("apps", {"op": "action", "action": "start", "ok": False,
                                      "error": "Executable path unavailable."})
        except Exception as exc:  # noqa: BLE001
            self.enqueue("apps", {"op": "action", "action": "start", "ok": False, "error": str(exc)})

    # ------------------------------------------------------------------ #
    #  Feature: file browser / upload / download                         #
    # ------------------------------------------------------------------ #
    MAX_FILE = 25 * 1024 * 1024  # 25 MB transfer cap

    def list_dir(self, path: str) -> dict:
        # empty path -> list drives (Windows) or root (POSIX)
        if not path:
            if platform.system() == "Windows":
                drives = [f"{d}:\\" for d in string.ascii_uppercase if os.path.exists(f"{d}:\\")]
                return {"path": "", "parent": None,
                        "entries": [{"name": d, "is_dir": True, "size": 0} for d in drives]}
            path = "/"
        entries = []
        try:
            with os.scandir(path) as it:
                for e in it:
                    try:
                        is_dir = e.is_dir()
                        entries.append({"name": e.name, "is_dir": is_dir,
                                        "size": (0 if is_dir else e.stat().st_size)})
                    except Exception:
                        continue
            entries.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
            norm = path.rstrip("\\/")
            parent = os.path.dirname(norm)
            if parent == norm or (len(norm) == 2 and norm[1] == ":"):
                parent = ""
            return {"path": path, "parent": parent, "entries": entries}
        except Exception as exc:  # noqa: BLE001
            return {"path": path, "parent": "", "entries": [], "error": str(exc)}

    def download_file(self, path: str) -> None:
        try:
            size = os.path.getsize(path)
            if size > self.MAX_FILE:
                self.enqueue("file", {"op": "download",
                                      "error": f"File too large ({size // 1024 // 1024} MB, max 25 MB)."})
                return
            with open(path, "rb") as fh:
                data = fh.read()
            self.enqueue("file", {"op": "download", "name": os.path.basename(path),
                                  "size": size, "data": base64.b64encode(data).decode()})
        except Exception as exc:  # noqa: BLE001
            self.enqueue("file", {"op": "download", "error": str(exc)})

    def upload_file(self, folder: str, name: str, data_b64: str) -> None:
        try:
            raw = base64.b64decode(data_b64)
            dest = os.path.join(folder, os.path.basename(name))
            with open(dest, "wb") as fh:
                fh.write(raw)
            self.enqueue("file", {"op": "upload", "status": "saved", "name": name, "path": dest})
        except Exception as exc:  # noqa: BLE001
            self.enqueue("file", {"op": "upload", "error": str(exc)})

    # ------------------------------------------------------------------ #
    #  Feature: sustainability / energy metrics                          #
    # ------------------------------------------------------------------ #
    def system_stats(self) -> dict:
        cpu = psutil.cpu_percent(interval=None)
        freq = psutil.cpu_freq()
        vm = psutil.virtual_memory()

        # --- temperature: real sensor if available, else estimated ---
        temp, temp_est = None, False
        try:
            temps = psutil.sensors_temperatures() or {}
            for entries in temps.values():
                for e in entries:
                    if e.current:
                        temp = round(e.current, 1)
                        break
                if temp is not None:
                    break
        except Exception:
            pass

        # --- battery ---
        battery = None
        is_laptop = False
        try:
            b = psutil.sensors_battery()
            if b is not None:
                is_laptop = True
                secs = b.secsleft
                if secs in (getattr(psutil, "POWER_TIME_UNLIMITED", -1),
                            getattr(psutil, "POWER_TIME_UNKNOWN", -2)) or secs < 0:
                    secs = None
                battery = {"percent": round(b.percent),
                           "plugged": bool(b.power_plugged),
                           "secsleft": secs}
        except Exception:
            pass

        if temp is None:  # Windows rarely exposes CPU temp via psutil
            temp = round(38 + cpu * 0.42, 1)
            temp_est = True

        # --- estimated power draw (watts): idle base + load-scaled ---
        base, dyn = (7, 33) if is_laptop else (30, 95)
        power = round(base + (cpu / 100.0) * dyn, 1)

        return {
            "cpu_percent": round(cpu, 1),
            "cpu_freq_mhz": round(freq.current) if freq else None,
            "cpu_cores": psutil.cpu_count(logical=True),
            "ram_percent": round(vm.percent, 1),
            "ram_used_gb": round(vm.used / 1e9, 1),
            "ram_total_gb": round(vm.total / 1e9, 1),
            "temperature_c": temp,
            "temp_estimated": temp_est,
            "power_watts": power,
            "battery": battery,
        }

    # ------------------------------------------------------------------ #
    #  Feature: screen streaming                                         #
    # ------------------------------------------------------------------ #
    def screen_loop(self) -> None:
        sct = mss.MSS()
        while True:
            if not self.screen_on.is_set():
                self.screen_on.wait()
            try:
                monitor = sct.monitors[1]  # primary display
                self._screen_geom = (monitor["left"], monitor["top"],
                                     monitor["width"], monitor["height"])
                shot = sct.grab(monitor)
                img = Image.frombytes("RGB", shot.size, shot.rgb)
                img.thumbnail((1280, 720))
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=40)
                self.enqueue_stream("screen", {
                    "image": base64.b64encode(buf.getvalue()).decode(),
                    "w": img.width, "h": img.height,
                })
            except Exception as exc:  # noqa: BLE001
                self.enqueue("error", {"where": "screen", "message": str(exc)})
                time.sleep(0.5)
            time.sleep(0.06)  # capture ~15 fps; sender drops stale frames

    # ------------------------------------------------------------------ #
    #  Feature: webcam streaming                                         #
    # ------------------------------------------------------------------ #
    def webcam_loop(self) -> None:
        cap = None
        while True:
            if not self.webcam_on.is_set():
                if cap is not None:
                    cap.release()
                    cap = None
                self.webcam_on.wait()
                continue
            if cap is None:
                cap = cv2.VideoCapture(0, cv2.CAP_ANY)
            ok, frame = cap.read()
            if ok:
                frame = cv2.resize(frame, (640, 480))
                ok2, enc = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 55])
                if ok2:
                    self.enqueue_stream("webcam", {"image": base64.b64encode(enc.tobytes()).decode()})
            else:
                self.enqueue("error", {"where": "webcam", "message": "cannot read camera"})
                time.sleep(0.5)
            time.sleep(0.08)  # ~12 fps

    # ------------------------------------------------------------------ #
    #  Feature: keylogger                                                #
    # ------------------------------------------------------------------ #
    def keylog_start_listener(self) -> None:
        def on_press(key) -> None:
            if not self.keylog_on.is_set():
                return
            try:
                ch = key.char
            except AttributeError:
                ch = f"[{key.name}]"
            if ch is None:
                return
            with self._keylock:
                self._keybuf.append(ch)

        listener = keyboard.Listener(on_press=on_press)
        listener.daemon = True
        listener.start()

        def flusher() -> None:
            while True:
                time.sleep(1.0)
                if not self.keylog_on.is_set():
                    continue
                with self._keylock:
                    if self._keybuf:
                        text = "".join(self._keybuf)
                        self._keybuf.clear()
                    else:
                        text = ""
                if text:
                    self.enqueue("keylog", {"text": text})

        threading.Thread(target=flusher, daemon=True).start()

    # ------------------------------------------------------------------ #
    #  Feature: remote mouse + keyboard control                          #
    # ------------------------------------------------------------------ #
    def _screen_size(self):
        """(left, top, width, height) of the primary monitor."""
        if self._screen_geom is None:
            with mss.MSS() as m:
                mon = m.monitors[1]
                self._screen_geom = (mon["left"], mon["top"], mon["width"], mon["height"])
        return self._screen_geom

    def _to_pixels(self, fx: float, fy: float):
        left, top, w, h = self._screen_size()
        return int(left + fx * w), int(top + fy * h)

    def mouse_move(self, fx: float, fy: float) -> None:
        self.mouse.position = self._to_pixels(fx, fy)

    def mouse_click(self, fx: float, fy: float, button: str, double: bool) -> None:
        self.mouse.position = self._to_pixels(fx, fy)
        btn = Button.right if button == "right" else \
              Button.middle if button == "middle" else Button.left
        self.mouse.click(btn, 2 if double else 1)

    def mouse_scroll(self, fx: float, fy: float, dy: int) -> None:
        self.mouse.position = self._to_pixels(fx, fy)
        self.mouse.scroll(0, dy)

    def key_press(self, key: str) -> None:
        try:
            if key in self.SPECIAL_KEYS:
                self.keyboard.tap(self.SPECIAL_KEYS[key])
            elif len(key) == 1:
                self.keyboard.type(key)
        except Exception as exc:  # noqa: BLE001
            self.enqueue("error", {"where": "key_press", "message": str(exc)})

    # ------------------------------------------------------------------ #
    #  Feature: power                                                    #
    # ------------------------------------------------------------------ #
    def power(self, mode: str) -> None:
        is_windows = platform.system() == "Windows"
        if mode == "shutdown":
            self.enqueue("power", {"mode": mode, "status": "scheduled (5s)"})
            os.system("shutdown /s /t 5" if is_windows else "shutdown -h +0")
        elif mode == "restart":
            self.enqueue("power", {"mode": mode, "status": "scheduled (5s)"})
            os.system("shutdown /r /t 5" if is_windows else "shutdown -r +0")
        elif mode == "lock":
            self.enqueue("power", {"mode": mode, "status": "screen locked"})
            os.system("rundll32.exe user32.dll,LockWorkStation" if is_windows
                      else "loginctl lock-session")
        elif mode == "sleep":
            self.enqueue("power", {"mode": mode, "status": "going to sleep"})
            os.system("rundll32.exe powrprof.dll,SetSuspendState 0,1,0" if is_windows
                      else "systemctl suspend")

    # ------------------------------------------------------------------ #
    #  Command dispatch                                                  #
    # ------------------------------------------------------------------ #
    def handle_command(self, action: str, params: dict) -> None:
        print(f"[cmd] {action} {params or ''}")
        if action == "list_processes":
            threading.Thread(target=self._emit_processes, daemon=True).start()
        elif action == "kill_process":
            threading.Thread(target=self.kill_process,
                             args=(params.get("pid"),), daemon=True).start()
        elif action == "list_apps":
            threading.Thread(target=self._emit_apps, daemon=True).start()
        elif action == "stop_app":
            self.stop_app(params.get("pids"))
        elif action == "start_app":
            self.start_app(params.get("exe", ""), params.get("name", ""))
        elif action == "system_stats":
            self.enqueue("system", self.system_stats())
        elif action == "list_dir":
            res = self.list_dir(params.get("path", ""))
            res["op"] = "list"
            self.enqueue("file", res)
        elif action == "download_file":
            self.download_file(params.get("path", ""))
        elif action == "upload_file":
            self.upload_file(params.get("folder", ""), params.get("name", ""), params.get("data", ""))
        elif action == "screen_start":
            self.screen_on.set()
        elif action == "screen_stop":
            self.screen_on.clear()
        elif action == "webcam_start":
            self.webcam_on.set()
        elif action == "webcam_stop":
            self.webcam_on.clear()
        elif action == "keylog_start":
            self.keylog_on.set()
        elif action == "keylog_stop":
            self.keylog_on.clear()
        elif action == "mouse_move":
            self.mouse_move(params.get("x", 0), params.get("y", 0))
        elif action == "mouse_click":
            self.mouse_click(params.get("x", 0), params.get("y", 0),
                             params.get("button", "left"), params.get("double", False))
        elif action == "mouse_scroll":
            self.mouse_scroll(params.get("x", 0), params.get("y", 0), params.get("dy", 0))
        elif action == "key_press":
            self.key_press(params.get("key", ""))
        elif action in ("shutdown", "restart", "lock", "sleep"):
            self.power(action)

    # ------------------------------------------------------------------ #
    #  Networking                                                        #
    # ------------------------------------------------------------------ #
    async def sender(self, ws) -> None:
        while True:
            sent = False

            # 1) newest live frames (coalesced — never a backlog)
            with self.latest_lock:
                frames = self.latest
                self.latest = {}
            for channel, payload in frames.items():
                await ws.send(json.dumps(
                    {"type": "data", "channel": channel, "payload": payload}))
                sent = True

            # 2) discrete messages
            try:
                while True:
                    await ws.send(json.dumps(self.outbox.get_nowait()))
                    sent = True
            except queue.Empty:
                pass

            if not sent:
                await asyncio.sleep(0.01)

    async def receiver(self, ws) -> None:
        async for raw in ws:
            # A single bad command must never drop the whole connection.
            try:
                msg = json.loads(raw)
                if msg.get("type") == "command":
                    self.handle_command(msg.get("action"), msg.get("params", {}))
            except Exception as exc:  # noqa: BLE001
                self.enqueue("error", {"where": "command", "message": str(exc)})

    async def run(self) -> None:
        # Start feature worker threads once.
        threading.Thread(target=self.screen_loop, daemon=True).start()
        threading.Thread(target=self.webcam_loop, daemon=True).start()
        self.keylog_start_listener()

        while True:
            try:
                async with websockets.connect(self.server_url, max_size=64 * 1024 * 1024) as ws:
                    await ws.send(json.dumps({
                        "type": "register",
                        "device_id": self.device_id,
                        "password": self.password,
                        "virtual": self.virtual,
                        "hostname": socket.gethostname(),
                        "platform": platform.platform(),
                        "username": getpass.getuser(),
                    }))
                    print(f"[+] connected to {self.server_url} as device {self.device_id}")
                    send_task = asyncio.create_task(self.sender(ws))
                    try:
                        await self.receiver(ws)
                    finally:
                        send_task.cancel()
            except Exception as exc:  # noqa: BLE001
                print(f"[!] connection error: {exc}")
            print("[*] reconnecting in 3s ...")
            await asyncio.sleep(3)


def main() -> None:
    server = os.environ.get("RMC_SERVER", "ws://localhost:8000/ws/agent")
    token = os.environ.get("RMC_TOKEN", "")

    agent = Agent(server, token)

    # Also write the credentials to a file so they can be read any time,
    # even when the agent runs without a visible console window.
    # (Skip for a fixed/virtual identity so it doesn't overwrite the real one.)
    try:
        if os.environ.get("RMC_DEVICE_ID"):
            raise RuntimeError("virtual identity — skip file")
        cred_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "MY-DEVICE-CREDENTIALS.txt")
        with open(cred_path, "w", encoding="utf-8") as fh:
            fh.write("Remote Management — this PC's credentials\n")
            fh.write("Give these to whoever should control this PC.\n\n")
            fh.write(f"DEVICE ID : {agent.device_id}\n")
            fh.write(f"PASSWORD  : {agent.password}\n\n")
            fh.write("(The password changes each time you restart the agent.)\n")
    except Exception:
        pass

    print("=" * 60)
    print("  REMOTE MANAGEMENT AGENT")
    print("  Give these to whoever should connect to THIS PC:")
    print("")
    print(f"      DEVICE ID :  {agent.device_id}")
    print(f"      PASSWORD  :  {agent.password}")
    print("")
    print("  They open the console, enter the ID + password, and can then")
    print("  see your screen/webcam/keys and control this PC.")
    print("  The password changes each time you restart the agent.")
    print("  Only run this on a machine you own or are authorized to manage.")
    print("  Close this window to stop the agent (cuts off all access).")
    print("=" * 60)

    try:
        asyncio.run(agent.run())
    except KeyboardInterrupt:
        print("\n[*] agent stopped.")


if __name__ == "__main__":
    main()
