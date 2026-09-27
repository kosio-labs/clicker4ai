"""Live end-to-end test: real `claude` subprocess through the session engine.

Runs three scenarios (uses the haiku alias to keep cost/time low):
  1. plain text turn  → expect streaming deltas + final text + result
  2. Bash permission  → expect permission event, approve it, see tool result
  3. manual /compact  → report whether compact_boundary arrives

Costs a few model turns on the user's account. Run: .venv/bin/python scripts/e2e_live.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _testdata import isolate  # noqa: E402
isolate("e2e_live")   # never the live ~/.clicker4ai

from clicker4ai.sessions import SessionManager  # noqa: E402

CWD = str(Path(__file__).resolve().parent.parent / ".scratch" / "e2e-playground")
Path(CWD).mkdir(parents=True, exist_ok=True)


class FakeConn:
    def __init__(self):
        self.subs = set()
        self.q = asyncio.Queue()

    def push(self, payload):
        self.q.put_nowait(payload)


async def next_ev(conn, sid, timeout=90):
    while True:
        payload = await asyncio.wait_for(conn.q.get(), timeout)
        if payload.get("type") == "event" and payload.get("session_id") == sid:
            return payload["ev"]


async def main():
    m = SessionManager()
    conn = FakeConn()
    m.conns.add(conn)

    print("creating session (haiku)…")
    r = await m.create(cwd=CWD, model="haiku", mode="default")
    conn.subs.add(r.sid)

    # ---- scenario 1: text turn
    await r.send('Reply with exactly the single word: pong')
    saw = {"delta": 0, "text": None, "result": None, "meta": None}
    while True:
        ev = await next_ev(conn, r.sid)
        k = ev.get("kind")
        if k == "delta":
            saw["delta"] += 1
        elif k == "text":
            saw["text"] = ev["text"]
        elif k == "meta":
            saw["meta"] = ev
        elif k == "notice" and ev.get("level") == "error":
            print("ERROR NOTICE:", ev["text"])
        elif k == "result":
            saw["result"] = ev
            break
    assert saw["text"] and "pong" in saw["text"].lower(), f"bad text: {saw['text']!r}"
    assert saw["delta"] > 0, "no streaming deltas seen"
    assert saw["result"]["is_error"] is False
    print(f"S1 PASS — text={saw['text']!r} deltas={saw['delta']} "
          f"cost=${saw['result']['cost_usd']} model={saw['meta']['model'] if saw['meta'] else '?'}")
    print(f"   context: {r.context}")

    # ---- scenario 2: permission flow
    await r.send("Use the Bash tool to run exactly: echo hello-rc-42\nThen tell me it worked.")
    perm_seen = False
    tool_out = ""
    while True:
        ev = await next_ev(conn, r.sid)
        k = ev.get("kind")
        if k == "permission":
            perm_seen = True
            print(f"   permission request: tool={ev['tool']} title={ev['title']!r} "
                  f"suggestions={len(ev['suggestions'])}")
            ok = r.respond_permission(ev["request_id"], {"behavior": "allow"})
            assert ok, "respond_permission returned False"
        elif k == "tool_result":
            tool_out += ev.get("text") or ""
        elif k == "notice" and ev.get("level") == "error":
            print("ERROR NOTICE:", ev["text"])
        elif k == "result":
            break
    assert "hello-rc-42" in tool_out, f"tool output missing marker: {tool_out!r}"
    print(f"S2 PASS — permission_prompted={perm_seen} tool_out={tool_out.strip()!r}")

    # ---- scenario 3: manual /compact probe
    await r.compact()
    compact_seen = False
    try:
        while True:
            ev = await next_ev(conn, r.sid, timeout=60)
            k = ev.get("kind")
            if k == "compact":
                compact_seen = True
                print(f"S3 PASS — compact_boundary arrived (pre_tokens={ev.get('pre_tokens')})")
                break
            if k == "result":
                print(f"S3 result before boundary (subtype={ev.get('subtype')})")
                break
            if k == "text":
                print(f"S3 text response: {ev['text'][:80]!r}")
    except TimeoutError:
        print("S3 TIMEOUT — no compact response in 60s")
    if not compact_seen:
        # boundary may arrive right after the result
        try:
            while True:
                ev = await next_ev(conn, r.sid, timeout=15)
                if ev.get("kind") == "compact":
                    compact_seen = True
                    print(f"S3 PASS (late) — compact_boundary (pre_tokens={ev.get('pre_tokens')})")
                    break
        except TimeoutError:
            pass
    print(f"   /compact supported: {compact_seen}")

    snap = r.snapshot()
    print(f"final snapshot: state={snap['state']} cost={snap['cost_usd']} "
          f"ctx={snap['context_pct']}% claude_id={snap['claude_session_id'][:8] if snap['claude_session_id'] else None}")

    await m.delete(r.sid)
    print("cleaned up. E2E DONE")


asyncio.run(main())
