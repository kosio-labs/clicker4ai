"""Incognito chat test, in process, no browser and no message to the model.

Everything lives under .scratch/incognito-test/ (data dir, incognito dir and
CLAUDE_CONFIG_DIR), so neither data/ nor ~/.claude is touched. It does start
`claude` processes (runners), but sends them nothing.

    .venv/bin/python scripts/incognito_test.py
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BASE = REPO / ".scratch" / "incognito-test"
shutil.rmtree(BASE, ignore_errors=True)
(BASE / "data").mkdir(parents=True)
(BASE / "claude").mkdir()
(BASE / "proj").mkdir()
os.environ["C4AI_DATA_DIR"] = str(BASE / "data")
# inside ~/work on purpose: an allowed root that contains the incognito dir
os.environ["C4AI_INCOGNITO_DIR"] = str(BASE / "incognito")
os.environ["CLAUDE_CONFIG_DIR"] = str(BASE / "claude")
(BASE / "data" / "config.json").write_text(json.dumps(
    {"allowed_roots": [str(Path.home() / "work")], "port": 8799}))
sys.path.insert(0, str(REPO))

from clicker4ai import main, rpc, trust  # noqa: E402  (after the env above)
from clicker4ai.config import INCOGNITO_DIR  # noqa: E402
from clicker4ai.projects import browse  # noqa: E402
from clicker4ai.scope import device_scope  # noqa: E402
from clicker4ai.sessions import INCOGNITO_TOOLS, SessionManager  # noqa: E402

store = main.devices
config = main.config
failures: list[str] = []


def check(what: str, ok: bool) -> None:
    print(("ok   " if ok else "FAIL ") + what)
    if not ok:
        failures.append(what)


def device(name: str, roots: list[str], terminal: bool = False) -> dict:
    _token, dev = store.create(name, roots=roots, manage_devices=False,
                               terminal=terminal, require_passkey=False)
    return dev


def ctx(dev: dict) -> rpc.RpcContext:
    dev = next(d for d in store._devices if d["id"] == dev["id"])
    return rpc.RpcContext(store, device_scope(config.allowed_roots, dev), dev)


async def call(m, dev, method, **params):
    return await rpc.dispatch(m, config, ctx(dev), method, params)


async def refused(m, dev, method, code, **params) -> bool:
    try:
        await call(m, dev, method, **params)
    except rpc.RpcError as e:
        return e.code == code
    return False


def project_dir(folder: str) -> Path:
    return BASE / "claude" / "projects" / re.sub(r"[^a-zA-Z0-9]", "-", folder)


def fake_traces(folder: str) -> None:
    """What a real turn leaves: a transcript and a typed prompt."""
    pdir = project_dir(folder)
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "0f0f0f0f-0000-4000-8000-000000000000.jsonl").write_text(json.dumps(
        {"type": "user", "cwd": folder, "sessionId": "0f0f0f0f-0000-4000-8000-000000000000",
         "message": {"role": "user", "content": "secret"}}) + "\n")
    with open(BASE / "claude" / "history.jsonl", "a") as f:
        f.write(json.dumps({"display": "secret", "project": folder, "timestamp": 1}) + "\n")


def history_mentions(folder: str) -> bool:
    p = BASE / "claude" / "history.jsonl"
    return p.exists() and folder in p.read_text()


async def wait_live(r, secs: float = 60) -> bool:
    for _ in range(int(secs * 10)):
        if r.client is not None and r.state == "idle":
            return True
        await asyncio.sleep(0.1)
    return False


async def main_test() -> None:
    m = SessionManager(max_runners=6)
    a = device("inc-a", [str(Path.home() / "work")], terminal=True)
    b = device("inc-b", [str(Path.home() / "work")])
    c = device("inc-c", [])

    # --- who may start one
    check("state: device with folders can_incognito",
          (await call(m, a, "state"))["can_incognito"] is True)
    check("state: device without folders cannot",
          (await call(m, c, "state"))["can_incognito"] is False)
    check("sessions.incognito refused without folders",
          await refused(m, c, "sessions.incognito", "forbidden"))

    # --- one per device, own folder
    s1 = await call(m, a, "sessions.incognito")
    s2 = await call(m, a, "sessions.incognito")
    check("second request opens the same chat", s1["sid"] == s2["sid"])
    folder = s1["cwd"]
    check("folder is INCOGNITO_DIR/<device>/<sid>",
          Path(folder) == INCOGNITO_DIR / a["id"] / s1["sid"] and Path(folder).is_dir())
    check("snapshot marks it incognito", s1["incognito"] and s1["project"] == "incognito")

    # --- visibility
    check("owner sees it in state", any(s["sid"] == s1["sid"]
          for s in (await call(m, a, "state"))["sessions"]))
    check("other device does not", all(s["sid"] != s1["sid"]
          for s in (await call(m, b, "state"))["sessions"]))
    check("other device cannot stop it",
          await refused(m, b, "sessions.stop", "not_found", sid=s1["sid"]))
    check("sessions.create refuses the folder (owner)",
          await refused(m, a, "sessions.create", "forbidden", cwd=folder))
    check("sessions.create refuses the folder (other device)",
          await refused(m, b, "sessions.create", "forbidden", cwd=folder))
    listed = [d["path"] for d in browse(str(BASE), list(ctx(a).scope.roots))["dirs"]]
    check("folder picker hides the incognito dir", str(INCOGNITO_DIR) not in listed)

    # --- tools and True View
    r1 = m.get(s1["sid"])
    opts = r1._build_options(None, False)
    check("tools = WebSearch, WebFetch only", opts.tools == INCOGNITO_TOOLS)
    check("no MCP servers", opts.mcp_servers == {} and opts.strict_mcp_config is True)
    check("True View refused even with --terminal",
          main._terminal_denial(a, folder, incognito=True) == main.INCOGNITO_DENIED)
    check("True View still allowed for an ordinary session",
          main._terminal_denial(a, str(BASE / "proj")) is None)

    # --- fixed name and mode
    check("sessions.rename refused",
          await refused(m, a, "sessions.rename", "forbidden", sid=s1["sid"], title="x"))

    class Conn:
        def __init__(self): self.pushed = []
        def push(self, p): self.pushed.append(p)
    conn = Conn()
    await main.handle_ws(m, conn, ctx(a), {"type": "set_mode", "session_id": s1["sid"],
                                           "mode": "plan"})
    check("set_mode refused", m.get(s1["sid"]).mode == "default"
          and any(p.get("type") == "error" for p in conn.pushed))

    # --- runner limit: idle incognito chats go right after empty sessions
    check("incognito runner started", await wait_live(r1))
    r1.events.append({"kind": "user_text", "text": "x"})   # not empty any more
    m.max_runners = 1
    # the repo's own .claude/settings.local.json sits above it (trust.py)
    trust.add(str(BASE / "proj"))
    normal = await call(m, a, "sessions.create", cwd=str(BASE / "proj"))
    check("new session started despite the limit", normal["sid"] in m.runners)
    check("idle incognito chat was stopped, not erased",
          r1.client is None and s1["sid"] in m.runners and Path(folder).is_dir())
    await call(m, a, "sessions.delete", sid=normal["sid"])
    m.max_runners = 6

    # --- erase
    fake_traces(folder)
    dry = subprocess.run(["claude", "project", "purge", "--dry-run", folder],
                         capture_output=True, text=True, timeout=60).stdout
    check("purge --dry-run sees transcripts and history",
          "projects" in dry and "history.jsonl" in dry)
    await call(m, a, "sessions.delete", sid=s1["sid"])
    check("erase: runner gone", s1["sid"] not in m.runners)
    check("erase: folder gone", not Path(folder).exists())
    check("erase: transcripts gone", not project_dir(folder).exists())
    check("erase: typed prompts gone from history.jsonl", not history_mentions(folder))
    check("erase: data/sessions/<sid> gone",
          not (BASE / "data" / "sessions" / s1["sid"]).exists())

    # --- sweep: idle, device gone, leftovers
    sb = await call(m, b, "sessions.incognito")
    m.get(sb["sid"]).last_active -= 25 * 3600 * 1000
    await m.incognito_sweep({a["id"], b["id"]})
    check("sweep erases a chat idle for 24 h", sb["sid"] not in m.runners
          and not Path(sb["cwd"]).exists())
    sa = await call(m, a, "sessions.incognito")
    await m.incognito_sweep({b["id"]})
    check("sweep erases a chat whose device is gone", sa["sid"] not in m.runners
          and not Path(sa["cwd"]).exists())
    stray = INCOGNITO_DIR / b["id"] / "stray"
    stray.mkdir(parents=True)
    await m.incognito_sweep({a["id"], b["id"]})
    check("sweep removes folders no chat owns", not stray.exists())
    foreign = INCOGNITO_DIR / "otherserverdev" / "live"
    foreign.mkdir(parents=True)
    await m.incognito_sweep({a["id"], b["id"]})
    check("sweep leaves folders of devices it does not know", foreign.is_dir())
    shutil.rmtree(foreign.parent)
    # a chat whose folder was erased under it starts afresh in a new one
    sc = await call(m, a, "sessions.incognito")
    rc = m.get(sc["sid"])
    await wait_live(rc)
    await rc.stop()
    shutil.rmtree(sc["cwd"])
    check("lost folder is not listed as missing", not rc.snapshot()["cwd_missing"])
    await rc.start(resume=rc.claude_ids[-1] if rc.claude_ids else None)
    check("lost folder recreated at start", Path(sc["cwd"]).is_dir() and await wait_live(rc))
    await m.delete(sc["sid"])
    outside = BASE / "proj"
    from clicker4ai.sessions import erase_incognito_dir
    await erase_incognito_dir(str(outside))
    check("erase refuses anything outside INCOGNITO_DIR/<device>/<sid>", outside.is_dir())

    # --- revoke in the app erases at once
    sa = await call(m, a, "sessions.incognito")
    await call(m, a, "devices.revoke", id=a["id"])
    check("revoking the device erases its chat", sa["sid"] not in m.runners
          and not Path(sa["cwd"]).exists())

    await m.shutdown()


try:
    asyncio.run(main_test())
finally:
    shutil.rmtree(BASE, ignore_errors=True)
print(f"\n{len(failures)} failure(s)" if failures else "\nall passed")
sys.exit(1 if failures else 0)
