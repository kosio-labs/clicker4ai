"""Verify turn-end hook feedback: with include_hook_events on, the status
ticker must say what's happening between the final text and the result
(claude-mem's Stop hooks can hold the turn open for 25-120s).

Run: .venv/bin/python scripts/e2e_hooks.py
"""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _testdata import isolate  # noqa: E402
isolate("e2e_hooks")   # never the live ~/.clicker4ai

from clicker4ai.sessions import SessionManager  # noqa: E402

CWD = str(Path(__file__).resolve().parent.parent / ".scratch" / "e2e-playground")
Path(CWD).mkdir(parents=True, exist_ok=True)


class FakeConn:
    def __init__(self):
        self.subs = set()
        self.q = asyncio.Queue()

    def push(self, payload):
        self.q.put_nowait(payload)


async def main():
    m = SessionManager()
    conn = FakeConn()
    m.conns.add(conn)
    r = await m.create(cwd=CWD, model="haiku", mode="default")
    conn.subs.add(r.sid)

    await r.send("Reply with exactly: OK")
    t_text = t_result = None
    hook_details = []
    while True:
        payload = await asyncio.wait_for(conn.q.get(), 180)
        if payload.get("type") != "event" or payload.get("session_id") != r.sid:
            continue
        ev = payload["ev"]
        k = ev.get("kind")
        if k == "text":
            t_text = time.monotonic()
        elif k == "status" and "hook" in (ev.get("detail") or "").lower():
            hook_details.append(ev["detail"])
            print(f"  ticker: {ev['state']} — {ev['detail']!r}")
        elif k == "notice" and ev.get("level") == "error":
            print("ERROR:", ev["text"])
        elif k == "result":
            t_result = time.monotonic()
            break

    gap = (t_result - t_text) if (t_text and t_result) else None
    print(f"text→result gap: {gap:.1f}s" if gap is not None else "no text seen")
    print(f"hook ticker updates seen: {len(hook_details)}")
    assert hook_details, ("no Stop-hook status reached the UI — "
                          "include_hook_events wiring failed")
    await m.delete(r.sid)
    print("HOOK FEEDBACK E2E PASS")


asyncio.run(main())
