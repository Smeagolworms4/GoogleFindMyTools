import os
import time
import asyncio
import threading
import subprocess
import json
import traceback
import builtins
import signal
import shutil
from pathlib import Path
from collections import deque

from fastapi import FastAPI, WebSocket
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

import httpx
import websockets

app = FastAPI()

BASE_DIR = Path(__file__).parent
WEB_DIR = BASE_DIR / "web"
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

# ---- X / VNC / noVNC ----
DISPLAY = os.environ.get("DISPLAY", ":99")
VNC_PORT = int(os.environ.get("GFM_VNC_PORT", "5900"))
NOVNC_PORT = int(os.environ.get("GFM_NOVNC_PORT", "5800"))

# Keep close to iframe size
SCREEN_W = int(os.environ.get("GFM_SCREEN_W", "1280"))
SCREEN_H = int(os.environ.get("GFM_SCREEN_H", "720"))
SCREEN_D = int(os.environ.get("GFM_SCREEN_D", "24"))

# ---- Repo paths ----
TOOLS_DIR = Path(os.environ.get("GFM_TOOLS_DIR", "/app")).resolve()
AUTH_DIR = TOOLS_DIR / "Auth"
DATA_AUTH_DIR = Path("/data/Auth")

SECRETS_FILE = Path(os.environ.get("GFM_SECRETS_PATH", "/data/Auth/secrets.json"))
AUTH_DONE_MARKER = Path(os.environ.get("GFM_AUTH_DONE_MARKER", "/data/Auth/.auth_done.json"))
AUTH_WAIT_SECONDS = int(os.environ.get("GFM_AUTH_WAIT_SECONDS", "900"))  # seconds

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


def _log(line: str):
    line = (line or "").rstrip("\n")
    if not line:
        return
    with log_lock:
        log_buf.append(line)


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
    """Terminate a process and its process-group if possible (reliable in containers)."""
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
    """Run shell command synchronously (so we don't leave pkill/sleep zombies)."""
    subprocess.run(["bash", "-lc", cmd], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _has_any_process(pattern: str) -> bool:
    r = subprocess.run(
        ["bash", "-lc", f"pgrep -fa {json.dumps(pattern)} >/dev/null 2>&1"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return r.returncode == 0


def _kill_chrome_family_hard():
    """
    Robust cleanup for container:
    1) Kill anything that belongs to the Xvfb DISPLAY
    2) Kill chrome/driver families (TERM then KILL)
    3) Wait briefly and ensure gone
    """
    # 1) Prefer DISPLAY-scoped kill (only affects GUI launched on :99)
    _run_sh(
        f"pkill -TERM -f 'DISPLAY={DISPLAY}' 2>/dev/null || true; "
        f"pkill -TERM -f 'XDG_RUNTIME_DIR=/tmp/runtime-root' 2>/dev/null || true; true"
    )

    # 2) Standard families (TERM)
    _run_sh(
        "pkill -TERM -f 'undetected_chromedriver' 2>/dev/null || true; "
        "pkill -TERM -f 'chromedriver' 2>/dev/null || true; "
        "pkill -TERM -f 'google-chrome' 2>/dev/null || true; "
        "pkill -TERM -f 'chrome_crashpad' 2>/dev/null || true; "
        "pkill -TERM -f '\\bchrome\\b' 2>/dev/null || true; true"
    )

    time.sleep(0.4)

    # (KILL)
    _run_sh(
        f"pkill -KILL -f 'DISPLAY={DISPLAY}' 2>/dev/null || true; "
        "pkill -KILL -f 'undetected_chromedriver' 2>/dev/null || true; "
        "pkill -KILL -f 'chromedriver' 2>/dev/null || true; "
        "pkill -KILL -f 'google-chrome' 2>/dev/null || true; "
        "pkill -KILL -f 'chrome_crashpad' 2>/dev/null || true; "
        "pkill -KILL -f '\\bchrome\\b' 2>/dev/null || true; true"
    )

    # 3) Verify a short moment
    deadline = time.time() + 2.0
    while time.time() < deadline:
        if not (
            _has_any_process("undetected") or
            _has_any_process("chromedriver") or
            _has_any_process("google-chrome") or
            _has_any_process("chrome_crashpad") or
            _has_any_process(" chrome")
        ):
            return
        time.sleep(0.15)


def _schedule_cleanup_after_done_if_success(marker: dict | None):
    """
    Triggered from /api/auth/status (polling) with NO extra front call.
    Only cleanup if success==True (=> secrets exist).
    """
    global cleanup_scheduled
    if not marker or not marker.get("success"):
        return

    with cleanup_lock:
        if cleanup_scheduled:
            return
        cleanup_scheduled = True

    def _job():
        time.sleep(1.0)  # avoid UI flash
        try:
            _log("[cleanup] killing chrome (sync)...")
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


def _ensure_auth_persisted_like_start_sh():
    DATA_AUTH_DIR.mkdir(parents=True, exist_ok=True)

    try:
        is_empty = not any(DATA_AUTH_DIR.iterdir())
    except Exception:
        is_empty = True

    if is_empty and AUTH_DIR.exists() and AUTH_DIR.is_dir():
        _log("[persist] copying /app/Auth -> /data/Auth (first run)")
        for item in AUTH_DIR.iterdir():
            dst = DATA_AUTH_DIR / item.name
            if item.is_dir():
                shutil.copytree(item, dst, dirs_exist_ok=True)
            else:
                shutil.copy2(item, dst)

    try:
        if AUTH_DIR.is_symlink() or AUTH_DIR.resolve() == DATA_AUTH_DIR.resolve():
            return
    except Exception:
        pass

    if AUTH_DIR.exists() and not AUTH_DIR.is_symlink():
        _log("[persist] replacing /app/Auth with symlink to /data/Auth")
        shutil.rmtree(AUTH_DIR)

    try:
        AUTH_DIR.parent.mkdir(parents=True, exist_ok=True)
        AUTH_DIR.symlink_to(DATA_AUTH_DIR, target_is_directory=True)
    except Exception as e:
        _log(f"[persist] WARN: failed to symlink Auth dir: {e}")


def _ensure_x_started():
    global xvfb_proc, wm_proc, vnc_proc, ws_proc

    _ensure_runtime_env()
    _ensure_auth_persisted_like_start_sh()

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


def _force_regen():
    try:
        if SECRETS_FILE.exists():
            SECRETS_FILE.unlink()
    except Exception:
        pass
    try:
        p = DATA_AUTH_DIR / "secrets.json"
        if p.exists():
            p.unlink()
    except Exception:
        pass


def _secrets_exist() -> bool:
    return SECRETS_FILE.exists()


def _clear_done_marker():
    if AUTH_DONE_MARKER.exists():
        try:
            AUTH_DONE_MARKER.unlink()
        except Exception:
            pass


def _write_done_marker(success: bool, error: str | None):
    AUTH_DONE_MARKER.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "success": bool(success),
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


def _auth_only_job():
    global auth_state, auth_started_ts

    success = False
    err = None

    try:
        _ensure_x_started()
        _force_regen()
        _clear_done_marker()

        if str(TOOLS_DIR) not in os.sys.path:
            os.sys.path.insert(0, str(TOOLS_DIR))

        from Auth.aas_token_retrieval import get_aas_token
        from Auth.adm_token_retrieval import get_adm_token
        from Auth.username_provider import get_username

        orig_input = builtins.input
        builtins.input = lambda *args, **kwargs: ""

        try:
            _log("[auth] get_aas_token()...")
            _ = get_aas_token()

            _kick_chrome_into_view()

            _log("[auth] get_adm_token(username)...")
            _ = get_adm_token(get_username())
        finally:
            builtins.input = orig_input

        if _wait_for_secrets(AUTH_WAIT_SECONDS):
            success = True
            _log("[auth] secrets.json detected -> success")
        else:
            err = f"Timeout: secrets.json not created after {AUTH_WAIT_SECONDS}s"
            _log(f"[auth] {err}")

    except Exception:
        err = traceback.format_exc()
        _log("[auth] EXCEPTION:\n" + err)

    finally:
        _write_done_marker(success=success, error=err)
        auth_state = "idle"
        auth_started_ts = None
        _log("[auth] done marker written")


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
        return JSONResponse(
            {"ok": False, "error": "noVNC backend not reachable (is it started?)"},
            status_code=503
        )


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
