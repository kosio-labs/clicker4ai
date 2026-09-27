"""Verify live terminal↔webapp sync.

Phase 1  seed a webapp session with a codeword, wait for the turn to end.
Phase 2  append a turn from "the terminal" — a real `claude -p --resume`
         subprocess on the same session id — and assert the webapp session
         mirrors both the user message and the reply live (no close/reopen),
         marked external.
Phase 3  send from the webapp and assert the reply knows BOTH codewords
         (stale-context rotation resumed with the terminal turns included)
         and the claude session id never changed (no fork).

Run: .venv/bin/python scripts/e2e_sync.py
"""

import asyncio
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _testdata import isolate  # noqa: E402
isolate("e2e_sync")   # never the live ~/.clicker4ai

from clicker4ai.sessions import SessionManager  # noqa: E402

CWD = str(Path(__file__).resolve().parent.parent / ".scratch" / "e2e-playground")
Path(CWD).mkdir(parents=True, exist_ok=True)


class FakeConn:
    def __init__(self):
        self.subs = set()
        self.q = asyncio.Queue()

    def push(self, payload):
        self.q.put_nowait(payload)


async def wait_for(conn, sid, pred, timeout=180):
    deadline = time.monotonic() + timeout
    while True:
        remain = deadline - time.monotonic()
        if remain <= 0:
            raise TimeoutError("timed out waiting for event")
        payload = await asyncio.wait_for(conn.q.get(), remain)
        if payload.get("type") != "event" or payload.get("session_id") != sid:
            continue
        ev = payload["ev"]
        if ev.get("kind") == "notice" and ev.get("level") == "error":
            print("  ERROR notice:", ev.get("text"))
        if pred(ev):
            return ev


async def main():
    m = SessionManager()
    conn = FakeConn()
    m.conns.add(conn)

    # ---- Phase 1: seed via webapp ----
    r = await m.create(cwd=CWD, model="haiku", mode="default")
    conn.subs.add(r.sid)
    await r.send("Remember codeword ONE: mango-31. Reply with exactly: OK")
    await wait_for(conn, r.sid, lambda ev: ev.get("kind") == "result")
    cid = r.claude_ids[-1]
    print(f"P1 seeded, claude_id={cid}")

    # ---- Phase 2: a real terminal-style process appends to the session ----
    claude = shutil.which("claude") or str(Path.home() / ".claude" / "local" / "claude")
    proc = await asyncio.create_subprocess_exec(
        claude, "-p", "--resume", cid, "--model", "haiku",
        "Remember codeword TWO: papaya-88. Reply with exactly: NOTED",
        cwd=CWD,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )

    t0 = time.monotonic()
    ev_user = await wait_for(conn, r.sid, lambda ev:
                             ev.get("kind") == "user_text" and ev.get("external")
                             and "papaya-88" in (ev.get("text") or ""))
    ev_text = await wait_for(conn, r.sid, lambda ev:
                             ev.get("kind") == "text" and ev.get("external")
                             and "NOTED" in (ev.get("text") or ""))
    print(f"P2 external turn mirrored live in {time.monotonic() - t0:.1f}s "
          f"(user_text + text, both external) — no close/reopen")
    assert r.context_stale, "external rows must mark the live client stale"
    _, stderr = await proc.communicate()
    assert proc.returncode == 0, f"terminal claude failed: {stderr.decode()[-400:]}"

    # ---- Phase 3: continue from the webapp with full merged context ----
    await r.send("Reply with codeword ONE and codeword TWO, separated by a space. "
                 "Nothing else.")
    reply = await wait_for(conn, r.sid, lambda ev:
                           ev.get("kind") == "text" and not ev.get("external"))
    await wait_for(conn, r.sid, lambda ev: ev.get("kind") == "result")
    txt = reply.get("text", "")
    print(f"P3 webapp reply after rotation: {txt!r}")
    assert "mango-31" in txt and "papaya-88" in txt, "merged context missing a codeword"
    assert set(r.claude_ids) == {cid}, f"session id changed/forked: {r.claude_ids}"
    assert not r.context_stale

    await m.delete(r.sid)
    print("SYNC E2E PASS — terminal turns mirror live; webapp continues with "
          "merged context on the same claude session id")


asyncio.run(main())
