"""Focused test: the can_use_tool → phone approval → tool runs path."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _testdata import isolate  # noqa: E402
isolate("e2e_permission")   # never the live ~/.clicker4ai

from clicker4ai.sessions import SessionManager  # noqa: E402

CWD = str(Path(__file__).resolve().parent.parent / ".scratch" / "e2e-playground")
Path(CWD).mkdir(parents=True, exist_ok=True)
marker = Path(CWD) / "perm-test.txt"
marker.unlink(missing_ok=True)


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

    await r.send("Use the Write tool to create the file perm-test.txt containing "
                 "exactly: approved-by-phone\nDo nothing else.")

    perm = None
    denied_first = False
    while True:
        payload = await asyncio.wait_for(conn.q.get(), 120)
        if payload.get("type") != "event":
            continue
        ev = payload["ev"]
        k = ev.get("kind")
        if k == "permission":
            perm = ev
            print(f"permission fired: tool={ev['tool']} title={ev['title']!r}")
            print(f"  display_name={ev['display_name']!r} suggestions={ev['suggestions']}")
            if not denied_first:
                # exercise the deny path first
                denied_first = True
                r.respond_permission(ev["request_id"], {
                    "behavior": "deny",
                    "message": "Denied once for testing — please try the exact same Write again.",
                })
                print("  → denied once (testing deny path)")
            else:
                r.respond_permission(ev["request_id"], {"behavior": "allow"})
                print("  → allowed")
        elif k == "permission_resolved":
            print(f"resolved: {ev['behavior']}")
        elif k == "notice" and ev.get("level") == "error":
            print("ERROR:", ev["text"])
        elif k == "result":
            print(f"turn done (is_error={ev['is_error']})")
            if denied_first and perm and marker.exists():
                break
            if not marker.exists():
                # Claude may need another nudge after the deny; if the turn
                # ended without a second attempt, ask again.
                if denied_first:
                    await r.send("Now please actually write perm-test.txt as asked.")
                else:
                    break

    content = marker.read_text().strip()
    assert perm is not None, "permission callback never fired"
    assert content == "approved-by-phone", f"unexpected content: {content!r}"
    print(f"PERMISSION E2E PASS — file written after phone-style approval: {content!r}")
    await m.delete(r.sid)


asyncio.run(main())
