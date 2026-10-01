"""RPC method table — the single implementation behind WS and REST.

The browser calls these over the /ws control channel:
    {type:"rpc", id, method, params}
        -> {type:"rpc_result", id, ok:true,  result}
        -> {type:"rpc_result", id, ok:false, error:{code, message}}

The REST routes in main.py are thin wrappers over the same handlers (kept
for curl and local debugging).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from claude_agent_sdk import rename_session

from . import VERSION, files, library, passkey, projects, sdk_update, search, trust
from .auth import NAME_MAX, DeviceStore
from .config import INCOGNITO_DIR, Config
from .scope import Scope, incognito_home
from .sessions import (
    MODEL_CHOICES,
    REMOTE_MODES,
    RunnerLimit,
    SessionManager,
    valid_model,
)
from .watch import valid_session_id

# same cut as the automatic title (sessions.py), so a renamed session cannot
# grow a label the cards refuse to fit
TITLE_MAX = 64


class RpcError(Exception):
    def __init__(self, code: str, message: str, data: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data or None


@dataclass
class RpcContext:
    """Who is calling: the device session behind the request, the store it
    came from and the directory scope it may reach (scope.py)."""
    devices: DeviceStore
    scope: Scope
    device: dict | None = None
    # a passkey device that has not confirmed recently (passkey.py); the
    # few methods that widen access refuse until it does
    needs_stepup: bool = False

    @property
    def device_id(self) -> str | None:
        return self.device.get("id") if self.device else None

    @property
    def manages_devices(self) -> bool:
        return bool(self.device and self.device.get("manage_devices"))

    @property
    def can_terminal(self) -> bool:
        return bool(self.device and self.device.get("terminal"))

    def require_stepup(self, what: str) -> None:
        if self.needs_stepup:
            raise RpcError("stepup_required", f"Confirm with your passkey to {what}")

    def require_confirmation(self, what: str) -> None:
        """A fresh passkey confirmation from ANY device, not only one that
        carries a passkey: a key synced to a desktop confirms there too.
        Without a key on this server (or off https) the action stays CLI-only."""
        if not passkey.available() or not passkey.has_credentials():
            raise RpcError("forbidden", f"To {what} from the app, open it at its "
                                        "https address with a key registered; "
                                        "otherwise use the server's CLI")
        if not passkey.is_elevated(self.device or {}):
            raise RpcError("stepup_required", f"Confirm with your passkey to {what}")


# rpc error code -> HTTP status, for the REST wrappers
HTTP_STATUS = {
    "bad_request": 400,
    "forbidden": 403,
    "not_found": 404,
    "unknown_method": 404,
    "stepup_required": 403,
    "runner_limit": 409,
    "untrusted": 409,
    "internal": 500,
}


def _require_str(params: dict, key: str) -> str:
    val = params.get(key)
    if not isinstance(val, str) or not val:
        raise RpcError("bad_request", f"missing required param: {key}")
    return val


def _jailed_path(raw: str, scope: Scope) -> Path:
    """Resolve a user-supplied path and require it inside the caller's scope."""
    p = scope.resolve(raw)
    if p is None:
        raise RpcError("forbidden", "outside allowed folders")
    return p


def _jailed_opt(params: dict, key: str, scope: Scope) -> str | None:
    """Optional path param: None when absent, else jailed to the scope."""
    raw = params.get(key)
    if raw is None or raw == "":
        return None
    if not isinstance(raw, str):
        raise RpcError("bad_request", f"{key} must be a string")
    return str(_jailed_path(raw, scope))


async def _require_project_session(session_id: str, cwd: str, scope: Scope) -> None:
    """The past session must belong to project `cwd` and stay in scope."""
    found = await asyncio.to_thread(projects.session_cwd, session_id, cwd)
    if found is None or not scope.contains(found):
        raise RpcError("not_found", "no such session in this project")


def _holding_runner(m: SessionManager, session_id: str, scope: Scope):
    """The runner (in scope, most recently active) whose conversation
    includes claude session `session_id`, else None."""
    held = [r for r in m.runners.values()
            if session_id in r.claude_ids and scope.contains(r.cwd)]
    return max(held, key=lambda r: r.last_active) if held else None


def _session_id(params: dict, key: str) -> str:
    val = _require_str(params, key)
    if not valid_session_id(val):
        raise RpcError("bad_request", f"invalid {key}")
    return val


def _runner(m: SessionManager, params: dict, scope: Scope):
    sid = _require_str(params, "sid")
    runner = m.get(sid)
    if runner is None or not scope.contains(runner.cwd):
        raise RpcError("not_found", "no such session")
    return runner


def runner_limit_error(e: RunnerLimit, scope: Scope) -> RpcError:
    """Turn the limit into an rpc error the app can act on: the message
    explains it, the data offers what may be stopped to make room —
    narrowed to the folders this device may see, so the refusal never
    reveals a session outside them."""
    e.candidates = [c for c in e.candidates if scope.contains(c["cwd"])]
    if e.candidates:
        message = (f"{e.limit} sessions are already running — "
                   "stop one to start another")
    else:
        message = (f"{e.limit} sessions are already running, and they are "
                   "all busy or outside this device's folders")
    return RpcError("runner_limit", message,
                    {"limit": e.limit, "candidates": e.candidates})


def untrusted_error(e: trust.Untrusted) -> RpcError:
    """The folder has project settings that would run on their own: the
    data lists them, so the app can ask the device to trust it first."""
    return RpcError("untrusted", "This folder has project settings Claude Code "
                                 "runs on its own — trust it first",
                    {"cwd": e.cwd, "items": e.items, "trusted_parent": e.parent})


async def check_trust(cwd: str) -> None:
    """Raise the rpc error unless a `claude` process may start in cwd."""
    try:
        await asyncio.to_thread(trust.check, cwd)
    except trust.Untrusted as e:
        raise untrusted_error(e) from None


def _snapshots(m: SessionManager, scope: Scope) -> list[dict]:
    return [s for s in m.snapshots() if scope.contains(s["cwd"])]


# ---------------- methods ----------------

async def _state(m: SessionManager, config: Config, params: dict,
                 ctx: RpcContext) -> dict:
    return {
        "ok": True,
        "version": VERSION,
        "home": str(Path.home()),
        "sessions": _snapshots(m, ctx.scope),
        "defaults": {"models": MODEL_CHOICES, "modes": REMOTE_MODES},
        "can_terminal": ctx.can_terminal,   # True View grant (pair --terminal)
        "passkey_device": bool(ctx.device and ctx.device.get("passkey")),
        "require_passkey": bool(ctx.device and ctx.device.get("require_passkey")),
        # a device signed in with a passkey starts with no folders at all
        # until the CLI gives it some; the app says so instead of looking broken
        "has_roots": bool(ctx.scope.roots),
        # any device with a folder may open one incognito chat
        "can_incognito": bool(ctx.scope.private),
        "can_manage": ctx.manages_devices,
        # project files without Claude (files.py; devices set --files)
        "can_files": files.can_view(ctx.device),
        "can_upload": files.can_upload(ctx.device),
        # a dot on the menu: a newer SDK or a restart to finish one
        # (`claude -v` behind it, cached 60 s: off the event loop)
        "sdk_attention": (ctx.manages_devices
                          and await asyncio.to_thread(sdk_update.WATCH.attention)),
    }


async def _sessions_create(m: SessionManager, config: Config, params: dict,
                           ctx: RpcContext) -> dict:
    cwd = _jailed_path(_require_str(params, "cwd"), ctx.scope)
    if cwd.is_relative_to(INCOGNITO_DIR):
        # only sessions.incognito opens these, never a second runner or a
        # resume that would outlive the erase
        raise RpcError("forbidden", "an incognito folder opens only as its incognito chat")
    if not cwd.is_dir():
        raise RpcError("bad_request", "cwd is not a directory")
    model = params.get("model") or "default"
    if not valid_model(model):
        raise RpcError("bad_request", "invalid model")
    mode = params.get("mode") or "default"
    if mode not in REMOTE_MODES:
        raise RpcError("bad_request", f"permission mode {mode} is not available "
                                      "in remote control")
    resume = params.get("resume") or None
    if resume is not None:
        if not valid_session_id(resume):
            raise RpcError("bad_request", "invalid resume id")
        await _require_project_session(resume, str(cwd), ctx.scope)
        if not params.get("fork"):
            # a second runner on the same claude session would only
            # duplicate the list entry: open the one that exists
            held = _holding_runner(m, resume, ctx.scope)
            if held:
                return held.snapshot()
    else:
        # a new session starts its process right away (a resumed one with
        # its first message, checked on the send path)
        await check_trust(str(cwd))
    title = params.get("title") or ""
    if not isinstance(title, str):
        raise RpcError("bad_request", "title must be a string")
    try:
        runner = await m.create(
            cwd=str(cwd),
            model=model,
            mode=mode,
            resume=resume,
            fork=bool(params.get("fork")),
            title=" ".join(title.split())[:TITLE_MAX],   # as a rename would
        )
    except RunnerLimit as e:
        raise runner_limit_error(e, ctx.scope)
    return runner.snapshot()


async def _sessions_incognito(m: SessionManager, config: Config, params: dict,
                              ctx: RpcContext) -> dict:
    """Open this device's incognito chat, starting one if it has none."""
    home = incognito_home(ctx.device_id or "")
    if home is None or home not in ctx.scope.private:
        raise RpcError("forbidden", "an incognito chat needs a device with "
                                    "at least one folder")
    model = params.get("model") or "default"
    if not valid_model(model):
        raise RpcError("bad_request", "invalid model")
    try:
        runner = await m.create_incognito(ctx.device_id, home, model)
    except RunnerLimit as e:
        raise runner_limit_error(e, ctx.scope)
    return runner.snapshot()


async def _sessions_stop(m: SessionManager, config: Config, params: dict,
                         ctx: RpcContext) -> dict:
    runner = _runner(m, params, ctx.scope)
    await runner.stop()
    return runner.snapshot()


async def _sessions_rename(m: SessionManager, config: Config, params: dict,
                           ctx: RpcContext) -> dict:
    """Rename a session. Titles are not unique — they are a label, not an id."""
    runner = _runner(m, params, ctx.scope)
    if runner.incognito:
        raise RpcError("forbidden", "an incognito chat keeps its name")
    title = " ".join(_require_str(params, "title").split())
    if not title:
        raise RpcError("bad_request", "title must not be empty")
    await runner.rename(title[:TITLE_MAX])
    return runner.snapshot()


async def _sessions_rewind(m: SessionManager, config: Config, params: dict,
                           ctx: RpcContext) -> dict:
    """"Edit from here" on one of the latest prompts: {"text", "prompts",
    "files"}. With "dry" it only reports what would go (the confirmation)."""
    runner = _runner(m, params, ctx.scope)
    seq = params.get("seq")
    if not isinstance(seq, int) or isinstance(seq, bool):
        raise RpcError("bad_request", "seq must be an integer")
    try:
        return await runner.rewind(seq, m.terminal_open(runner.sid),
                                   dry=bool(params.get("dry")))
    except ValueError as e:
        raise RpcError("bad_request", str(e)) from None


async def _sessions_delete(m: SessionManager, config: Config, params: dict,
                           ctx: RpcContext) -> dict:
    sid = _require_str(params, "sid")
    runner = m.get(sid)
    if runner is not None and not ctx.scope.contains(runner.cwd):
        raise RpcError("not_found", "no such session")
    await m.delete(sid)
    return {"ok": True}


async def _projects_recent(m: SessionManager, config: Config, params: dict,
                           ctx: RpcContext) -> dict:
    items = await asyncio.to_thread(projects.recent_projects)
    return {"projects": [p for p in items if ctx.scope.contains(p["path"])]}


PINNED_MAX = 50   # pinned past sessions looked up per project list
PAST_PAGE = 25    # past sessions per page ("Show more" loads the next)


async def _projects_sessions(m: SessionManager, config: Config, params: dict,
                             ctx: RpcContext) -> dict:
    cwd = str(_jailed_path(_require_str(params, "cwd"), ctx.scope))
    # ids the app has pinned in this project (optional); a bounded list of
    # well-formed ids, anything else is ignored rather than refused
    pinned = params.get("pinned")
    pinned = [p for p in pinned if isinstance(p, str) and valid_session_id(p)][:PINNED_MAX] \
        if isinstance(pinned, list) else []
    offset = params.get("offset", 0)
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise RpcError("bad_request", "invalid offset")
    past, more = await asyncio.to_thread(projects.past_sessions, cwd, PAST_PAGE,
                                         offset=offset, pinned=pinned)
    # worktree sessions carry their own cwd; keep only those in scope
    past = [s for s in past if ctx.scope.contains(s["cwd"] or cwd)]
    # a transcript some runner already holds opens as that runner, as
    # from the session list, instead of offering to resume it again
    for s in past:
        held = _holding_runner(m, s["session_id"], ctx.scope)
        s["runner_sid"] = held.sid if held else None
    # the offset of the next page, counted before the scope filter above
    return {"sessions": past, "next": offset + PAST_PAGE if more else None}


async def _projects_preview(m: SessionManager, config: Config, params: dict,
                            ctx: RpcContext) -> dict:
    session_id = _session_id(params, "session_id")
    cwd = str(_jailed_path(_require_str(params, "cwd"), ctx.scope))
    await _require_project_session(session_id, cwd, ctx.scope)
    return {"messages": await asyncio.to_thread(
        projects.session_preview, session_id, cwd)}


async def _projects_rename(m: SessionManager, config: Config, params: dict,
                           ctx: RpcContext) -> dict:
    """Rename a past session without resuming it: a custom-title line goes
    into its transcript, as /rename in the terminal does. A runner whose
    current claude session this is takes the name too, so the two labels
    never disagree."""
    session_id = _session_id(params, "session_id")
    cwd = str(_jailed_path(_require_str(params, "cwd"), ctx.scope))
    await _require_project_session(session_id, cwd, ctx.scope)
    title = " ".join(_require_str(params, "title").split())[:TITLE_MAX]
    if not title:
        raise RpcError("bad_request", "title must not be empty")
    runners = [r for r in m.runners.values()
               if r.claude_ids and r.claude_ids[-1] == session_id]
    for r in runners:
        await r.rename(title)   # writes the transcript line itself
    if not runners:
        try:
            await asyncio.to_thread(rename_session, session_id, title, cwd)
        except (OSError, ValueError) as e:
            raise RpcError("not_found", f"could not rename: {e}")
    return {"ok": True, "title": title}


SEARCH_PAGE = 50   # search results per page ("Show more" loads the next)
# one search at a time: each reads every transcript, and a second one
# running alongside would only slow both
_search_lock = asyncio.Lock()


async def _projects_search(m: SessionManager, config: Config, params: dict,
                           ctx: RpcContext) -> dict:
    query = _require_str(params, "q")
    offset = params.get("offset", 0)
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise RpcError("bad_request", "invalid offset")
    async with _search_lock:
        try:
            found, more = await asyncio.to_thread(
                search.search, query, ctx.scope.contains, SEARCH_PAGE, offset)
        except search.QueryError as e:
            raise RpcError("bad_request", str(e))
    for s in found:   # a session some runner holds opens as that runner
        held = _holding_runner(m, s["session_id"], ctx.scope)
        s["runner_sid"] = held.sid if held else None
    return {"sessions": found, "next": offset + SEARCH_PAGE if more else None}


async def _browse(m: SessionManager, config: Config, params: dict,
                  ctx: RpcContext) -> dict:
    path = params.get("path")
    if path is not None and not isinstance(path, str):
        raise RpcError("bad_request", "path must be a string")
    return await asyncio.to_thread(projects.browse, path, list(ctx.scope.roots))



async def _mkdir(m: SessionManager, config: Config, params: dict,
                 ctx: RpcContext) -> dict:
    parent = _jailed_path(_require_str(params, "parent"), ctx.scope)
    name = _require_str(params, "name")
    try:
        child = await asyncio.to_thread(projects.make_dir, parent, name)
    except FileExistsError:
        raise RpcError("exists", "a folder with that name already exists")
    except PermissionError:
        raise RpcError("forbidden", "cannot create a folder here")
    except FileNotFoundError:
        raise RpcError("not_found", "parent folder is gone")
    except (ValueError, OSError) as e:
        raise RpcError("bad_request", str(e) or "could not create the folder")
    return {"path": str(child)}

async def _trust_add(m: SessionManager, config: Config, params: dict,
                     ctx: RpcContext) -> dict:
    """Trust a folder, as the terminal's first-run question does: its
    hooks, allow rules and commands may then run without asking."""
    cwd = _jailed_path(_require_str(params, "cwd"), ctx.scope)
    if not cwd.is_dir():
        raise RpcError("bad_request", "not a directory")
    ctx.require_stepup("trust this folder")
    await asyncio.to_thread(trust.add, str(cwd))
    return {"ok": True}


async def _files_call(ctx: RpcContext, fn, *args):
    if not files.can_view(ctx.device):
        raise RpcError("forbidden", "this device may not browse files "
                                    "(devices set --files on the server)")
    try:
        return await asyncio.to_thread(fn, *args, ctx.scope)
    except files.FilesError as e:
        raise RpcError(e.code if e.code in HTTP_STATUS else "bad_request", e.message)
    except OSError as e:
        raise RpcError("bad_request", e.strerror or str(e))


async def _files_list(m: SessionManager, config: Config, params: dict,
                      ctx: RpcContext) -> dict:
    path = params.get("path")
    if path is not None and not isinstance(path, str):
        raise RpcError("bad_request", "path must be a string")
    return await _files_call(ctx, files.list_dir, path)


async def _files_text(m: SessionManager, config: Config, params: dict,
                      ctx: RpcContext) -> dict:
    return await _files_call(ctx, files.read_text, _require_str(params, "path"))


async def _library(m: SessionManager, config: Config, params: dict,
                   ctx: RpcContext) -> dict:
    cwd = _jailed_opt(params, "cwd", ctx.scope)
    if not ctx.scope.roots:
        # no folders granted yet: user-level config (MCP URLs/commands,
        # skill paths) is not its to see either
        return {"skills": [], "commands": [], "agents": [],
                "mcp_servers": [], "cwd": cwd}
    return await asyncio.to_thread(library.get_library, cwd)


async def _devices_list(m: SessionManager, config: Config, params: dict,
                        ctx: RpcContext) -> dict:
    # sync on purpose: DeviceStore is also used from the event loop (verify)
    devs = ctx.devices.list()
    if not ctx.manages_devices:   # without the pairing grant: only itself
        devs = [d for d in devs if d["id"] == ctx.device_id]
    for d in devs:
        d["current"] = d["id"] == ctx.device_id
    return {"devices": devs, "can_manage": ctx.manages_devices}


async def _devices_rename(m: SessionManager, config: Config, params: dict,
                          ctx: RpcContext) -> dict:
    """Rename a device: its own name always, another device's only with the
    manage grant (and a fresh passkey confirmation, like revoking one)."""
    device_id = _require_str(params, "id")
    name = _require_str(params, "name")
    if device_id != ctx.device_id:
        if not ctx.manages_devices:
            raise RpcError("forbidden", "this device may only rename itself")
        ctx.require_stepup("rename another device")
    name = " ".join(name.split())
    if not name:
        raise RpcError("bad_request", "name must not be empty")
    if len(name) > NAME_MAX:
        raise RpcError("bad_request", f"name is longer than {NAME_MAX} characters")
    if ctx.devices.name_taken(name, except_id=device_id):
        raise RpcError("bad_request", "another device already has that name or id")
    if not ctx.devices.set_name(device_id, name):
        raise RpcError("not_found", "no such device")
    return {"ok": True, "id": device_id}


async def _devices_revoke(m: SessionManager, config: Config, params: dict,
                          ctx: RpcContext) -> dict:
    device_id = _require_str(params, "id")
    if device_id != ctx.device_id and not ctx.manages_devices:
        raise RpcError("forbidden", "this device may only sign itself out")
    if device_id != ctx.device_id:
        ctx.require_stepup("sign out another device")
    if not ctx.devices.revoke(device_id):
        raise RpcError("not_found", "no such device")
    # the credential itself stays: it grants nothing on its own
    passkey.drop_device(device_id)
    passkey.drop_elevation(device_id)
    await m.erase_owner(device_id)
    return {"ok": True, "current": device_id == ctx.device_id}


def _require_manager(ctx: RpcContext) -> None:
    if not ctx.manages_devices:
        raise RpcError("forbidden", "needs the manage grant")


async def _sdk_status(m: SessionManager, config: Config, params: dict,
                      ctx: RpcContext) -> dict:
    _require_manager(ctx)
    w = sdk_update.WATCH
    if params.get("check"):
        await w.check()
    return await asyncio.to_thread(w.status, w.busy())


async def _sdk_install(m: SessionManager, config: Config, params: dict,
                       ctx: RpcContext) -> dict:
    """Start installing the version the last check offered; the app polls
    sdk.status. Only that exact version: the app never names an arbitrary
    one. `cli` (optional, "Update both"): the CLI version on offer, updated
    right after the SDK installs."""
    _require_manager(ctx)
    ctx.require_confirmation("update the Agent SDK")
    w = sdk_update.WATCH
    version = _require_str(params, "version")
    if not w.update_available() or version != w.latest:
        raise RpcError("bad_request", "that is not the update on offer")
    cli = params.get("cli")
    if cli is not None and (cli != w.cli_latest
                            or not await asyncio.to_thread(w.cli_update_available)):
        raise RpcError("bad_request", "that is not the CLI update on offer")
    if not sdk_update.can_install():
        raise RpcError("forbidden", "no pip here; run: " + sdk_update.hint_command(version))
    if w.installing or w.cli_updating:
        raise RpcError("bad_request", "another update is running")
    asyncio.create_task(w.install(version, and_cli=cli is not None))
    return {"ok": True, "installing": version}


async def _sdk_cli_update(m: SessionManager, config: Config, params: dict,
                          ctx: RpcContext) -> dict:
    """Start `claude update` when the last check found a newer CLI; the app
    polls sdk.status. `version` must be the one on offer, as in sdk.install."""
    _require_manager(ctx)
    ctx.require_confirmation("update the claude CLI")
    w = sdk_update.WATCH
    version = _require_str(params, "version")
    if not await asyncio.to_thread(w.cli_update_available) or version != w.cli_latest:
        raise RpcError("bad_request", "that is not the update on offer")
    if w.installing or w.cli_updating:
        raise RpcError("bad_request", "another update is running")
    asyncio.create_task(w.update_cli())
    return {"ok": True, "updating": version}


async def _sdk_restart(m: SessionManager, config: Config, params: dict,
                       ctx: RpcContext) -> dict:
    """when: "now" (stops every session), "idle" (once no turn is running
    and no True View is open) or "cancel" (drop a pending "idle")."""
    _require_manager(ctx)
    w = sdk_update.WATCH
    when = params.get("when")
    if when == "cancel":
        w.restart_when_idle = False
        return {"ok": True}
    if when not in ("now", "idle"):
        raise RpcError("bad_request", "when must be now, idle or cancel")
    ctx.require_confirmation("restart the server")
    if not sdk_update.supervised():
        raise RpcError("forbidden", "no supervisor (pm2/systemd) would start the "
                                    "server again; restart it on the host")
    if w.installing or w.cli_updating:
        raise RpcError("bad_request", "wait for the install to finish")
    if when == "idle":
        # armed with nothing pending, it would fire unasked after a later install
        if not w.restart_pending():
            raise RpcError("bad_request", "nothing to restart for")
        w.restart_when_idle = True
        return {"ok": True, "when": "idle"}
    asyncio.get_running_loop().call_later(0.5, sdk_update.restart_now)
    return {"ok": True, "when": "now"}


METHODS = {
    "state": _state,
    "sessions.create": _sessions_create,
    "sessions.incognito": _sessions_incognito,
    "sessions.stop": _sessions_stop,
    "sessions.rename": _sessions_rename,
    "sessions.rewind": _sessions_rewind,
    "sessions.delete": _sessions_delete,
    "projects.recent": _projects_recent,
    "projects.sessions": _projects_sessions,
    "projects.preview": _projects_preview,
    "projects.rename": _projects_rename,
    "projects.search": _projects_search,
    "browse": _browse,
    "mkdir": _mkdir,
    "trust.add": _trust_add,
    "files.list": _files_list,
    "files.text": _files_text,
    "library": _library,
    "devices.list": _devices_list,
    "devices.rename": _devices_rename,
    "devices.revoke": _devices_revoke,
    "sdk.status": _sdk_status,
    "sdk.install": _sdk_install,
    "sdk.cli_update": _sdk_cli_update,
    "sdk.restart": _sdk_restart,
}


async def dispatch(m: SessionManager, config: Config, ctx: RpcContext,
                   method: str, params: dict) -> dict:
    handler = METHODS.get(method)
    if handler is None:
        raise RpcError("unknown_method", f"unknown method: {method}")
    if not isinstance(params, dict):
        raise RpcError("bad_request", "params must be an object")
    return await handler(m, config, params, ctx)
