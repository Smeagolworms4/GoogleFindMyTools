# server_web.py
import os
import sys
import io
import re
import time
import asyncio
import threading
import subprocess
import json
import traceback
import builtins
import signal
import ast
import multiprocessing as mp
from pathlib import Path
from collections import deque
from typing import Optional, Any

from fastapi import FastAPI, WebSocket
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

import httpx
import websockets


# ---------------------------
# multiprocessing start method (Linux: fork)
# ---------------------------
try:
    mp.set_start_method("fork", force=True)
except Exception:
    pass


app = FastAPI()

BASE_DIR = Path(__file__).parent
WEB_DIR = BASE_DIR / "web"
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

# ---- X / VNC / noVNC ----
DISPLAY = os.environ.get("DISPLAY", ":99")
VNC_PORT = int(os.environ.get("GFM_VNC_PORT", "5900"))
NOVNC_PORT = int(os.environ.get("GFM_NOVNC_PORT", "5800"))

SCREEN_W = int(os.environ.get("GFM_SCREEN_W", "1280"))
SCREEN_H = int(os.environ.get("GFM_SCREEN_H", "720"))
SCREEN_D = int(os.environ.get("GFM_SCREEN_D", "24"))

# ---- Repo paths ----
TOOLS_DIR = Path(os.environ.get("GFM_TOOLS_DIR", "/app")).resolve()

# persistent auth data lives here (mounted volume)
DATA_AUTH_DIR = Path("/data/Auth")
SECRETS_FILE = Path(os.environ.get("GFM_SECRETS_PATH", str(DATA_AUTH_DIR / "secrets.json")))
AUTH_DONE_MARKER = Path(os.environ.get("GFM_AUTH_DONE_MARKER", str(DATA_AUTH_DIR / ".auth_done.json")))
AUTH_WAIT_SECONDS = int(os.environ.get("GFM_AUTH_WAIT_SECONDS", "900"))

# ---- Processes ----
xvfb_proc = None
wm_proc = None
vnc_proc = None
ws_proc = None

# ---- Auth job state ----
auth_state = "idle"   # idle | running
auth_thread: threading.Thread | None = None
auth_started_ts: float | None = None

# ---- Cleanup state ----
cleanup_scheduled = False
cleanup_lock = threading.Lock()

# ---- Logs tail ----
LOG_LINES = int(os.environ.get("GFM_LOG_LINES", "300"))
log_buf = deque(maxlen=LOG_LINES)
log_lock = threading.Lock()


# =========================================================
# Logging / helpers
# =========================================================

def _log(line: str):
    line = (line or "").rstrip("\n")
    if not line:
        return
    with log_lock:
        log_buf.append(line)
    # utile aussi dans docker logs
    print(line, flush=True)


def is_port_open(port: int) -> bool:
    import socket
    s = socket.socket()
    try:
        s.settimeout(0.5)
        s.connect(("127.0.0.1", port))
        return True
    except Exception:
        return False
    finally:
        s.close()


def _terminate(proc):
    if not proc:
        return
    try:
        if proc.poll() is None:
            try:
                pgid = os.getpgid(proc.pid)
                os.killpg(pgid, signal.SIGTERM)
            except Exception:
                proc.terminate()

            try:
                proc.wait(timeout=3)
            except Exception:
                try:
                    pgid = os.getpgid(proc.pid)
                    os.killpg(pgid, signal.SIGKILL)
                except Exception:
                    proc.kill()
    except Exception:
        pass


def _stop_x_stack():
    global xvfb_proc, wm_proc, vnc_proc, ws_proc
    _terminate(ws_proc); ws_proc = None
    _terminate(vnc_proc); vnc_proc = None
    _terminate(wm_proc); wm_proc = None
    _terminate(xvfb_proc); xvfb_proc = None


def _run_sh(cmd: str):
    subprocess.run(["bash", "-lc", cmd], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _kill_chrome_family_hard():
    """
    Cleanup: sync pkill (no zombies).
    """
    _run_sh(
        f"pkill -TERM -f 'DISPLAY={DISPLAY}' 2>/dev/null || true; "
        f"pkill -TERM -f 'XDG_RUNTIME_DIR=/tmp/runtime-root' 2>/dev/null || true; true"
    )
    _run_sh(
        "pkill -TERM -f 'undetected_chromedriver' 2>/dev/null || true; "
        "pkill -TERM -f 'chromedriver' 2>/dev/null || true; "
        "pkill -TERM -f 'google-chrome' 2>/dev/null || true; "
        "pkill -TERM -f 'chrome_crashpad' 2>/dev/null || true; "
        "pkill -TERM -f '\\bchrome\\b' 2>/dev/null || true; true"
    )
    time.sleep(0.4)
    _run_sh(
        f"pkill -KILL -f 'DISPLAY={DISPLAY}' 2>/dev/null || true; "
        "pkill -KILL -f 'undetected_chromedriver' 2>/dev/null || true; "
        "pkill -KILL -f 'chromedriver' 2>/dev/null || true; "
        "pkill -KILL -f 'google-chrome' 2>/dev/null || true; "
        "pkill -KILL -f 'chrome_crashpad' 2>/dev/null || true; "
        "pkill -KILL -f '\\bchrome\\b' 2>/dev/null || true; true"
    )


def _schedule_cleanup_after_done_if_success(marker: dict | None):
    """
    IMPORTANT: on ne cleanup que si success=True.
    """
    global cleanup_scheduled
    if not marker or not marker.get("success"):
        return

    with cleanup_lock:
        if cleanup_scheduled:
            return
        cleanup_scheduled = True

    def _job():
        time.sleep(1.0)
        try:
            _log("[cleanup] killing chrome...")
            _kill_chrome_family_hard()
        except Exception:
            pass
        try:
            _log("[cleanup] stopping X stack...")
            _stop_x_stack()
        except Exception:
            pass
        _log("[cleanup] done")

    threading.Thread(target=_job, daemon=True).start()


def _iframe_url():
    return "/novnc/vnc.html?path=novnc/websockify&autoconnect=1&reconnect=1&resize=scale"


def _ensure_runtime_env():
    os.environ["DISPLAY"] = DISPLAY
    os.environ.setdefault("HOME", "/tmp")
    os.environ.setdefault("XDG_RUNTIME_DIR", "/tmp/runtime-root")
    os.environ.setdefault("DBUS_SESSION_BUS_ADDRESS", "/dev/null")
    os.environ.setdefault("CHROME_BIN", "/usr/bin/google-chrome")
    os.environ.setdefault("NO_AT_BRIDGE", "1")

    try:
        Path(os.environ["XDG_RUNTIME_DIR"]).mkdir(parents=True, exist_ok=True)
        os.chmod(os.environ["XDG_RUNTIME_DIR"], 0o700)
    except Exception:
        pass


def _ensure_x_started():
    """
    Ne change rien à ton comportement d’avant :
    - start Xvfb
    - start openbox
    - start x11vnc
    - start websockify
    - wait novnc port open
    """
    global xvfb_proc, wm_proc, vnc_proc, ws_proc

    _ensure_runtime_env()

    if not (xvfb_proc and xvfb_proc.poll() is None):
        _log(f"[x] starting Xvfb {DISPLAY} {SCREEN_W}x{SCREEN_H}x{SCREEN_D}")
        xvfb_proc = subprocess.Popen(
            ["Xvfb", DISPLAY, "-screen", "0", f"{SCREEN_W}x{SCREEN_H}x{SCREEN_D}", "-ac"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        time.sleep(0.8)

    if not (wm_proc and wm_proc.poll() is None):
        _log("[x] starting openbox")
        wm_proc = subprocess.Popen(
            ["openbox"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=dict(os.environ, DISPLAY=DISPLAY),
            start_new_session=True,
        )
        time.sleep(0.3)

    if not (vnc_proc and vnc_proc.poll() is None):
        _log("[vnc] starting x11vnc")
        vnc_proc = subprocess.Popen(
            ["x11vnc", "-display", DISPLAY, "-nopw", "-forever", "-shared",
             "-rfbport", str(VNC_PORT), "-noxdamage"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        time.sleep(0.6)

    if not (ws_proc and ws_proc.poll() is None):
        _log("[novnc] starting websockify")
        ws_proc = subprocess.Popen(
            ["websockify", "--web=/usr/share/novnc", str(NOVNC_PORT), f"127.0.0.1:{VNC_PORT}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    deadline = time.time() + 20
    while time.time() < deadline:
        if is_port_open(NOVNC_PORT):
            _log("[novnc] ready")
            return
        time.sleep(0.25)

    raise RuntimeError("noVNC failed to start")


def _secrets_exist() -> bool:
    return SECRETS_FILE.exists()


def _clear_done_marker():
    if AUTH_DONE_MARKER.exists():
        try:
            AUTH_DONE_MARKER.unlink()
        except Exception:
            pass


def _write_done_marker(success: bool, error: str | None, advanced_ok: bool | None = None):
    AUTH_DONE_MARKER.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "success": bool(success),
        "advanced_ok": bool(advanced_ok) if advanced_ok is not None else None,
        "error": error,
        "secrets_exists": _secrets_exist(),
        "secrets_path": str(SECRETS_FILE),
        "ts": int(time.time()),
    }
    AUTH_DONE_MARKER.write_text(json.dumps(payload), encoding="utf-8")


def _wait_for_secrets(timeout_s: int) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if _secrets_exist():
            return True
        time.sleep(1.0)
    return False


def _kick_chrome_into_view():
    """
    Helper pour ramener Chrome en plein écran dans VNC.
    """
    cmd = f"""
set -e
export DISPLAY={DISPLAY}
for i in $(seq 1 160); do
  WID="$(xdotool search --onlyvisible --class chrome 2>/dev/null | head -n 1 || true)"
  if [ -z "$WID" ]; then
    WID="$(xdotool search --onlyvisible --class Google-chrome 2>/dev/null | head -n 1 || true)"
  fi
  if [ -z "$WID" ]; then
    WID="$(xdotool search --onlyvisible --name 'Chrome' 2>/dev/null | head -n 1 || true)"
  fi
  if [ -n "$WID" ]; then
    xdotool windowmove "$WID" 0 0 || true
    xdotool windowsize "$WID" {SCREEN_W} {SCREEN_H} || true
    exit 0
  fi
  sleep 0.25
done
exit 0
"""
    subprocess.Popen(
        ["bash", "-lc", cmd],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


# =========================================================
# FULL AUTH JOB (single iframe, but may open Chrome multiple times)
# =========================================================

def _auth_only_job():
    """
    FULL AUTH:
    - aas + adm
    - spot token + owner key check
    """
    global auth_state, auth_started_ts

    success = False
    advanced_ok = False
    err = None

    try:
        _ensure_x_started()
        _clear_done_marker()

        if str(TOOLS_DIR) not in sys.path:
            sys.path.insert(0, str(TOOLS_DIR))

        from Auth.aas_token_retrieval import get_aas_token
        from Auth.adm_token_retrieval import get_adm_token
        from Auth.username_provider import get_username

        orig_input = builtins.input
        builtins.input = lambda *args, **kwargs: ""

        try:
            _log("[auth] get_aas_token()...")
            _ = get_aas_token()
            _kick_chrome_into_view()

            username = get_username()

            _log("[auth] get_adm_token(username)...")
            _ = get_adm_token(username)

            _log("[auth] get_spot_token(username) (ADVANCED)...")
            from Auth.spot_token_retrieval import get_spot_token
            _ = get_spot_token(username)
            _kick_chrome_into_view()

            _log("[auth] get_owner_key() (ADVANCED CHECK)...")
            from SpotApi.GetEidInfoForE2eeDevices.get_owner_key import get_owner_key
            _ = get_owner_key()

            advanced_ok = True
            _log("[auth] advanced auth OK (spot + owner key)")
        finally:
            builtins.input = orig_input

        if not _wait_for_secrets(AUTH_WAIT_SECONDS):
            err = f"Timeout: secrets.json not created after {AUTH_WAIT_SECONDS}s"
            _log("[auth] " + err)
        elif not advanced_ok:
            err = "AUTH_INCOMPLETE_ADVANCED"
            _log("[auth] " + err)
        else:
            success = True
            _log("[auth] FULL auth success")

    except Exception:
        err = traceback.format_exc()
        _log("[auth] EXCEPTION:\n" + err)

    finally:
        _write_done_marker(success=success, error=err, advanced_ok=advanced_ok)
        auth_state = "idle"
        auth_started_ts = None
        _log("[auth] done marker written")


# =========================================================
# API errors
# =========================================================

def _reauth_response(details: str, status_code: int = 401):
    return JSONResponse(
        {
            "ok": False,
            "code": "REAUTH_REQUIRED",
            "message": "Token invalid/expired or login required. Please re-authenticate.",
            "details": details,
            "action": "OPEN_IFRAME",
        },
        status_code=status_code,
    )


def _require_auth():
    if not _secrets_exist():
        return _reauth_response("SECRETS_MISSING", status_code=401)
    return None


# =========================================================
# Devices helpers
# =========================================================

def _normalize_devices(devices_any):
    out = []
    if devices_any is None:
        return out

    if isinstance(devices_any, dict):
        iterable = list(devices_any.values())
    elif isinstance(devices_any, list):
        iterable = devices_any
    else:
        iterable = [devices_any]

    def _from_tuplelike(t):
        name = t[0] if len(t) >= 1 else None
        dev_id = t[1] if len(t) >= 2 else None
        dev_type = t[2] if len(t) >= 3 else None
        return {"id": dev_id, "name": name, "type": dev_type, "raw": t}

    for d in iterable:
        if isinstance(d, dict):
            name = d.get("device_name") or d.get("name") or d.get("user_defined_name")
            dev_id = d.get("canonic_device_id") or d.get("device_id") or d.get("id") or d.get("eid")
            dev_type = d.get("device_type") or d.get("type") or d.get("model_name") or d.get("model")
            out.append({"id": dev_id, "name": name, "type": dev_type, "raw": d})
            continue

        if isinstance(d, (tuple, list)):
            out.append(_from_tuplelike(d))
            continue

        if isinstance(d, str):
            s = d.strip()
            if (s.startswith("(") and s.endswith(")")) or (s.startswith("[") and s.endswith("]")):
                try:
                    parsed = ast.literal_eval(s)
                    if isinstance(parsed, (tuple, list)) and len(parsed) >= 2:
                        out.append(_from_tuplelike(parsed))
                        continue
                except Exception:
                    pass
            out.append({"id": None, "name": s, "type": None, "raw": s})
            continue

        out.append({"id": None, "name": str(d), "type": None, "raw": str(d)})

    for x in out:
        if x["name"] is not None and not isinstance(x["name"], str):
            x["name"] = str(x["name"])
        if x["id"] is not None and not isinstance(x["id"], str):
            x["id"] = str(x["id"])
        if x["type"] is not None and not isinstance(x["type"], str):
            x["type"] = str(x["type"])

    return out


# =========================================================
# Isolated runner (NO FREEZE) - generic
# =========================================================

_AUTH_ABORT_PATTERNS = [
    ("press enter to continue", "PRESS_ENTER"),
    ("this script will now open google chrome", "OPEN_CHROME"),
    ("phone_registration_error", "PHONE_REGISTRATION_ERROR"),
    ("gcm register request attempt", "GCM_REGISTER_FAILED"),
]


def _detect_reauth_line(line: str) -> str | None:
    if not line:
        return None
    ll = line.strip().lower()
    if not ll:
        return None

    for needle, code in _AUTH_ABORT_PATTERNS:
        if needle in ll:
            if code == "GCM_REGISTER_FAILED":
                if "has failed" in ll:
                    return code
                continue
            return code

    return None


class _CaptureAndAbort(io.TextIOBase):
    def __init__(self, max_chars: int = 24000):
        super().__init__()
        self._max = max_chars
        self._buf: list[str] = []
        self._size = 0

    def write(self, s):
        if not s:
            return 0

        text = str(s)
        for line in text.splitlines():
            reason = _detect_reauth_line(line)
            if reason:
                raise KeyboardInterrupt(reason)

            if line:
                self._buf.append(line)
                self._size += len(line) + 1
                while self._size > self._max and self._buf:
                    dropped = self._buf.pop(0)
                    self._size -= len(dropped) + 1

        return len(text)

    def flush(self):
        return

    def get_text(self) -> str:
        return "\n".join(self._buf)


def _worker_bootstrap(tools_dir_str: str):
    if "/app" not in sys.path:
        sys.path.insert(0, "/app")
    if tools_dir_str and tools_dir_str not in sys.path:
        sys.path.insert(0, tools_dir_str)

    orig_input = builtins.input
    builtins.input = lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt("INPUT_BLOCKED"))

    cap = _CaptureAndAbort()
    orig_out, orig_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = cap, cap

    return orig_input, orig_out, orig_err, cap


def _worker_restore(orig_input, orig_out, orig_err):
    builtins.input = orig_input
    sys.stdout, sys.stderr = orig_out, orig_err


def _job_list_devices():
    from NovaApi.ListDevices.nbe_list_devices import (
        request_device_list,
        parse_device_list_protobuf,
        get_canonic_ids,
    )
    device_list = request_device_list()
    devices_decoded = parse_device_list_protobuf(device_list)
    return get_canonic_ids(devices_decoded)


def _parse_advertisement_key(stdout_text: str) -> str | None:
    if not stdout_text:
        return None
    cands = re.findall(r"\b[0-9a-fA-F]{24,128}\b", stdout_text)
    if not cands:
        return None
    cands.sort(key=len, reverse=True)
    return cands[0].lower()


def _job_create_custom_esp32():
    from SpotApi.CreateBleDevice.create_ble_device import register_esp32
    return register_esp32()


def _job_locate_device(device_id: str):
    """
    Locate (best-effort). Adapte l'import si besoin selon ton repo.
    """
    # tentative 1 (Nova)
    try:
        from NovaApi.LocateDevice.nbe_locate_device import locate_device
        return locate_device(device_id)
    except Exception:
        pass

    # tentative 2 (Spot)
    try:
        from SpotApi.GetDeviceLocation.get_device_location import get_device_location
        return get_device_location(device_id)
    except Exception:
        pass

    raise RuntimeError("No locate implementation found in repo. Wire the correct locate function import.")


def _worker_entry(conn, job: str, tools_dir_str: str, payload: dict | None):
    orig_input = orig_out = orig_err = cap = None
    try:
        orig_input, orig_out, orig_err, cap = _worker_bootstrap(tools_dir_str)

        if job == "list_devices":
            res = _job_list_devices()
            conn.send(("ok", {"result": res, "stdout": cap.get_text()}))
            return

        if job == "create_custom_esp32":
            res = _job_create_custom_esp32()
            conn.send(("ok", {"result": res, "stdout": cap.get_text()}))
            return

        if job == "locate_device":
            dev_id = (payload or {}).get("device_id")
            if not dev_id:
                conn.send(("err", {"error": "device_id missing", "stdout": cap.get_text()}))
                return
            res = _job_locate_device(str(dev_id))
            conn.send(("ok", {"result": res, "stdout": cap.get_text()}))
            return

        conn.send(("err", {"error": f"Unknown job: {job}", "stdout": cap.get_text() if cap else ""}))

    except KeyboardInterrupt as e:
        conn.send(("reauth", {"reason": str(e), "stdout": cap.get_text() if cap else ""}))
    except Exception:
        conn.send(("err", {"error": traceback.format_exc(), "stdout": cap.get_text() if cap else ""}))
    finally:
        if orig_input is not None:
            _worker_restore(orig_input, orig_out, orig_err)


def _run_isolated(job: str, payload: dict | None = None, timeout_s: int = 25):
    parent, child = mp.Pipe(duplex=False)
    p = mp.Process(target=_worker_entry, args=(child, job, str(TOOLS_DIR), payload), daemon=True)
    p.start()

    if parent.poll(timeout_s):
        kind, out = parent.recv()
        try:
            p.join(timeout=0.3)
        except Exception:
            pass
        if p.is_alive():
            try:
                p.terminate()
            except Exception:
                pass
        return kind, out

    try:
        p.terminate()
    except Exception:
        pass
    return "reauth", {"reason": "TIMEOUT", "stdout": ""}


# =========================================================
# Routes
# =========================================================

@app.get("/")
def index():
    return FileResponse(WEB_DIR / "index.html")


@app.get("/api/connection/status")
def connection_status():
    return {"ok": True, "connected": _secrets_exist(), "secrets_path": str(SECRETS_FILE)}


@app.post("/api/novnc/start")
def novnc_start():
    try:
        _ensure_x_started()
        return {"ok": True, "ready": True, "iframe_url": _iframe_url()}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.get("/api/novnc/status")
def novnc_status():
    running = ws_proc is not None and ws_proc.poll() is None
    ready = running and is_port_open(NOVNC_PORT)
    return {"ok": True, "running": running, "ready": ready, "iframe_url": _iframe_url()}


@app.post("/api/auth/start")
def auth_start():
    global auth_thread, auth_state, cleanup_scheduled, auth_started_ts

    if auth_state == "running":
        return {"ok": True, "state": "running"}

    with cleanup_lock:
        cleanup_scheduled = False

    with log_lock:
        log_buf.clear()

    _clear_done_marker()
    auth_state = "running"
    auth_started_ts = time.time()
    _log("[api] /api/auth/start")

    auth_thread = threading.Thread(target=_auth_only_job, daemon=True)
    auth_thread.start()

    return {"ok": True, "state": "running"}


@app.get("/api/auth/status")
def auth_status():
    done = AUTH_DONE_MARKER.exists()
    marker = None

    if done:
        try:
            marker = json.loads(AUTH_DONE_MARKER.read_text(encoding="utf-8"))
        except Exception:
            marker = {"success": False, "error": "Failed to parse done marker"}

        if auth_state != "running":
            _schedule_cleanup_after_done_if_success(marker)

    elapsed = None
    if auth_started_ts:
        elapsed = int(time.time() - auth_started_ts)

    with log_lock:
        logs = list(log_buf)

    return {
        "ok": True,
        "running": auth_state == "running",
        "done": done,
        "elapsed_s": elapsed,
        "result": marker,
        "connected": _secrets_exist(),
        "logs_tail": logs[-160:],
    }


# ===== secrets.json endpoint (DANGEROUS) =====
@app.get("/api/secrets")
def api_secrets():
    deny = _require_auth()
    if deny:
        return deny

    try:
        txt = SECRETS_FILE.read_text(encoding="utf-8")
        parsed = None
        try:
            parsed = json.loads(txt)
        except Exception:
            parsed = None
        return {"ok": True, "secrets_text": txt, "secrets_json": parsed}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# =========================================================
# noVNC HTTP proxy
# =========================================================
@app.api_route("/novnc/{path:path}", methods=["GET"])
async def novnc_http_proxy(path: str):
    target = f"http://127.0.0.1:{NOVNC_PORT}/{path}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(target)
            headers = dict(r.headers)
            headers.pop("transfer-encoding", None)
            return Response(content=r.content, status_code=r.status_code, headers=headers)
    except httpx.ConnectError:
        return JSONResponse({"ok": False, "error": "noVNC backend not reachable (is it started?)"}, status_code=503)


# =========================================================
# noVNC WebSocket proxy
# =========================================================
@app.websocket("/novnc/websockify")
async def novnc_ws_proxy(ws: WebSocket):
    await ws.accept()
    upstream_url = f"ws://127.0.0.1:{NOVNC_PORT}/websockify"

    try:
        async with websockets.connect(upstream_url) as upstream:

            async def client_to_upstream():
                try:
                    while True:
                        data = await ws.receive_bytes()
                        await upstream.send(data)
                except Exception:
                    pass

            async def upstream_to_client():
                try:
                    async for msg in upstream:
                        if isinstance(msg, bytes):
                            await ws.send_bytes(msg)
                        else:
                            await ws.send_text(msg)
                except Exception:
                    pass

            await asyncio.gather(client_to_upstream(), upstream_to_client())

    except Exception:
        try:
            await ws.close(code=1011)
        except Exception:
            pass


# =========================================================
# DEVICE API
# =========================================================

@app.get("/api/devices")
def api_devices():
    deny = _require_auth()
    if deny:
        return deny

    kind, payload = _run_isolated("list_devices", timeout_s=12)

    if kind == "ok":
        return {"ok": True, "devices": _normalize_devices((payload or {}).get("result"))}

    if kind == "reauth":
        reason = (payload or {}).get("reason", "REAUTH")
        _log(f"[devices] reauth detected: {reason}")
        return _reauth_response(reason, status_code=401)

    err = (payload or {}).get("error", "Unknown error")
    _log("[devices] EXCEPTION:\n" + str(err))
    return JSONResponse({"ok": False, "error": err}, status_code=500)


@app.post("/api/devices/custom")
def api_devices_custom_create():
    """
    ESP32 tracker: SpotApi.CreateBleDevice.create_ble_device.register_esp32()
    Return advertisement_key (eid hex).
    """
    deny = _require_auth()
    if deny:
        return deny

    kind, payload = _run_isolated("create_custom_esp32", payload={}, timeout_s=25)

    if kind == "ok":
        stdout_text = (payload or {}).get("stdout", "")
        adv_key = _parse_advertisement_key(stdout_text)
        if not adv_key:
            return {
                "ok": True,
                "message": "Device registered, but key not parsed. Check stdout.",
                "advertisement_key": None,
                "stdout_tail": stdout_text[-1600:],
            }
        return {"ok": True, "message": "ESP32 device registered.", "advertisement_key": adv_key}

    if kind == "reauth":
        reason = (payload or {}).get("reason", "REAUTH")
        _log(f"[devices/custom] reauth detected: {reason}")
        return _reauth_response(reason, status_code=401)

    err = (payload or {}).get("error", "Unknown error")
    _log("[devices/custom] EXCEPTION:\n" + str(err))
    return JSONResponse({"ok": False, "error": err}, status_code=500)


@app.post("/api/devices/locate")
def api_devices_locate(body: dict):
    """
    Locate a device by id. Returns raw JSON from repo.
    """
    deny = _require_auth()
    if deny:
        return deny

    device_id = (body or {}).get("device_id")
    if not device_id:
        return JSONResponse({"ok": False, "error": "device_id required"}, status_code=400)

    kind, payload = _run_isolated("locate_device", payload={"device_id": str(device_id)}, timeout_s=25)

    if kind == "ok":
        return {"ok": True, "location": (payload or {}).get("result")}

    if kind == "reauth":
        reason = (payload or {}).get("reason", "REAUTH")
        _log(f"[devices/locate] reauth detected: {reason}")
        return _reauth_response(reason, status_code=401)

    err = (payload or {}).get("error", "Unknown error")
    _log("[devices/locate] EXCEPTION:\n" + str(err))
    return JSONResponse({"ok": False, "error": err}, status_code=500)
