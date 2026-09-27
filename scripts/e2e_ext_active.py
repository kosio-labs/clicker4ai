"""Verify terminal-turn activity detection on session cards.

Phase 1  seed a webapp session, then stop its subprocess (detached —
         the normal state for cards driven from a terminal).
Phase 2  run a terminal turn (`claude -p --resume`) and assert the
         session flips to state=working while rows land (this is what
         puts the "active" chip + pulsing dot on cards).
Phase 3  assert it reverts to detached after the quiet window
         (patched down from 30s to keep the test fast).

Run: .venv/bin/python scripts/e2e_ext_active.py
"""

import asyncio
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _testdata import isolate  # noqa: E402
isolate("e2e_ext_active")   # never the live ~/.clicker4ai

import clicker4ai.sessions as sessions  # noqa: E402
from clicker4ai.sessions import SessionManager  # noqa: E402

sessions.EXT_TURN_QUIET_SECS = 6.0  # fast revert for the test

CWD = str(Path(__file__).resolve().parent.parent / ".scratch" / "e2e-playground")
Path(CWD).mkdir(parents=True, exist_ok=True)


class FakeConn:
    def __init__(self):
        self.subs = set()
        self.q = asyncio.Queue()

    def push(self, payload):
        self.q.put_nowait(payload)


async def wait_status(conn, sid, pred, timeout=120):
    deadline = time.monotonic() + timeout
    while True:
        remain = deadline - time.monotonic()
        if remain <= 0:
            raise TimeoutError("timed out waiting for status")
        payload = await asyncio.wait_for(conn.q.get(), remain)
        if payload.get("type") != "event" or payload.get("session_id") != sid:
            continue
        ev = payload["ev"]
        if ev.get("kind") == "status" and pred(ev):
            return ev


async def main():
    m = SessionManager()
    conn = FakeConn()
    m.conns.add(conn)

    # ---- Phase 1: seed, then detach ----
    r = await m.create(cwd=CWD, model="haiku", mode="default")
    conn.subs.add(r.sid)
    await r.send("Reply with exactly: OK")
    await wait_status(conn, r.sid, lambda ev: ev["state"] == "idle"
                      and ev.get("detail", "").startswith("Done"))
    cid = r.claude_ids[-1]
    await r.stop()
    assert r.state == "detached" and r.client is None
    print(f"P1 seeded + detached, claude_id={cid}")

    # ---- Phase 2: terminal turn must mark the session working ----
    claude = shutil.which("claude") or str(Path.home() / ".claude" / "local" / "claude")
    proc = await asyncio.create_subprocess_exec(
        claude, "-p", "--resume", cid, "--model", "haiku",
        "Reply with exactly: NOTED",
        cwd=CWD,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    t0 = time.monotonic()
    ev = await wait_status(conn, r.sid, lambda ev: ev["state"] == "working")
    print(f"P2 state=working {time.monotonic() - t0:.1f}s after terminal turn "
          f"started (detail={ev.get('detail')!r})")
    snap = r.snapshot()
    assert snap["state"] == "working", snap["state"]
    _, stderr = await proc.communicate()
    assert proc.returncode == 0, f"terminal claude failed: {stderr.decode()[-400:]}"

    # ---- Phase 3: reverts to detached after the quiet window ----
    t0 = time.monotonic()
    ev = await wait_status(conn, r.sid, lambda ev: ev["state"] == "detached",
                           timeout=60)
    print(f"P3 reverted to detached {time.monotonic() - t0:.1f}s after turn end "
          f"(detail={ev.get('detail')!r})")
    assert not r._ext_turn

    await m.delete(r.sid)
    print("EXT-ACTIVE E2E PASS — terminal turns show as working, then revert")


asyncio.run(main())
