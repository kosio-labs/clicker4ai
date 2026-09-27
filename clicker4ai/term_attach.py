"""True View attach: connects one WebSocket client (main.py /ws/term) to
the session's PTY terminal.

The transport hides how bytes reach the client:

    send_bytes(data)  raw PTY output (binary WS frame)
    send_json(obj)    control messages {"type": "exit"|"pong"} (text frame)
    recv_json()       next client control dict ({input,resize,ping,kill}),
                      or None once the client is gone
    close(code)       tear down the client leg (4413: too slow, reopen)
    quiet_exempt()    whether the device behind the last message may type
                      in quiet hours (quiet.py)
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Protocol

from . import trust
from .quiet import mark_term_writer
from .sessions import BUSY_STATES, RunnerLimit, SessionManager, now_ms
from .terminal import TerminalError, TerminalManager, claude_argv


class TermTransport(Protocol):
    async def send_bytes(self, data: bytes) -> None: ...
    async def send_json(self, obj: dict) -> None: ...
    async def recv_json(self) -> dict | None: ...
    async def close(self, code: int = 1000) -> None: ...
    def quiet_exempt(self) -> bool: ...


def _chat_busy(runner) -> bool:
    """The chat's own process is in a turn — or still starting (no client
    yet), which a terminal must not resume beside: the start is for a
    message, and nothing would hand that process over."""
    return runner._connecting or (runner.client is not None
                                  and runner.state in BUSY_STATES)


async def attach_terminal(m: SessionManager, terms: TerminalManager,
                          sid: str, transport: TermTransport,
                          spawn_denial: str | None = None) -> None:
    """Attach one client to the session's PTY terminal, spawning
    `claude --resume <claude_id>` on first attach (`--session-id <new id>`
    before the chat's first message). `spawn_denial`: the
    client may join a running terminal but not start one (the reason it
    gets instead). Returns when the client leg is gone (transport closed /
    recv_json() -> None)."""

    async def bail(reason: str) -> None:
        await transport.send_json({"type": "exit", "reason": reason})
        await transport.close()

    runner = m.get(sid)
    if runner is None:
        await bail("No such session")
        return
    # a terminal closed a moment ago may still be exiting: a new one would
    # resume the transcript beside it
    await terms.wait_closed(sid)
    if _chat_busy(runner):
        await bail("Session is busy in the app — wait for the turn to finish")
        return
    term = terms.get(sid)
    if term is None and runner.rewind_at is not None:
        # a TUI would resume the whole transcript, dropped turns included
        await bail("Send the edited prompt first — the rewind takes effect with it")
        return
    if term is None and spawn_denial:
        await bail(spawn_denial)
        return
    if term is None:
        # the TUI goes by the CLI's own trust record, which may count a
        # trusted parent; the app's rule wants this exact folder (trust.py)
        try:
            await asyncio.to_thread(trust.check, runner.cwd)
        except trust.Untrusted:
            await bail("This folder has project settings Claude Code runs on "
                       "its own — trust it in the chat first")
            return
    if term is None:
        # a new terminal is a new `claude` process, so it takes a runner
        # slot (the chat's own process, if any, is handed over below)
        try:
            await m.make_room(runner)
        except RunnerLimit as e:
            await bail(f"{e.limit} sessions are already running — stop one "
                       "(Sessions, or the tab menu) to open True View")
            return
        if _chat_busy(runner):
            # a turn began while make_room was stopping another session
            await bail("Session is busy in the app — wait for the turn to finish")
            return
        runner._term_opening = True   # holds the slot until the PTY exists
    try:
        if runner.client is not None:
            await runner.detach_for_terminal()
        if term is None:
            try:
                cid, new = runner.id_for_terminal()
                argv = claude_argv(cid, runner.model, runner.mode, sid, new=new)
                term = terms.open(sid, argv, runner.cwd)
                term.writer_exempt = transport.quiet_exempt()
                mark_term_writer(sid, term.writer_exempt)
                # in use from now: it sorts with the running sessions, and
                # the chat says where the session is being driven
                runner.last_active = now_ms()
                runner.set_status("detached", "In terminal view")
                runner.save_meta()
                m.schedule_sessions_update()   # the session shows as running
            except (TerminalError, OSError) as e:
                await bail(f"Could not start terminal: {e}")
                return
    finally:
        runner._term_opening = False

    q: asyncio.Queue = asyncio.Queue()
    replay = term.attach(q)

    async def pump() -> None:
        if replay:
            await transport.send_bytes(replay)
        while True:
            item = await q.get()
            if isinstance(item, dict):
                if item.get("type") == "too_slow":
                    await transport.close(4413)
                    return
                await transport.send_json(item)
                if item.get("type") == "exit":
                    await transport.close()
                    return
            else:
                await transport.send_bytes(item)
                term.sent(q, len(item))

    sender = asyncio.create_task(pump())
    try:
        while True:
            data = await transport.recv_json()
            if data is None:
                break
            dtype = data.get("type")
            if dtype == "input":
                # the quiet-hours hook judges a prompt by whoever typed last
                exempt = transport.quiet_exempt()
                if exempt != term.writer_exempt:
                    term.writer_exempt = exempt
                    mark_term_writer(sid, exempt)
                term.write(str(data.get("data", "")).encode())
            elif dtype == "resize":
                cols, rows = data.get("cols"), data.get("rows")
                # the app sends ints; anything else is ignored, not fatal
                if type(cols) is int and type(rows) is int:
                    term.resize(cols, rows)
            elif dtype == "ping":
                q.put_nowait({"type": "pong"})
            elif dtype == "kill":
                terms.close(sid, "Terminal ended")
    finally:
        term.detach(q)
        sender.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await sender
