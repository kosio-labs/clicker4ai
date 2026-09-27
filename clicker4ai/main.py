"""FastAPI app: auth, REST wrappers over rpc, WebSockets, static frontend."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import VERSION, files, passkey, quiet, rpc, sdk_update
from .auth import (DeviceStore, DevicesUnreadable, device_name_from_ua, needs_passkey,
                   redeem_pairing_code)
from .config import Config
from .rpc import RpcContext, RpcError
from .scope import Scope, device_scope, incognito_home
from .sessions import (BUSY_STATES, REWIND_MAX, RunnerLimit, SessionManager,
                       suggestion_paths)
from .term_attach import attach_terminal
from .terminal import TerminalManager

log = logging.getLogger("clicker4ai.files")

WEB_DIR = Path(__file__).resolve().parent / "web"

# Files whose change means the running page is out of date. The phone keeps
# app.js in memory, so a restart alone changes nothing there — only a page
# load does. Stamping them on every hello and pong lets the app notice by
# itself, whether it just connected or has been open for hours.
ASSET_FILES = ("index.html", "app.js", "transport.js", "style.css")


def asset_stamp() -> str:
    """Short digest of the frontend's size+mtime. Computed per call, so a
    frontend-only edit (which needs no restart) is picked up too."""
    h = hashlib.sha256(VERSION.encode())
    for name in ASSET_FILES:
        try:
            st = (WEB_DIR / name).stat()
            h.update(f"{name}:{st.st_size}:{st.st_mtime_ns}".encode())
        except OSError:
            h.update(f"{name}:-".encode())
    return h.hexdigest()[:12]

# Every `claude` this server starts (SDK sessions and True View) inherits
# this environment: no telemetry, error reporting, auto-updater or other
# non-essential traffic — only the API itself. An explicit value in the
# server's environment wins.
os.environ.setdefault("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1")
# A server started or restarted from a Claude Code tool call (pm2 included)
# carries CLAUDE_CODE_CHILD_SESSION=1; `claude` then treats itself as a
# child and turns transcript saving off ("Transcript saving is off" in True
# View). The sessions this server starts are top-level, so drop the marker.
os.environ.pop("CLAUDE_CODE_CHILD_SESSION", None)

config = Config.load()
devices = DeviceStore()
manager: SessionManager | None = None
terms = TerminalManager()


# How often incognito chats are checked for idleness and for a device that
# is gone. A revoke from the CLI happens in another process, so this is also
# how quickly its incognito chat disappears.
INCOGNITO_SWEEP_SECS = 60


async def _incognito_sweeper(m: SessionManager) -> None:
    while True:
        with contextlib.suppress(Exception):
            known = {d["id"] for d in devices.list()}
            # an unreadable devices.json lists nobody: every chat would go
            if devices.read_error is None:
                await m.incognito_sweep(known)
        await asyncio.sleep(INCOGNITO_SWEEP_SECS)


def _busy(m: SessionManager) -> int:
    """What a restart would interrupt: sessions in a turn (or waiting on a
    permission) and every open True View terminal, whose turn we cannot see."""
    return (sum(1 for r in m.runners.values() if r.state in BUSY_STATES or r.pending)
            + sum(1 for t in list(terms.terms.values()) if t.alive))


@asynccontextmanager
async def lifespan(app: FastAPI):
    global manager
    manager = SessionManager(max_runners=config.max_runners)
    manager.on_delete = lambda sid: terms.close(sid, "Session deleted")
    manager.terminal_open = lambda sid: terms.get(sid) is not None
    manager.terminal_running = terms.running
    manager.close_terminal = terms.close
    terms.on_exit = lambda sid: manager.terminal_closed(sid)
    sdk_update.WATCH.busy = lambda: _busy(manager)
    tasks = [asyncio.create_task(_incognito_sweeper(manager)),
             asyncio.create_task(sdk_update.WATCH.loop()),
             asyncio.create_task(sdk_update.WATCH.idle_restarter())]
    yield
    for t in tasks:
        t.cancel()
    terms.close_all()
    await manager.shutdown()


app = FastAPI(title="Clicker4AI", lifespan=lifespan)

# Device-session cookie. Behind an https public URL it is Secure with the
# __Host- prefix (no Domain, Path=/, https only); on plain http a plain name.
COOKIE_PLAIN = "c4ai_session"
COOKIE_HOST = "__Host-c4ai_session"
# The server enforces the 30-day sliding idle expiry; the cookie itself just
# has to outlive it (browsers cap Max-Age at 400 days).
COOKIE_MAX_AGE = 365 * 24 * 3600
_cookie_secure = False

# Global throttle on failed logins (pairing codes are single use and live
# 2 minutes; this caps guessing across all clients).
LOGIN_FAIL_LIMIT = 10
LOGIN_FAIL_WINDOW = 60.0
_login_failures: deque[float] = deque()


def configure(public_url: str | None, port: int) -> None:
    """Called by the CLI before serving: an https public URL switches the
    cookie to Secure + __Host- prefix, and pins the passkey RP ID. `port`
    is the one actually served (`serve --port` wins over config.json)."""
    global _cookie_secure
    _cookie_secure = bool(public_url) and public_url.startswith("https://")
    passkey.configure(public_url, port)
    # only where a passkey can wake it again
    devices.idle_lock_secs = config.lock_idle_minutes * 60 if passkey.available() else 0


def _cookie_name() -> str:
    return COOKIE_HOST if _cookie_secure else COOKIE_PLAIN


def _login_throttled(now: float) -> bool:
    while _login_failures and now - _login_failures[0] > LOGIN_FAIL_WINDOW:
        _login_failures.popleft()
    return len(_login_failures) >= LOGIN_FAIL_LIMIT


LOCKED_STATUS = 423   # "Locked": the cookie is fine, the session is asleep
# "Precondition Required": the device must register a passkey first
PASSKEY_REQUIRED_STATUS = 428


async def _json_body(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="expected a JSON object")
    return body


def _passkey_call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except passkey.PasskeyError as e:
        raise HTTPException(status_code=400, detail=str(e))


def _refused_ws_code() -> int:
    """Close code for a cookie verify() refused: 4503 while devices.json
    is unreadable (the cookie may be fine; the app shows a server error
    and retries), else 4401 (sign in again)."""
    return 4503 if devices.read_error is not None else 4401


async def require_device(request: Request) -> dict:
    """The device behind the cookie, locked or not. Only the lock/unlock
    routes use this; everything else goes through require_unlocked or
    require_auth."""
    device = devices.verify(request.cookies.get(_cookie_name()))
    if device is None:
        if devices.read_error is not None:
            raise HTTPException(status_code=503, detail="devices.json unreadable")
        raise HTTPException(status_code=401, detail="unauthorized")
    return device


async def require_unlocked(request: Request) -> dict:
    """An awake device, even one that still owes a passkey: only the
    passkey registration routes use this."""
    device = await require_device(request)
    if device.get("locked"):
        raise HTTPException(status_code=LOCKED_STATUS, detail="locked")
    return device


async def require_auth(request: Request) -> dict:
    device = await require_unlocked(request)
    if needs_passkey(device):
        raise HTTPException(status_code=PASSKEY_REQUIRED_STATUS, detail="passkey_required")
    return device


def _ctx(device: dict | None) -> RpcContext:
    """Per-request rpc context. Built on every request/message so CLI
    changes (revoke, set-roots, set-manage) and a step-up that has just
    expired apply immediately."""
    return RpcContext(devices, device_scope(config.allowed_roots, device), device,
                      needs_stepup=bool(device) and passkey.needs_stepup(device))


def mgr() -> SessionManager:
    assert manager is not None
    return manager


# ---------------- CSRF guard ----------------

# An upload is a raw body, not JSON. It carries a custom header instead,
# which a cross-site page cannot send without a CORS preflight either.
UPLOAD_PATH = "/api/files/upload"
UPLOAD_HEADER = "x-c4ai-upload"

@app.middleware("http")
async def same_origin_api(request: Request, call_next):
    """No /api/ request from another site, before the cookie is even looked
    at. SameSite=Lax still sends it from a sibling-subdomain page (an
    <img> or a fetch — CORS hides the answer, not the request), and
    verify() counts every request as device activity: such a page could
    hold off the idle lock. Sec-Fetch-Site where the browser sends it
    (none = typed URL), else the Origin check the WebSockets use; without
    either (CLI, curl) it passes."""
    if request.url.path.startswith("/api/"):
        site = request.headers.get("sec-fetch-site")
        bad = site in ("same-site", "cross-site") if site else not _origin_ok(request)
        if bad:
            return JSONResponse({"detail": "cross-site request"}, status_code=403)
    return await call_next(request)


@app.middleware("http")
async def require_json_body(request: Request, call_next):
    """State-changing /api/ requests must be application/json. A cross-site
    form or no-cors fetch cannot send that without a CORS preflight, and
    request.json() alone ignores the Content-Type."""
    if request.method in ("POST", "PUT", "PATCH", "DELETE") \
            and request.url.path.startswith("/api/") \
            and not (request.url.path == UPLOAD_PATH
                     and request.headers.get(UPLOAD_HEADER) == "1"):
        ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if ctype != "application/json":
            return JSONResponse({"detail": "Content-Type must be application/json"},
                                status_code=415)
    return await call_next(request)


# ---------------- auth ----------------

@app.post("/api/login")
async def login(request: Request, response: Response):
    """Redeem a one-time pairing code → new device session cookie."""
    now = time.monotonic()
    if _login_throttled(now):
        raise HTTPException(status_code=429,
                            detail="too many failed logins, try again in a minute")
    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="expected a JSON object")
    code = body.get("code")
    devices.check_readable()   # before the one-time code is used up
    grants = redeem_pairing_code(code) if isinstance(code, str) else None
    if grants is None:
        _login_failures.append(now)
        raise HTTPException(status_code=401, detail="invalid or expired pairing code")
    name = body.get("name")
    if not isinstance(name, str) or not name.strip():
        name = device_name_from_ua(request.headers.get("user-agent", ""))
    # grants (roots, manage_devices, terminal, files, …) come from the code
    # (set by the CLI), never from the request body
    token, _device = devices.create(name, **grants)
    response.set_cookie(
        _cookie_name(), token, max_age=COOKIE_MAX_AGE, path="/",
        httponly=True, samesite="lax", secure=_cookie_secure,
    )
    return {"ok": True}


@app.post("/api/logout")
async def logout(request: Request, response: Response):
    device = devices.verify(request.cookies.get(_cookie_name()))
    if device is not None:
        passkey.drop_elevation(device["id"])
        devices.revoke(device["id"])
        await mgr().erase_owner(device["id"])
    response.delete_cookie(_cookie_name(), path="/", httponly=True,
                           samesite="lax", secure=_cookie_secure)
    return {"ok": True}


@app.post("/api/lock")
async def lock(device: dict = Depends(require_auth)):
    """Put this device session to sleep without giving up its grants. The
    cookie stays in the browser; a passkey wakes it (see /api/unlock)."""
    if not device.get("passkey"):
        raise HTTPException(status_code=400,
                            detail="add a passkey first — nothing could unlock this")
    passkey.drop_elevation(device["id"])
    devices.set_locked(device["id"], True)
    return {"ok": True}


@app.post("/api/unlock/begin")
async def unlock_begin(device: dict = Depends(require_device)):
    return _passkey_call(passkey.unlock_options, device)


@app.post("/api/unlock/finish")
async def unlock_finish(request: Request, device: dict = Depends(require_device)):
    """A passkey wakes the session the cookie already points at, so this
    hands out nothing the browser did not already hold."""
    body = await _json_body(request)
    _passkey_call(passkey.unlock_verify, device, body.get("credential"))
    devices.set_locked(device["id"], False)
    return {"ok": True}


# ---------------- passkeys ----------------
# Login (a passkey creates a new, empty device session) and step-up (a
# fresh confirmation for risky actions). See passkey.py: a credential
# carries no grants, so signing in with one can never widen access —
# folders and grants come from the CLI.

@app.get("/api/passkey")
async def passkey_status(request: Request):
    """Whether this origin can use passkeys and whether any is registered
    — the login screen asks before it is authenticated."""
    device = devices.verify(request.cookies.get(_cookie_name()))
    return passkey.status(device)


@app.post("/api/passkey/register/begin")
async def passkey_register_begin(device: dict = Depends(require_unlocked)):
    if passkey.needs_stepup(device):
        raise HTTPException(status_code=403, detail="stepup_required")
    return _passkey_call(passkey.registration_options, device)


@app.post("/api/passkey/register/finish")
async def passkey_register_finish(request: Request,
                                  device: dict = Depends(require_unlocked)):
    if passkey.needs_stepup(device):
        raise HTTPException(status_code=403, detail="stepup_required")
    body = await _json_body(request)
    name = body.get("name") if isinstance(body.get("name"), str) else ""
    cred = _passkey_call(passkey.register_finish, device,
                         body.get("credential"), name)
    # from now on this device session confirms risky actions with the passkey
    devices.set_passkey(device["id"], True)
    passkey.stepup_finish(device)
    return {"ok": True, "id": cred["id"], "name": cred["name"]}


@app.post("/api/passkey/login/begin")
async def passkey_login_begin():
    return _passkey_call(passkey.login_options)


@app.post("/api/passkey/login/finish")
async def passkey_login_finish(request: Request, response: Response):
    """A verified passkey creates an *empty* device session: no folders,
    no True View, no device management. The passkey proves who you are;
    what the device may reach is then granted from the CLI."""
    now = time.monotonic()
    if _login_throttled(now):
        raise HTTPException(status_code=429,
                            detail="too many failed logins, try again in a minute")
    body = await _json_body(request)
    try:
        cred = passkey.login_finish(body.get("credential"))
    except passkey.PasskeyError as e:
        _login_failures.append(now)
        raise HTTPException(status_code=401, detail=str(e))
    name = body.get("name")
    if not isinstance(name, str) or not name.strip():
        name = device_name_from_ua(request.headers.get("user-agent", ""))
    # [] = no folders yet; a passkey device keeps needing one
    token, device = devices.create(name, roots=[], require_passkey=True)
    devices.set_passkey(device["id"], True)
    passkey.record_use(cred["id"], device["id"])
    passkey.stepup_finish(device)   # the login itself was a confirmation
    response.set_cookie(
        _cookie_name(), token, max_age=COOKIE_MAX_AGE, path="/",
        httponly=True, samesite="lax", secure=_cookie_secure,
    )
    return {"ok": True}


@app.post("/api/passkey/require")
async def passkey_require(device: dict = Depends(require_auth)):
    """Turn the passkey requirement on for this device. Only on: turning
    it off is a CLI decision (`devices set <id> --no-passkey`), or a stolen
    cookie could shed it."""
    devices.set_require_passkey(device["id"], True)
    return {"ok": True, "passkey_required": needs_passkey(
        {**device, "require_passkey": True})}


@app.post("/api/passkey/stepup/begin")
async def passkey_stepup_begin(device: dict = Depends(require_auth)):
    return _passkey_call(passkey.stepup_options, device)


@app.post("/api/passkey/stepup/finish")
async def passkey_stepup_finish(request: Request,
                                device: dict = Depends(require_auth)):
    body = await _json_body(request)
    until = _passkey_call(passkey.stepup_verify, device, body.get("credential"))
    return {"ok": True, "until": until}


@app.get("/api/passkeys")
async def passkey_list(device: dict = Depends(require_auth)):
    """The passkeys this device may see: the ones it has signed in with,
    or all of them when it may manage devices."""
    keys = [k for k in passkey.list_public() if passkey.may_touch(k, device)]
    for k in keys:
        k["current_device"] = device["id"] in k["used_by"]
    return {"passkeys": keys, "can_manage": bool(device.get("manage_devices"))}


@app.get("/api/passkeys/signal")
async def passkey_signal(device: dict = Depends(require_auth)):
    """The credential ids this server still accepts, for the browser to
    prune the keychain with (see passkey.signal_payload)."""
    return passkey.signal_payload()


@app.delete("/api/passkeys/{cred_id:path}")
async def passkey_delete(cred_id: str, device: dict = Depends(require_auth)):
    keys = {k["id"]: k for k in passkey.list_public()}
    key = keys.get(cred_id)
    if key is None:
        raise HTTPException(status_code=404, detail="no such passkey")
    if not passkey.may_touch(key, device):
        raise HTTPException(
            status_code=403,
            detail="only a device that signed in with this passkey may remove it")
    if passkey.needs_stepup(device):
        raise HTTPException(status_code=403, detail="stepup_required")
    passkey.remove(cred_id)
    # every device that used it and has no other key: nothing left to
    # confirm or unlock with (a require_passkey device then has to add one)
    devices.clear_passkey(key["used_by"], passkey.devices_with_keys())
    return {"ok": True}


@app.patch("/api/passkeys/{cred_id:path}")
async def passkey_rename(cred_id: str, request: Request,
                         device: dict = Depends(require_auth)):
    """Rename a passkey: freely one this device has signed in with; any
    other only with the manage grant and a fresh passkey confirmation, as
    for renaming another device."""
    key = next((k for k in passkey.list_public() if k["id"] == cred_id), None)
    if key is None:
        raise HTTPException(status_code=404, detail="no such passkey")
    if not passkey.may_touch(key, device):
        raise HTTPException(
            status_code=403,
            detail="only a device that signed in with this passkey may rename it")
    if device["id"] not in key["used_by"] and passkey.needs_stepup(device):
        raise HTTPException(status_code=403, detail="stepup_required")
    body = await _json_body(request)
    name = body.get("name") if isinstance(body.get("name"), str) else ""
    return {"ok": True, "name": _passkey_call(passkey.rename, cred_id, name)}


# ---------------- REST wrappers over the rpc method table ----------------
# Kept for curl and local debugging; the PWA talks rpc over /ws.

async def _rest(device: dict, method: str, params: dict):
    try:
        return await rpc.dispatch(mgr(), config, _ctx(device), method, params)
    except RpcError as e:
        raise HTTPException(status_code=rpc.HTTP_STATUS.get(e.code, 500),
                            detail=e.message)


@app.get("/api/state")
async def state(device: dict = Depends(require_auth)):
    return await _rest(device, "state", {})


@app.post("/api/sessions")
async def create_session(request: Request, device: dict = Depends(require_auth)):
    return await _rest(device, "sessions.create", await _json_body(request))


@app.post("/api/sessions/{sid}/stop")
async def stop_session(sid: str, device: dict = Depends(require_auth)):
    return await _rest(device, "sessions.stop", {"sid": sid})


@app.delete("/api/sessions/{sid}")
async def delete_session(sid: str, device: dict = Depends(require_auth)):
    return await _rest(device, "sessions.delete", {"sid": sid})


@app.get("/api/projects")
async def api_projects(device: dict = Depends(require_auth)):
    return await _rest(device, "projects.recent", {})


@app.get("/api/projects/sessions")
async def api_project_sessions(cwd: str, device: dict = Depends(require_auth)):
    return await _rest(device, "projects.sessions", {"cwd": cwd})


@app.get("/api/projects/preview")
async def api_session_preview(session_id: str, cwd: str,
                              device: dict = Depends(require_auth)):
    return await _rest(device, "projects.preview", {"session_id": session_id, "cwd": cwd})


@app.get("/api/browse")
async def api_browse(path: str | None = None, device: dict = Depends(require_auth)):
    return await _rest(device, "browse", {"path": path})


@app.get("/api/library")
async def api_library(cwd: str | None = None, device: dict = Depends(require_auth)):
    return await _rest(device, "library", {"cwd": cwd})


# ---------------- project files (files.py) ----------------

# a file straight from a project: never run as a page on this origin
FILE_HEADERS = {"Content-Security-Policy": "sandbox", "X-Content-Type-Options": "nosniff",
                "Cross-Origin-Resource-Policy": "same-origin", "Cache-Control": "no-store"}
FILES_DENIED = "this device may not browse files (devices set --files on the server)"


def _files_http(e: files.FilesError) -> HTTPException:
    status = {"forbidden": 403, "not_found": 404, "exists": 409,
              "too_large": 413}.get(e.code, 400)
    return HTTPException(status_code=status, detail=e.message)


@app.get("/api/files/raw")
async def files_raw(path: str, download: int = 0, device: dict = Depends(require_auth)):
    """One file: images inline (the app's <img>), everything else, and
    anything with ?download=1, as an attachment."""
    if not files.can_view(device):
        raise HTTPException(status_code=403, detail=FILES_DENIED)
    try:
        p = files.jail(path, _ctx(device).scope)
    except files.FilesError as e:
        raise _files_http(e)
    if not p.is_file():
        raise HTTPException(status_code=404, detail="no such file")
    kind = files.image_type(p)
    inline = bool(kind) and not download
    return FileResponse(p, media_type=kind or "application/octet-stream",
                        filename=p.name,
                        content_disposition_type="inline" if inline else "attachment",
                        headers=FILE_HEADERS)


@app.put(UPLOAD_PATH)
async def files_upload(request: Request, dir: str, name: str,
                       device: dict = Depends(require_auth)):
    """Raw request body → dir/name. An existing name is a 409 before any
    byte is read; the app then asks for another name."""
    if request.headers.get(UPLOAD_HEADER) != "1":
        raise HTTPException(status_code=400, detail="missing upload header")
    if not files.can_upload(device):
        raise HTTPException(status_code=403, detail="this device may not upload "
                            "(devices set --files-upload on the server)")
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > files.UPLOAD_MAX:
        raise _files_http(files.FilesError("too_large", "file too large"))
    try:
        target = await asyncio.to_thread(files.upload_target, dir, name, _ctx(device).scope)
        up = await asyncio.to_thread(files.Upload, target)
    except files.FilesError as e:
        raise _files_http(e)
    except OSError as e:
        raise HTTPException(status_code=400, detail=e.strerror or "cannot write here")
    try:
        async for chunk in request.stream():
            await asyncio.to_thread(up.write, chunk)
        await asyncio.to_thread(up.finish)
    except files.FilesError as e:
        up.abort()
        raise _files_http(e)
    except BaseException:
        up.abort()
        raise
    log.info("upload %s (%d bytes) by device %s", target, up.size, device.get("id"))
    return {"ok": True, "path": str(target), "size": up.size}


# ---------------- websocket ----------------

class WSConn:
    def __init__(self, ws: WebSocket, scope: Scope):
        self.ws = ws
        self.scope = scope   # refreshed on every incoming message
        self.subs: set[str] = set()
        self.queue: asyncio.Queue = asyncio.Queue()
        self._sender: asyncio.Task | None = None

    def push(self, payload: dict) -> None:
        self.queue.put_nowait(payload)

    def push_sessions(self, snaps: list[dict]) -> None:
        """Session list, limited to what this connection's device may see."""
        self.push({"type": "sessions",
                   "sessions": [s for s in snaps if self.scope.contains(s["cwd"])]})

    async def _send_loop(self) -> None:
        while True:
            payload = await self.queue.get()
            await self.ws.send_text(json.dumps(payload, default=str))

    def start(self) -> None:
        self._sender = asyncio.create_task(self._send_loop())

    async def close(self) -> None:
        if self._sender:
            self._sender.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._sender


def _origin_ok(ws: WebSocket | Request) -> bool:
    """Same-origin allowlist for browser WS upgrades (and /api/ requests
    from a browser without Sec-Fetch-Site). Non-browser clients
    (no Origin header) pass — cross-site WebSocket hijacking is a browser
    attack, and browsers always send Origin on WS upgrades."""
    origin = ws.headers.get("origin")
    if origin is None:
        return True
    try:
        o = urlparse(origin)
    except ValueError:
        return False
    if o.scheme not in ("http", "https") or not o.netloc:
        return False
    host = ws.headers.get("host", "")

    def norm(netloc: str, scheme: str) -> str:
        if ":" in netloc:
            return netloc
        return f"{netloc}:{443 if scheme == 'https' else 80}"

    return norm(o.netloc, o.scheme) == norm(host, o.scheme)


async def _ws_auth(ws: WebSocket) -> tuple[str, dict] | None:
    """Cookie-only WS auth (no tokens in query strings) + Origin allowlist.
    Accepts the socket first so the browser can observe the close code.
    Returns (device token, device); callers re-verify the token on every
    incoming message so a revoked device is dropped (close 4401) within
    one ping and scope changes apply immediately."""
    await ws.accept()
    # Origin first: verify() counts as device activity, which a same-site
    # page must not be able to refresh (it would defeat the idle lock)
    if not _origin_ok(ws):
        await ws.close(code=4403)
        return None
    token = ws.cookies.get(_cookie_name())
    device = devices.verify(token)
    if device is None:
        await ws.close(code=_refused_ws_code())
        return None
    if device.get("locked"):
        await ws.close(code=4423)   # cookie fine, session asleep
        return None
    if needs_passkey(device):
        await ws.close(code=4428)   # register a passkey first (REST)
        return None
    return token, device


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    auth = await _ws_auth(ws)
    if auth is None:
        return
    token, device = auth
    m = mgr()
    conn = WSConn(ws, _ctx(device).scope)
    conn.start()
    m.conns.add(conn)
    conn.push({"type": "hello", "version": VERSION, "assets": asset_stamp(),
               "rewind_max": REWIND_MAX})
    conn.push_sessions(m.snapshots())
    try:
        while True:
            raw = await ws.receive_text()
            device = devices.verify(token)
            if device is None:
                await ws.close(code=_refused_ws_code())
                break
            if device.get("locked"):
                await ws.close(code=4423)
                break
            if needs_passkey(device):
                await ws.close(code=4428)
                break
            ctx = _ctx(device)
            conn.scope = ctx.scope
            # scope may have shrunk (CLI set-roots): drop subscriptions outside it
            conn.subs = {sid for sid in conn.subs
                         if (r := m.get(sid)) is not None and ctx.scope.contains(r.cwd)}
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            try:
                await handle_ws(m, conn, ctx, msg)
            except Exception as e:
                conn.push({"type": "error", "message": str(e),
                           "ref": msg.get("type")})
    except WebSocketDisconnect:
        pass
    finally:
        m.conns.discard(conn)
        await conn.close()


def _suggestions_in_scope(runner, request_id: str, picked: list, scope: Scope) -> list:
    """Drop the "Always" suggestions that would open a path outside this
    device's folders for the rest of the session (an edit of ~/.bashrc
    offers the whole of ~). The one-off Allow still goes through; what was
    left out is said in the chat."""
    info = runner.pending_info.get(request_id)
    if info is None:
        return picked
    sugg = info["context"].suggestions or []
    kept, skipped = [], []
    for i in picked:
        if not (isinstance(i, int) and 0 <= i < len(sugg)):
            continue
        outside = [p for p in suggestion_paths(sugg[i], runner.cwd) if not scope.contains(p)]
        if outside:
            skipped += outside
        else:
            kept.append(i)
    if skipped:
        runner.notice("Always: left out access outside this device's folders ("
                      + ", ".join(sorted(set(skipped))) + ") — allowed once", "warn")
    return kept


async def handle_ws(m: SessionManager, conn: WSConn, ctx: RpcContext,
                    msg: dict) -> None:
    mtype = msg.get("type")
    if mtype == "ping":
        # The stamp also travels here, not just in hello: a page that stays
        # connected would otherwise never learn the frontend changed.
        conn.push({"type": "pong", "assets": asset_stamp()})
        return
    if mtype == "rpc":
        rid = msg.get("id")
        try:
            result = await rpc.dispatch(m, config, ctx, str(msg.get("method") or ""),
                                        msg.get("params") or {})
            conn.push({"type": "rpc_result", "id": rid, "ok": True,
                       "result": result})
        except RpcError as e:
            err = {"code": e.code, "message": e.message}
            if e.data:
                err["data"] = e.data
            conn.push({"type": "rpc_result", "id": rid, "ok": False,
                       "error": err})
        except Exception as e:
            conn.push({"type": "rpc_result", "id": rid, "ok": False,
                       "error": {"code": "internal", "message": str(e)}})
        return
    if mtype == "sessions":
        conn.push_sessions(m.snapshots())
        return

    sid = msg.get("session_id", "")
    runner = m.get(sid)
    if runner is not None and not ctx.scope.contains(runner.cwd):
        runner = None   # outside this device's scope: behave as if missing

    if mtype == "attach":
        if runner is None:
            conn.push({"type": "error", "message": "no such session", "ref": sid})
            return
        runner.ensure_events_loaded()
        since = int(msg.get("since_seq") or 0)
        events = [e for e in runner.events if (e.get("seq") or 0) > since]
        conn.subs.add(sid)
        conn.push({
            "type": "attached", "session_id": sid,
            "snapshot": runner.snapshot(),
            "status": {"state": runner.state, "detail": runner.detail},
            "context": runner.context,
            "meta": runner.meta,
            "events": events,
            "btw": runner.btw,
        })
        return

    if mtype == "detach":
        conn.subs.discard(sid)
        return

    if runner is None:
        conn.push({"type": "error", "message": "no such session", "ref": sid})
        return

    if mtype in ("send", "btw"):
        # each asks Claude something new (compact only condenses what is
        # there, a cheap last step while the cache is warm, so it passes);
        # checked before a True View is closed below, so a refused prompt
        # leaves the terminal alone
        refused = quiet.refusal(ctx.device)
        if refused:
            conn.push({"type": "error", "code": "quiet_hours", "message": refused,
                       "ref": sid})
            return

    if mtype in ("send", "compact", "clear") and runner._term_opening:
        # a True View terminal is starting and nothing is there to close
        # yet: resuming now would put a second process on the transcript
        conn.push({"type": "error", "code": "term_opening", "ref": sid,
                   "message": "True View is opening — try again in a moment"})
        return

    if mtype in ("send", "compact", "clear") and not runner.incognito \
            and (runner.client is None or mtype == "clear"):
        # a `claude` process starts here (clear always restarts it): a
        # folder with project settings needs the device's trust first
        try:
            await rpc.check_trust(runner.cwd)
        except rpc.RpcError as err:
            conn.push({"type": "error", "code": err.code, "message": err.message,
                       "data": err.data, "ref": sid})
            return

    if mtype in ("send", "compact", "clear"):
        # The chat is taking the session back — a live True View terminal
        # would be left behind the transcript, so close it first, and let
        # its `claude` finish (also one closed by a stop just before):
        # two processes must never resume one transcript.
        terms.close(sid, "Continued from the app")
        await terms.wait_closed(sid)

    if mtype == "send":
        # a message to a stopped session restarts it, so the limit applies
        # here too — answer with the same candidate list the app knows
        try:
            await m.make_room(runner)
        except RunnerLimit as e:
            err = rpc.runner_limit_error(e, ctx.scope)
            conn.push({"type": "error", "code": err.code, "message": err.message,
                       "data": err.data, "ref": sid})
            return
        asyncio.create_task(_safe(runner.send(msg.get("text", "")), runner))
    elif mtype == "interrupt":
        asyncio.create_task(_safe(runner.interrupt(), runner))
    elif mtype == "permission":
        # "Always allow" widens what the session may do without asking
        # again, so on a passkey device it needs a fresh confirmation.
        # The plain one-off Allow/Deny never does.
        if msg.get("apply_suggestions") and ctx.needs_stepup:
            conn.push({"type": "error", "code": "stepup_required",
                       "message": "Confirm with your passkey to always allow",
                       "ref": msg.get("request_id", "")})
            return
        if msg.get("apply_suggestions"):
            msg = {**msg, "apply_suggestions": _suggestions_in_scope(
                runner, msg.get("request_id", ""), msg["apply_suggestions"], ctx.scope)}
        ok = runner.respond_permission(msg.get("request_id", ""), msg)
        if not ok:
            conn.push({"type": "error",
                       "message": "permission request no longer pending"})
    elif mtype == "clear":
        asyncio.create_task(_safe(runner.clear(), runner))
    elif mtype == "compact":
        asyncio.create_task(_safe(runner.compact(msg.get("instructions", "")), runner))
    elif mtype == "set_model":
        asyncio.create_task(_safe(runner.set_model(msg.get("model", "default")), runner))
    elif mtype == "set_mode":
        if runner.incognito:
            # plan mode would wait for ExitPlanMode, which an incognito chat
            # does not have; acceptEdits has nothing to accept
            conn.push({"type": "error", "message": "an incognito chat stays in "
                       "the default permission mode", "ref": sid})
            return
        asyncio.create_task(_safe(runner.set_mode(msg.get("mode", "default")), runner))
    elif mtype == "btw":
        # a side question reads the transcript and leaves the session (and
        # a True View terminal on it) alone
        try:
            runner.ask_btw(str(msg.get("question") or ""))
        except ValueError as e:
            conn.push({"type": "error", "message": str(e), "ref": sid})
    elif mtype == "btw_clear":
        runner.clear_btw()
    elif mtype == "refresh_context":
        asyncio.create_task(_safe(runner.refresh_context(), runner))


async def _safe(coro, runner) -> None:
    try:
        await coro
    except Exception as e:
        with contextlib.suppress(Exception):
            runner.notice(f"Action failed: {e}", "error")


# ---------------- true view (real claude TUI over a PTY) ----------------

TERMINAL_DENIED = "True View is not enabled for this device"
# True View is the full CLI with the user's tools and settings: it would give
# an incognito chat back everything its session leaves out (sessions.py)
INCOGNITO_DENIED = "True View is not available in an incognito chat"
STEPUP_DENIED = "Confirm with your passkey to open True View"


def _terminal_denial(device: dict, cwd: str,
                     incognito: bool = False) -> str | None:
    """Why this device may not use the session's True View, or None.
    The passkey confirmation is not checked here: it guards starting a
    terminal only (see ws_terminal), so a terminal already open is not
    torn down when the elevation expires."""
    if incognito:
        return INCOGNITO_DENIED
    if not device.get("terminal"):
        return TERMINAL_DENIED
    if not _ctx(device).scope.contains(cwd):
        return "No such session"
    return None


class _LocalTermTransport:
    """TermTransport over the local WebSocket: binary frames are raw
    terminal output, text frames are JSON control messages. Every client
    message re-verifies the device (revoked → close 4401), its True View
    grant and its scope (either lost → exit message + close 4403)."""

    def __init__(self, ws: WebSocket, token: str, device: dict, cwd: str,
                 incognito: bool):
        self.ws = ws
        self.token = token
        self.device = device   # as of the last message
        self.cwd = cwd
        self.incognito = incognito

    def quiet_exempt(self) -> bool:
        return bool(self.device.get("quiet_exempt"))

    async def send_bytes(self, data: bytes) -> None:
        await self.ws.send_bytes(data)

    async def send_json(self, obj: dict) -> None:
        await self.ws.send_json(obj)

    async def close(self, code: int = 1000) -> None:
        with contextlib.suppress(Exception):
            await self.ws.close(code=code)

    async def recv_json(self) -> dict | None:
        while True:
            try:
                msg = await self.ws.receive()
            except (WebSocketDisconnect, RuntimeError):
                return None
            if msg["type"] == "websocket.disconnect":
                return None
            device = devices.verify(self.token)
            if device is not None and (device.get("locked") or needs_passkey(device)):
                device = None       # treat like a lost session: close 4401
            denial = None if device is None else _terminal_denial(
                device, self.cwd, incognito=self.incognito)
            if device is None or denial:
                with contextlib.suppress(Exception):
                    if denial:
                        await self.ws.send_json({"type": "exit", "reason": denial})
                    await self.ws.close(code=_refused_ws_code() if device is None
                                        else 4403)
                return None
            self.device = device
            text = msg.get("text")
            if not text:
                continue
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):   # anything else is not a message
                return data


@app.websocket("/ws/term/{sid}")
async def ws_terminal(ws: WebSocket, sid: str):
    """Attach an xterm.js client to the session's PTY terminal. The body
    lives in term_attach.attach_terminal.
    Server→client: binary frames are raw terminal output; text frames are
    JSON control messages ({"type": "exit"|"pong"}). Client→server: JSON
    text frames ({"type": "input"|"resize"|"ping"|"kill"})."""
    auth = await _ws_auth(ws)
    if auth is None:
        return
    token, device = auth
    runner = mgr().get(sid)
    # "No such session" is the message attach_terminal gives for an unknown one
    denial = ("No such session" if runner is None
              else _terminal_denial(device, runner.cwd,
                                    incognito=runner.incognito))
    if denial:
        with contextlib.suppress(Exception):
            await ws.send_json({"type": "exit", "reason": denial})
            await ws.close()
        return
    # True View is a real shell: a passkey device confirms before starting
    # a new terminal process, not before joining one that already runs
    # (switching between open terminals asks nothing; the device's idle
    # lock guards those)
    await attach_terminal(mgr(), terms, sid,
                          _LocalTermTransport(ws, token, device, runner.cwd,
                                              runner.incognito),
                          spawn_denial=STEPUP_DENIED if passkey.needs_stepup(device) else None)


# ---------------- static frontend ----------------

# "no-cache" means revalidate, not "do not store": the browser still keeps
# the file and the ETag turns the next request into a cheap 304. Without it
# the iOS home-screen app may serve a stale app.js after a frontend change.
NO_CACHE = {"Cache-Control": "no-cache"}


class RevalidatingStatic(StaticFiles):
    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers.update(NO_CACHE)
        return resp


@app.get("/")
async def index():
    return FileResponse(WEB_DIR / "index.html", headers=NO_CACHE)


@app.exception_handler(DevicesUnreadable)
async def devices_unreadable(request: Request, exc: DevicesUnreadable):
    return JSONResponse({"detail": str(exc)}, status_code=503)


@app.exception_handler(404)
async def not_found(request: Request, exc):
    if request.url.path.startswith(("/api/", "/ws")):
        return JSONResponse({"detail": "not found"}, status_code=404)
    return FileResponse(WEB_DIR / "index.html", headers=NO_CACHE)


app.mount("/", RevalidatingStatic(directory=str(WEB_DIR)), name="static")
