"""Verify the terminal-parity resume contract:

1. History backfill — resuming a past claude session shows its messages.
2. Same-session continuation — the resumed session keeps the SAME claude
   session id (no fork) and remembers earlier context.
3. Shared transcript — phone turns append to the same transcript file, so
   `claude --resume` in the terminal sees them afterwards.

Uses haiku; costs ~2 short turns. Run: .venv/bin/python scripts/e2e_resume.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _testdata import isolate  # noqa: E402
isolate("e2e_resume")   # never the live ~/.clicker4ai

from claude_agent_sdk import get_session_messages, list_sessions  # noqa: E402

from clicker4ai.sessions import SessionManager  # noqa: E402

CWD = str(Path(__file__).resolve().parent.parent / ".scratch" / "e2e-playground")
Path(CWD).mkdir(parents=True, exist_ok=True)


class FakeConn:
    def __init__(self):
        self.subs = set()
        self.q = asyncio.Queue()

    def push(self, payload):
        self.q.put_nowait(payload)


async def wait_result(conn, sid, collect=None, timeout=120):
    while True:
        payload = await asyncio.wait_for(conn.q.get(), timeout)
        if payload.get("type") != "event" or payload.get("session_id") != sid:
            continue
        ev = payload["ev"]
        if collect is not None:
            collect.append(ev)
        if ev.get("kind") == "notice" and ev.get("level") == "error":
            print("ERROR NOTICE:", ev["text"])
        if ev.get("kind") == "result":
            return ev


async def main():
    m = SessionManager()
    conn = FakeConn()
    m.conns.add(conn)

    # ---- phase 1: seed a session with a memorable fact
    r1 = await m.create(cwd=CWD, model="haiku", mode="default")
    conn.subs.add(r1.sid)
    await r1.send("The codeword is mango-427. Acknowledge with just: OK")
    await wait_result(conn, r1.sid)
    claude_id = r1.claude_ids[-1]
    assert claude_id, "no claude session id captured"
    n_before = len(get_session_messages(claude_id, directory=CWD))
    print(f"P1 seeded claude session {claude_id[:8]}… transcript rows={n_before}")

    # Simulate closing the terminal: drop the UI session entirely.
    await m.delete(r1.sid)
    conn.subs.discard(r1.sid)

    # ---- phase 2: "open the existing session from the phone"
    r2 = await m.create(cwd=CWD, model="haiku", mode="default",
                        resume=claude_id, title="resume test")
    conn.subs.add(r2.sid)

    got = []
    hist_user = hist_divider = None
    while hist_divider is None:
        payload = await asyncio.wait_for(conn.q.get(), 60)
        if payload.get("type") != "event" or payload.get("session_id") != r2.sid:
            continue
        ev = payload["ev"]
        got.append(ev)
        if ev.get("kind") == "user_text" and "mango-427" in (ev.get("text") or ""):
            hist_user = ev
        if ev.get("kind") == "resumed":
            hist_divider = ev
    assert hist_user, "history backfill missing the seeded user message"
    kinds = [e.get("kind") for e in got]
    print(f"P2 history backfilled: kinds={kinds} "
          f"(shown={hist_divider['shown']} total={hist_divider['total']})")

    # ---- phase 3: continuation must remember and reuse the SAME session id
    await r2.send("What is the codeword? Reply with just the codeword.")
    evs = []
    await wait_result(conn, r2.sid, collect=evs)
    reply = " ".join(e.get("text") or "" for e in evs if e.get("kind") == "text")
    assert "mango-427" in reply, f"context lost on resume: {reply!r}"
    ids_now = set(r2.claude_ids)
    same_id = ids_now == {claude_id}
    print(f"P3 continuation reply={reply.strip()!r} same_session_id={same_id} "
          f"ids={[i[:8] for i in ids_now]}")

    # ---- phase 4: transcript on disk grew under the original id
    n_after = len(get_session_messages(claude_id, directory=CWD))
    grew = n_after > n_before
    print(f"P4 transcript rows {n_before} → {n_after} (grew={grew})")
    sess_ids = [s.session_id for s in list_sessions(directory=CWD)]
    dupes = [i for i in ids_now if i != claude_id]
    print(f"P4 sessions in project now: {len(sess_ids)}; "
          f"new ids created by resume: {[d[:8] for d in dupes] or 'none'}")

    assert same_id, ("RESUME FORKED: claude gave a new session id "
                     f"{[i[:8] for i in ids_now]} — terminal handoff broken")
    assert grew, "transcript did not grow under the original session id"

    await m.delete(r2.sid)
    print("RESUME E2E PASS — same session id, shared transcript, history shown")


asyncio.run(main())
