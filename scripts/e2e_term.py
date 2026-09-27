"""Verify True View: the PTY terminal layer (spawn, echo roundtrip, resize,
scrollback replay, clean close) and a real `claude --resume` TUI producing
ANSI output for an existing session.

Run: .venv/bin/python scripts/e2e_term.py
"""

import asyncio
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _testdata import isolate  # noqa: E402
isolate("e2e_term")   # never the live ~/.clicker4ai

from clicker4ai.sessions import SessionManager  # noqa: E402
from clicker4ai.terminal import TerminalManager, TerminalSession, claude_argv  # noqa: E402

CWD = str(Path(__file__).resolve().parent.parent / ".scratch" / "e2e-playground")
Path(CWD).mkdir(parents=True, exist_ok=True)


async def drain_until(q: asyncio.Queue, pred, timeout=20.0) -> bytes:
    """Collect bytes from the queue until pred(all_bytes) or timeout."""
    buf = bytearray()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        try:
            item = await asyncio.wait_for(q.get(), deadline - loop.time())
        except asyncio.TimeoutError:
            break
        if isinstance(item, dict):
            buf.extend(b"<EXIT:%s>" % item.get("reason", "").encode())
            break
        buf.extend(item)
        if pred(bytes(buf)):
            break
    return bytes(buf)


async def test_pty_layer():
    print("-- PTY layer (sh) --")
    term = TerminalSession("t1", ["/bin/sh", "-c", "echo READY; cat"], CWD)
    q = asyncio.Queue()
    assert term.attach(q) == b""
    term.spawn()
    out = await drain_until(q, lambda b: b"READY" in b, 10)
    assert b"READY" in out, f"no READY from sh: {out!r}"

    term.write(b"marco\n")
    out = await drain_until(q, lambda b: b"marco" in b, 10)
    assert b"marco" in out, f"echo roundtrip failed: {out!r}"

    term.resize(120, 40)  # must not raise

    # a late client replays scrollback
    q2 = asyncio.Queue()
    replay = term.attach(q2)
    assert b"READY" in replay and b"marco" in replay, f"bad replay: {replay!r}"

    term.close("test over")
    exit_msg = await asyncio.wait_for(q2.get(), 10)
    while not isinstance(exit_msg, dict):
        exit_msg = await asyncio.wait_for(q2.get(), 10)
    assert exit_msg["type"] == "exit" and exit_msg["reason"] == "test over", exit_msg
    assert not term.alive
    print("PTY LAYER PASS")


async def test_claude_tui():
    print("-- real claude TUI --")
    assert shutil.which("claude"), "claude CLI not on PATH"
    m = SessionManager()
    r = await m.create(cwd=CWD, model="haiku", mode="default")

    # one cheap turn so the session has a claude transcript to resume
    await r.send("Reply with exactly: OK")
    for _ in range(600):
        await asyncio.sleep(0.3)
        if r.state == "idle" and r.claude_ids:
            break
    assert r.claude_ids, f"no claude id after turn (state={r.state})"
    cid = r.claude_ids[-1]
    print(f"  session {r.sid} → claude {cid[:8]}…")
    await r.detach_for_terminal()
    assert r.client is None and r.state == "detached"

    terms = TerminalManager()
    term = terms.open(r.sid, claude_argv(cid, r.model, r.mode, r.sid), r.cwd)
    q = asyncio.Queue()
    term.attach(q)
    term.resize(100, 30)
    out = await drain_until(q, lambda b: len(b) > 800 and b"\x1b[" in b, 45)
    assert b"\x1b[" in out, f"no ANSI output from TUI ({len(out)} bytes)"
    assert b"<EXIT" not in out, f"TUI died early: {out[-400:]!r}"
    print(f"  TUI drew {len(out)} bytes of terminal output")

    terms.close(r.sid, "e2e done")
    exit_msg = await drain_until(q, lambda b: b"<EXIT" in b, 15)
    assert b"<EXIT" in exit_msg, "no exit after close"
    assert terms.get(r.sid) is None
    await m.delete(r.sid)
    print("CLAUDE TUI PASS")


async def main():
    await test_pty_layer()
    await test_claude_tui()
    print("TRUE VIEW E2E PASS")


asyncio.run(main())
