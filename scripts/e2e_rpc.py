"""RPC-over-WebSocket e2e: every method in clicker4ai/rpc.py driven through a
real /ws connection (uvicorn on localhost), plus the hardening negatives:
pairing-code login + per-device sessions (revoke → 4401), cookie-only WS
auth, Origin allowlist, no ?token=/Bearer, cwd jail, param validation,
JSON-only POSTs, failed-login throttle, per-device folder scope, the
manage-devices and terminal (True View) grants and the remote
permission-mode block.

No Claude turns are run (sessions.create is exercised without send).
Run: .venv/bin/python scripts/e2e_rpc.py
"""

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _testdata import isolate  # noqa: E402
isolate("e2e_rpc")   # never the live ~/.clicker4ai

import httpx  # noqa: E402
import uvicorn  # noqa: E402
import websockets  # noqa: E402

from clicker4ai import main as srv  # noqa: E402
from clicker4ai.auth import issue_pairing_code, redeem_pairing_code  # noqa: E402
from clicker4ai.config import Config  # noqa: E402
from clicker4ai.rpc import METHODS  # noqa: E402

HOST, PORT = "127.0.0.1", 8798
BASE = f"http://{HOST}:{PORT}"
WS_URL = f"ws://{HOST}:{PORT}/ws"

CWD = str(Path(__file__).resolve().parent.parent / ".scratch" / "e2e-playground")
SCOPED = str(Path(CWD) / "scoped")   # a scoped device only reaches this
Path(SCOPED).mkdir(parents=True, exist_ok=True)

cfg = Config.load()
_rid = 0
driven: set[str] = set()


async def rpc(ws, method, params=None):
    global _rid
    _rid += 1
    rid = _rid
    driven.add(method)
    await ws.send(json.dumps(
        {"type": "rpc", "id": rid, "method": method, "params": params or {}}))
    while True:  # event pushes may interleave with rpc results
        msg = json.loads(await asyncio.wait_for(ws.recv(), 30))
        if msg.get("type") == "rpc_result" and msg.get("id") == rid:
            return msg


async def expect_close(code, why, url=WS_URL, **kwargs):
    try:
        async with websockets.connect(url, **kwargs) as ws:
            await asyncio.wait_for(ws.recv(), 10)
        raise AssertionError(f"{why}: connection survived, expected close {code}")
    except websockets.ConnectionClosed as e:
        got = e.rcvd.code if e.rcvd else None
        assert got == code, f"{why}: expected close {code}, got {got}"
    print(f"  auth negative ok — {why} -> {code}")


# Every device this test creates, so a failed assertion cannot leave one
# behind in the live data directory (the happy path revokes them as it goes).
CREATED: list[str] = []


SERVER = None   # uvicorn Server, so cleanup can stop it after a failure


async def pair(client, name, **grants):
    """Redeem a fresh pairing code → (device token, device id). No passkey
    requirement: these devices never register one (passkey_test.py covers it)."""
    grants.setdefault("require_passkey", False)
    r = await client.post(f"{BASE}/api/login",
                          json={"code": issue_pairing_code(**grants), "name": name})
    assert r.status_code == 200, r.text
    token = r.cookies.get(srv.COOKIE_PLAIN)
    assert token, f"no {srv.COOKIE_PLAIN} cookie: {r.headers}"
    dev = srv.devices.verify(token)
    assert dev is not None
    CREATED.append(dev["id"])
    return token, dev["id"]


async def main():
    global SERVER
    server = SERVER = uvicorn.Server(uvicorn.Config(
        srv.app, host=HOST, port=PORT, log_level="warning"))
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)

    assert len(cfg.allowed_roots) == 1, "e2e expects a single allowed root"
    home = cfg.allowed_roots[0]
    assert Path(CWD).is_relative_to(home), f"{CWD} must be inside allowed_roots"
    origin = f"http://{HOST}:{PORT}"

    # ---- login: pairing codes only, single use
    async with httpx.AsyncClient() as client:
        r = await client.post(f"{BASE}/api/login", json={"token": "anything"})
        assert r.status_code == 401, "master-token login must be gone"
        code = issue_pairing_code()
        r = await client.post(f"{BASE}/api/login", content=json.dumps({"code": code}),
                              headers={"Content-Type": "text/plain"})
        assert r.status_code == 415, f"non-JSON POST must be refused: {r.status_code}"
        token, dev_id = await pair(client, "rpc-e2e")
        other_token, other_id = await pair(client, "rpc-e2e-other")
        mgr_token, mgr_id = await pair(client, "rpc-e2e-manager", manage_devices=True)
        scoped_token, scoped_id = await pair(client, "rpc-e2e-scoped", roots=[SCOPED])
        r = await client.post(f"{BASE}/api/login", json={"code": code})
        assert r.status_code == 200, "code refused by the 415 must still be unused"
        extra = r.cookies.get(srv.COOKIE_PLAIN)
        r = await client.post(f"{BASE}/api/login", json={"code": code})
        assert r.status_code == 401, "pairing code must be single use"
        srv.devices.revoke(srv.devices.verify(extra)["id"])
    print("login ok — pairing code single use, JSON-only, no master token")

    good = {"Cookie": f"{srv.COOKIE_PLAIN}={token}", "Origin": origin}

    async with websockets.connect(WS_URL, additional_headers=good) as ws:
        hello = json.loads(await ws.recv())
        assert hello["type"] == "hello", hello
        first = json.loads(await ws.recv())
        assert first["type"] == "sessions", first
        # the frontend stamp tells an already-loaded page that it is stale
        stamp = hello.get("assets")
        assert stamp and stamp == srv.asset_stamp(), hello
        app_js = srv.WEB_DIR / "app.js"
        mtime = app_js.stat().st_mtime_ns
        try:
            os.utime(app_js, ns=(mtime + 10**9, mtime + 10**9))
            assert srv.asset_stamp() != stamp, "a changed frontend must restamp"
        finally:
            os.utime(app_js, ns=(mtime, mtime))
        assert srv.asset_stamp() == stamp, "restoring the file must restore the stamp"
        print(f"connected — hello v{hello['version']} assets {stamp}")

        # ---- state
        r = await rpc(ws, "state")
        assert r["ok"] and r["result"]["ok"] and "defaults" in r["result"], r
        print(f"state ok — {len(r['result']['sessions'])} sessions")

        # ---- projects.recent
        r = await rpc(ws, "projects.recent")
        assert r["ok"] and isinstance(r["result"]["projects"], list), r
        print(f"projects.recent ok — {len(r['result']['projects'])} projects")

        # ---- browse + root confinement
        r = await rpc(ws, "browse")
        assert r["ok"] and r["result"]["path"] == home, r
        r = await rpc(ws, "browse", {"path": "/etc"})
        assert r["ok"] and r["result"]["path"] == home, "browse escaped root!"
        print("browse ok — root confinement holds")

        # ---- library
        r = await rpc(ws, "library")
        assert r["ok"] and "skills" in r["result"], r
        print(f"library ok — {len(r['result']['skills'])} skills")

        # ---- protected dirs: out of every scope, even under a root
        from clicker4ai.config import PROTECTED_DIRS
        from clicker4ai.scope import Scope
        for d in PROTECTED_DIRS:
            assert not Scope([d.parent]).contains(d / "x"), f"{d} reachable"
        print("protected dirs ok — data, incognito, Claude dir out of scope")

        # ---- mkdir: one level, jailed, no clobbering, no hidden/odd names
        r = await rpc(ws, "mkdir", {"parent": CWD, "name": "new-proj"})
        made = Path(CWD) / "new-proj"
        assert r["ok"] and r["result"]["path"] == str(made) and made.is_dir(), r
        r = await rpc(ws, "mkdir", {"parent": str(made), "name": "sub"})
        assert r["ok"] and (made / "sub").is_dir(), r
        r = await rpc(ws, "mkdir", {"parent": CWD, "name": "new-proj"})
        assert not r["ok"] and r["error"]["code"] == "exists", r
        r = await rpc(ws, "mkdir", {"parent": "/etc", "name": "x"})
        assert not r["ok"] and r["error"]["code"] == "forbidden", r
        for bad in ("..", ".hidden", "a/b", "x" * 256):
            r = await rpc(ws, "mkdir", {"parent": CWD, "name": bad})
            assert not r["ok"] and r["error"]["code"] == "bad_request", (bad, r)
        assert not any((Path(CWD) / n).exists() for n in (".hidden", "a")), \
            "a rejected name created something"
        print("mkdir ok — nested + exists + jail + bad names")

        # ---- projects.sessions / projects.preview (real transcripts)
        r = await rpc(ws, "projects.sessions", {"cwd": CWD})
        assert r["ok"] and isinstance(r["result"]["sessions"], list), r
        past = r["result"]["sessions"]
        if past:
            r = await rpc(ws, "projects.preview",
                          {"session_id": past[0]["session_id"], "cwd": CWD})
            assert r["ok"] and isinstance(r["result"]["messages"], list), r
        r = await rpc(ws, "projects.preview", {"session_id": "no-such-session", "cwd": CWD})
        assert not r["ok"] and r["error"]["code"] == "not_found", r
        print(f"projects.sessions/preview ok — {len(past)} past, unknown id not_found")

        # ---- projects.rename: refusals only — the playground has no past
        # session, and renaming a real one would touch a live transcript
        r = await rpc(ws, "projects.rename",
                      {"session_id": "no-such-session", "cwd": CWD, "title": "x"})
        assert not r["ok"] and r["error"]["code"] == "not_found", r
        r = await rpc(ws, "projects.rename",
                      {"session_id": "no-such-session", "cwd": "/etc", "title": "x"})
        assert not r["ok"] and r["error"]["code"] == "forbidden", r
        print("projects.rename ok — unknown session not_found, cwd jail")

        # ---- projects.search: the RPC shape and its refusals (matching
        # itself is scripts/search_test.py)
        r = await rpc(ws, "projects.search", {"q": "zzqx-no-such-word"})
        assert r["ok"] and r["result"] == {"sessions": [], "next": None}, r
        r = await rpc(ws, "projects.search", {"q": "a?c"})
        assert not r["ok"] and r["error"]["code"] == "bad_request", r
        r = await rpc(ws, "projects.search", {"q": "x", "offset": -1})
        assert not r["ok"] and r["error"]["code"] == "bad_request", r
        print("projects.search ok — no match, bad query and offset refused")

        # ---- sessions.incognito: a bad model is refused before anything is
        # created under ~/incognito (the chat itself: scripts/incognito_test.py)
        r = await rpc(ws, "sessions.incognito", {"model": "bad model!"})
        assert not r["ok"] and r["error"]["code"] == "bad_request", r
        print("sessions.incognito ok — bad model refused, nothing started")

        # ---- devices without the manage grant: only themselves
        r = await rpc(ws, "devices.list")
        assert r["ok"] and r["result"]["can_manage"] is False, r
        ids = [d["id"] for d in r["result"]["devices"]]
        assert ids == [dev_id] and r["result"]["devices"][0]["current"], r
        r = await rpc(ws, "devices.revoke", {"id": other_id})
        assert not r["ok"] and r["error"]["code"] == "forbidden", r
        assert srv.devices.verify(other_token) is not None
        # renaming: itself yes, another device no, a taken name never
        bench = f"bench phone {os.getpid()}"
        r = await rpc(ws, "devices.rename", {"id": dev_id, "name": f"  {bench} "})
        assert r["ok"], r
        assert srv.devices.verify(token)["name"] == bench, srv.devices.list()
        r = await rpc(ws, "devices.rename", {"id": other_id, "name": "nope"})
        assert not r["ok"] and r["error"]["code"] == "forbidden", r
        taken = srv.devices.verify(other_token)["name"]
        r = await rpc(ws, "devices.rename", {"id": dev_id, "name": taken.upper()})
        assert not r["ok"] and r["error"]["code"] == "bad_request", r
        r = await rpc(ws, "devices.rename", {"id": dev_id, "name": "   "})
        assert not r["ok"] and r["error"]["code"] == "bad_request", r
        print("devices ok — plain device sees/revokes/renames only itself")

        # ---- remote permission-mode block
        r = await rpc(ws, "state")
        assert r["result"]["defaults"]["modes"] == ["default", "acceptEdits", "plan", "auto"], r
        for mode in ("bypassPermissions", "dontAsk"):
            r = await rpc(ws, "sessions.create", {"cwd": CWD, "mode": mode})
            assert not r["ok"] and r["error"]["code"] == "bad_request", f"{mode}: {r}"
        print("mode block ok — bypassPermissions/dontAsk refused, not offered")

        # ---- folder trust: an allow rule (nothing that runs) makes the
        # playground ask before its first process; trusted in this run's
        # data dir only
        (Path(CWD) / ".claude").mkdir(exist_ok=True)
        (Path(CWD) / ".claude" / "settings.json").write_text(
            json.dumps({"permissions": {"allow": ["Read"]}}))
        r = await rpc(ws, "sessions.create", {"cwd": CWD, "title": "untrusted"})
        assert not r["ok"] and r["error"]["code"] == "untrusted", r
        items = r["error"]["data"]["items"]
        assert {"path": ".claude/settings.json", "note": "allows: Read"} in items, items
        r = await rpc(ws, "trust.add", {"cwd": "/etc"})
        assert not r["ok"] and r["error"]["code"] == "forbidden", r
        r = await rpc(ws, "trust.add", {"cwd": CWD})
        assert r["ok"], r
        print("trust ok — untrusted folder refused with its findings, trust.add lets it start")

        # ---- sessions.create / stop / delete (no model turn)
        r = await rpc(ws, "sessions.create",
                      {"cwd": CWD, "model": "haiku", "title": "rpc-e2e"})
        assert r["ok"] and r["result"]["sid"], r
        sid = r["result"]["sid"]
        r = await rpc(ws, "sessions.stop", {"sid": sid})
        assert r["ok"], r
        r = await rpc(ws, "sessions.rename", {"sid": sid, "title": "  renamed  e2e "})
        assert r["ok"] and r["result"]["title"] == "renamed e2e", r
        r = await rpc(ws, "sessions.rename", {"sid": sid, "title": "   "})
        assert not r["ok"] and r["error"]["code"] == "bad_request", r
        r = await rpc(ws, "sessions.rename", {"sid": "zzz", "title": "x"})
        assert not r["ok"] and r["error"]["code"] == "not_found", r
        print("sessions.rename ok — trimmed, empty refused, unknown sid refused")
        # no message was sent, so there is no prompt to rewind to
        # (a real rewind needs a turn with claude — tested by hand, README)
        for params in ({"sid": sid, "seq": 1, "dry": True}, {"sid": sid, "seq": "1"}):
            r = await rpc(ws, "sessions.rewind", params)
            assert not r["ok"] and r["error"]["code"] == "bad_request", (params, r)
        r = await rpc(ws, "sessions.rewind", {"sid": "zzz", "seq": 1})
        assert not r["ok"] and r["error"]["code"] == "not_found", r
        print("sessions.rewind ok — no such prompt, bad seq, unknown sid refused")
        # a session outside SCOPED stays alive for the scope checks below
        r = await rpc(ws, "sessions.create", {"cwd": CWD, "title": "rpc-e2e-outside"})
        assert r["ok"], r
        outside_sid = r["result"]["sid"]
        r = await rpc(ws, "sessions.stop", {"sid": outside_sid})
        assert r["ok"], r
        r = await rpc(ws, "sessions.delete", {"sid": sid})
        assert r["ok"] and r["result"]["ok"], r
        print(f"sessions.create/stop/delete ok — sid {sid}")

        # ---- the runner limit: refusal carries what may be stopped
        r = await rpc(ws, "sessions.create", {"cwd": CWD, "title": "limit-holder"})
        assert r["ok"], r
        holder = r["result"]["sid"]
        # create() starts the process in the background; wait for it
        for _ in range(100):
            if srv.manager.get(holder).client is not None:
                break
            await asyncio.sleep(0.1)
        assert srv.manager.get(holder).client is not None, "holder never started"
        limit_was, srv.manager.max_runners = srv.manager.max_runners, 1
        try:
            # a session nobody wrote to is stopped to make room, whatever
            # its folder — no refusal
            r = await rpc(ws, "sessions.create", {"cwd": CWD, "title": "reclaims-holder"})
            assert r["ok"], r
            assert srv.manager.get(holder).client is None, "empty holder not reclaimed"
            taker = r["result"]["sid"]
            await rpc(ws, "sessions.stop", {"sid": taker})
            await rpc(ws, "sessions.delete", {"sid": taker})
            # a used one is not: the refusal lists it instead (marked used
            # by hand — sending a real message would cost tokens)
            h = srv.manager.get(holder)
            await h.start()
            h.events.append({"kind": "user_text", "text": "in use"})
            r = await rpc(ws, "sessions.create", {"cwd": CWD, "title": "over-limit"})
            assert not r["ok"] and r["error"]["code"] == "runner_limit", r
            data = r["error"].get("data") or {}
            assert data.get("limit") == 1, data
            # a candidate only appears once the holder is past "starting";
            # whenever it does, it must be in scope and carry a size hint
            cands = data.get("candidates") or []
            for c in cands:
                assert Path(c["cwd"]).is_relative_to(home), c
                assert c["resume_tokens"] is None or c["resume_tokens"] >= 0, c
                assert c["title"] and c["sid"], c
                assert c["model"], c   # tokens without a model mislead
            assert any(c["sid"] == holder for c in cands), (holder, cands)
        finally:
            srv.manager.max_runners = limit_was
        await rpc(ws, "sessions.stop", {"sid": holder})
        await rpc(ws, "sessions.delete", {"sid": holder})
        print(f"runner limit ok — refused at 1, {len(cands)} candidate(s) offered")

        # ---- error paths
        r = await rpc(ws, "sessions.create", {"cwd": "/etc"})
        assert not r["ok"] and r["error"]["code"] == "forbidden", \
            f"cwd jail failed: {r}"
        r = await rpc(ws, "sessions.create", {"cwd": ""})
        assert not r["ok"] and r["error"]["code"] == "bad_request", r
        r = await rpc(ws, "sessions.stop", {"sid": "zzz"})
        assert not r["ok"] and r["error"]["code"] == "not_found", r
        for bad, why in (({"mode": "yolo"}, "mode"), ({"model": "--help"}, "model"),
                         ({"resume": "../../etc/x"}, "resume")):
            r = await rpc(ws, "sessions.create", {"cwd": CWD, **bad})
            assert not r["ok"] and r["error"]["code"] == "bad_request", f"{why}: {r}"
        r = await rpc(ws, "projects.sessions", {"cwd": "/etc"})
        assert not r["ok"] and r["error"]["code"] == "forbidden", r
        r = await rpc(ws, "projects.preview", {"session_id": "x", "cwd": "/etc"})
        assert not r["ok"] and r["error"]["code"] == "forbidden", r
        r = await rpc(ws, "projects.preview", {"session_id": "../../x"})
        assert not r["ok"] and r["error"]["code"] == "bad_request", r
        r = await rpc(ws, "library", {"cwd": "/etc"})
        assert not r["ok"] and r["error"]["code"] == "forbidden", r
        print("validation ok — mode/model/resume/session_id, cwd jails")
        r = await rpc(ws, "does.not.exist")
        assert not r["ok"] and r["error"]["code"] == "unknown_method", r
        driven.discard("does.not.exist")
        print("error paths ok — cwd jail, bad_request, not_found, unknown_method")

    # ---- manager device: sees everyone, may revoke others
    mgr_hdr = {"Cookie": f"{srv.COOKIE_PLAIN}={mgr_token}", "Origin": origin}
    async with websockets.connect(WS_URL, additional_headers=mgr_hdr) as ws:
        await ws.recv(); await ws.recv()   # hello, sessions
        r = await rpc(ws, "devices.list")
        assert r["ok"] and r["result"]["can_manage"] is True, r
        mine = {d["id"]: d for d in r["result"]["devices"]}
        assert {dev_id, other_id, mgr_id, scoped_id} <= set(mine), r
        assert mine[mgr_id]["current"] and mine[mgr_id]["manage_devices"], r
        assert mine[scoped_id]["roots"] == [SCOPED], r
        assert all("token_sha256" not in d for d in mine.values()), "hash leaked"
        r = await rpc(ws, "devices.rename", {"id": other_id, "name": "renamed by manager"})
        assert r["ok"], r
        assert srv.devices.verify(other_token)["name"] == "renamed by manager"
        r = await rpc(ws, "devices.revoke", {"id": other_id})
        assert r["ok"] and r["result"]["current"] is False, r
        r = await rpc(ws, "devices.revoke", {"id": other_id})
        assert not r["ok"] and r["error"]["code"] == "not_found", r
        assert srv.devices.verify(other_token) is None
        # Agent SDK page: read-only status (no PyPI check), and install /
        # restart only down paths that change nothing — never a real pip run
        # or server restart ("now"/"idle" are not sent at all). Install needs
        # a fresh passkey confirmation first, which a test device cannot give.
        r = await rpc(ws, "sdk.status")
        assert r["ok"] and "installed" in r["result"], r
        r = await rpc(ws, "sdk.install", {"version": "0.0.0"})
        assert not r["ok"] and r["error"]["code"] in ("forbidden", "stepup_required"), r
        r = await rpc(ws, "sdk.install", {"version": "0.0.0", "cli": "0.0.0"})
        assert not r["ok"] and r["error"]["code"] in ("forbidden", "stepup_required"), r
        r = await rpc(ws, "sdk.cli_update", {"version": "0.0.0"})
        assert not r["ok"] and r["error"]["code"] in ("forbidden", "stepup_required"), r
        r = await rpc(ws, "sdk.restart", {"when": "sometime"})
        assert not r["ok"] and r["error"]["code"] == "bad_request", r
        r = await rpc(ws, "sdk.restart", {"when": "cancel"})
        assert r["ok"], r
    print("manager ok — lists all devices with grants, renames and revokes others")
    print("sdk ok — status read-only, install and CLI update refused without a passkey, "
          "restart only bad_request/cancel")

    # ---- scoped device: only SCOPED is reachable
    sc_hdr = {"Cookie": f"{srv.COOKIE_PLAIN}={scoped_token}", "Origin": origin}
    async with websockets.connect(WS_URL, additional_headers=sc_hdr) as ws:
        await ws.recv()
        first = json.loads(await ws.recv())
        assert all(s["sid"] != outside_sid for s in first["sessions"]), "leaked session"
        r = await rpc(ws, "state")
        assert all(s["sid"] != outside_sid for s in r["result"]["sessions"]), r
        r = await rpc(ws, "browse")
        assert r["ok"] and r["result"]["path"] == SCOPED and r["result"]["parent"] is None, r
        r = await rpc(ws, "browse", {"path": CWD})
        assert r["result"]["path"] == SCOPED, "browse escaped the device scope"
        r = await rpc(ws, "sessions.create", {"cwd": CWD})
        assert not r["ok"] and r["error"]["code"] == "forbidden", r
        r = await rpc(ws, "projects.sessions", {"cwd": CWD})
        assert not r["ok"] and r["error"]["code"] == "forbidden", r
        r = await rpc(ws, "projects.recent")
        assert all(Path(p["path"]).is_relative_to(SCOPED) for p in r["result"]["projects"]), r
        # ---- files: only with the grant, only inside the scope
        (Path(SCOPED) / "note.txt").write_text("hi\n")
        r = await rpc(ws, "files.list")
        assert not r["ok"] and r["error"]["code"] == "forbidden", r
        srv.devices.set_grants(scoped_id, files=True)
        r = await rpc(ws, "files.list")
        assert r["ok"] and r["result"]["path"] == SCOPED \
            and any(e["name"] == "note.txt" for e in r["result"]["entries"]), r
        r = await rpc(ws, "files.list", {"path": CWD})
        assert not r["ok"] and r["error"]["code"] == "forbidden", "files escaped the scope"
        r = await rpc(ws, "files.text", {"path": str(Path(SCOPED) / "note.txt")})
        assert r["ok"] and r["result"]["text"] == "hi\n", r
        r = await rpc(ws, "files.text", {"path": "/etc/hostname"})
        assert not r["ok"] and r["error"]["code"] == "forbidden", r
        srv.devices.set_grants(scoped_id, files=False)
        print("files ok — grant required, scope holds")
        r = await rpc(ws, "sessions.stop", {"sid": outside_sid})
        assert not r["ok"] and r["error"]["code"] == "not_found", r
        r = await rpc(ws, "sessions.delete", {"sid": outside_sid})
        assert not r["ok"] and r["error"]["code"] == "not_found", r
        await ws.send(json.dumps({"type": "attach", "session_id": outside_sid}))
        while True:
            msg = json.loads(await asyncio.wait_for(ws.recv(), 10))
            if msg.get("type") in ("error", "attached"):
                break
        assert msg["type"] == "error", f"attached outside scope: {msg}"
        srv.devices.set_terminal(scoped_id, True)   # scope, not the grant, must stop it
        async with websockets.connect(f"{WS_URL}/term/{outside_sid}",
                                      additional_headers=sc_hdr) as term:
            msg = json.loads(await asyncio.wait_for(term.recv(), 10))
            assert msg == {"type": "exit", "reason": "No such session"}, msg
        # CLI `devices set --root` narrows/widens a live connection
        srv.devices.set_roots(scoped_id, None)
        r = await rpc(ws, "projects.sessions", {"cwd": CWD})
        assert r["ok"], f"set-roots to all must apply at once: {r}"
    print("scope ok — sessions/browse/projects/attach confined, set-roots live")

    # ---- True View grant (pair --terminal / devices set --terminal)
    async def term_exit_reason(hdr):
        async with websockets.connect(f"{WS_URL}/term/{outside_sid}",
                                      additional_headers=hdr) as term:
            try:
                raw = await asyncio.wait_for(term.recv(), 10)
            except websockets.ConnectionClosed:
                return None
            if isinstance(raw, bytes):   # a real TUI started: stop it again
                await term.send('{"type":"kill"}')
                return "<terminal output>"
        msg = json.loads(raw)
        return msg.get("reason") if msg.get("type") == "exit" else msg

    async with websockets.connect(WS_URL, additional_headers=good) as ws:
        await ws.recv(); await ws.recv()
        r = await rpc(ws, "state")
        assert r["result"]["can_terminal"] is False, r
    reason = await term_exit_reason(good)
    assert reason == srv.TERMINAL_DENIED, f"terminal without grant: {reason}"
    srv.devices.set_terminal(dev_id, True)
    async with websockets.connect(WS_URL, additional_headers=good) as ws:
        await ws.recv(); await ws.recv()
        r = await rpc(ws, "state")
        assert r["result"]["can_terminal"] is True, r
    reason = await term_exit_reason(good)
    # past the grant check: attach_terminal itself answers (e.g. no Claude
    # session yet, or a live PTY) — anything but the denial
    assert reason != srv.TERMINAL_DENIED, f"grant not applied: {reason}"
    srv.devices.set_terminal(dev_id, False)
    print(f"terminal grant ok — denied without it, allowed with it ({reason!r})")

    async with websockets.connect(WS_URL, additional_headers=good) as ws:
        await ws.recv(); await ws.recv()
        r = await rpc(ws, "sessions.delete", {"sid": outside_sid})
        assert r["ok"], r
    for i in (mgr_id, scoped_id):
        srv.devices.revoke(i)

    missing = set(METHODS) - driven
    assert not missing, f"rpc methods not driven: {missing}"
    print(f"all {len(METHODS)} rpc methods driven over /ws")

    # ---- auth negatives (cookie-only + Origin allowlist, no query tokens)
    await expect_close(4401, "no cookie")
    await expect_close(4401, "wrong cookie",
                       additional_headers={"Cookie": f"{srv.COOKIE_PLAIN}=wrong"})
    await expect_close(4401, "revoked device", additional_headers={
        "Cookie": f"{srv.COOKIE_PLAIN}={other_token}", "Origin": origin})
    await expect_close(4401, "?token= query auth removed",
                       url=f"{WS_URL}?token={token}")
    await expect_close(4403, "cross-origin browser upgrade", additional_headers={
        "Cookie": f"{srv.COOKIE_PLAIN}={token}", "Origin": "https://evil.example"})

    async with httpx.AsyncClient() as client:
        r = await client.get(f"{BASE}/api/state", params={"token": token})
        assert r.status_code == 401, "REST ?token= auth must be gone"
        r = await client.get(f"{BASE}/api/state",
                             headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 401, "Bearer auth must be gone"
        r = await client.get(f"{BASE}/api/state",
                             cookies={srv.COOKIE_PLAIN: token})
        assert r.status_code == 200, r.text
    print("REST negatives ok — ?token= and Bearer dead, cookie works")

    # ---- revoke while connected: the next message closes with 4401
    async with websockets.connect(WS_URL, additional_headers=good) as ws:
        await ws.recv(); await ws.recv()   # hello, sessions
        srv.devices.revoke(dev_id)         # e.g. `c4ai revoke <id>`
        await ws.send('{"type":"ping"}')
        try:
            while True:
                await asyncio.wait_for(ws.recv(), 10)
        except websockets.ConnectionClosed as e:
            assert e.rcvd and e.rcvd.code == 4401, f"expected 4401, got {e.rcvd}"
    print("live revoke ok — open socket closed with 4401 on next message")

    # ---- failed-login throttle (last: it locks /api/login for a minute)
    srv._login_failures.clear()  # earlier negatives count toward the window
    async with httpx.AsyncClient() as client:
        codes = [(await client.post(f"{BASE}/api/login",
                                    json={"code": "AAAAA-AAAAA"})).status_code
                 for _ in range(srv.LOGIN_FAIL_LIMIT + 1)]
        assert codes == [401] * srv.LOGIN_FAIL_LIMIT + [429], codes
        valid = issue_pairing_code()
        r = await client.post(f"{BASE}/api/login", json={"code": valid})
        assert r.status_code == 429, "throttle must hold even for a valid code"
    assert redeem_pairing_code(valid), "throttled code must not be consumed"
    srv._login_failures.clear()
    print(f"login throttle ok — {codes.count(401)}x401 then 429")

    server.should_exit = True
    await task
    print("\nRPC E2E PASS")


def cleanup() -> None:
    if SERVER is not None:
        SERVER.should_exit = True
    left = [i for i in CREATED if srv.devices.revoke(i)]
    if left:
        print(f"cleaned up {len(left)} test device(s) left by a failed run")


try:
    asyncio.run(main())
finally:
    cleanup()
