"""True View: the real Claude Code TUI, streamed to the browser.

Each terminal owns a PTY running `claude --resume <claude_session_id>`
(`--session-id <new id>` for a session with no conversation yet) and
fans raw bytes out to xterm.js clients over a WebSocket — the browser renders
the actual terminal UI (ttyd-style), so it mirrors Claude Code exactly with
no reimplemented chat to drift. The PTY outlives page loads (tmux-like):
re-attaching replays capped scrollback, and the TUI redraws on resize.

Queue protocol (per attached client): bytes = terminal output to forward
verbatim; dict = control message ({"type": "exit", ...}) after which the
stream is over.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import os
import pty
import re
import shlex
import shutil
import signal
import struct
import sys
import termios
import threading

from .prompt import PHONE_APPEND

SCROLLBACK_MAX = 512 * 1024
# output queued for one client and not yet sent: past this its link cannot
# keep up, so it is dropped (close 4413) and reopens from the scrollback
CLIENT_BACKLOG_MAX = SCROLLBACK_MAX
READ_CHUNK = 65536
KILL_GRACE_SECS = 3.0
DEFAULT_COLS, DEFAULT_ROWS = 100, 30


class TerminalError(RuntimeError):
    pass


class TerminalSession:
    def __init__(self, sid: str, argv: list[str], cwd: str,
                 on_exit=lambda: None):
        self.sid = sid
        self.on_exit = on_exit
        self.argv = argv
        self.cwd = cwd
        self.loop = asyncio.get_running_loop()
        self.pid = -1
        self.fd = -1
        self.alive = False
        self.exit_reason = ""
        self.scrollback = bytearray()
        self.clients: set[asyncio.Queue] = set()
        self.backlog: dict[asyncio.Queue, int] = {}   # queued bytes per client
        self.exited = asyncio.Event()   # set once the process is gone
        # set by the reaper thread the moment waitpid() collects the child:
        # from then on the pid may belong to someone else, so no more kill()
        self._reaped = False
        self._fd_lock = threading.Lock()
        # the device that typed last is exempt from quiet hours (quiet.py)
        self.writer_exempt = False

    def spawn(self) -> None:
        # Env is fully prepared before fork: the child must only make
        # async-signal-safe calls (chdir, exec) between fork and exec.
        env = {**os.environ, "TERM": "xterm-256color", "COLORTERM": "truecolor"}
        argv = self.argv
        cwd = self.cwd
        pid, fd = pty.fork()
        if pid == 0:  # child — never returns
            try:
                os.chdir(cwd)
                os.execvpe(argv[0], argv, env)
            except OSError:
                pass
            os._exit(127)
        self.pid, self.fd = pid, fd
        self.alive = True
        self._set_winsize(DEFAULT_COLS, DEFAULT_ROWS)
        reaper = threading.Thread(target=self._reap, daemon=True,
                                  name=f"term-reap-{self.sid}")
        reaper.start()
        threading.Thread(target=self._read_loop, args=(reaper,), daemon=True,
                         name=f"term-{self.sid}").start()

    # ---------- output pump (reader thread → event loop) ----------

    def _reap(self) -> None:
        """Wait for the child itself. EIO on the PTY only means every end
        of the terminal is closed: the TUI may close it and exit a moment
        later, so a single non-blocking waitpid() there left zombies."""
        with contextlib.suppress(ChildProcessError):
            os.waitpid(self.pid, 0)
        self._reaped = True

    def _read_loop(self, reaper: threading.Thread) -> None:
        while True:
            try:
                data = os.read(self.fd, READ_CHUNK)
            except OSError:  # EIO — every end of the terminal closed
                break
            if not data:
                break
            self.loop.call_soon_threadsafe(self._on_data, data)
        # alive until the process is gone too: close() can still kill a TUI
        # that closed its terminal but lingers
        reaper.join()
        self.loop.call_soon_threadsafe(self._on_exit)

    def _on_data(self, data: bytes) -> None:
        self.scrollback.extend(data)
        if len(self.scrollback) > SCROLLBACK_MAX:
            del self.scrollback[:len(self.scrollback) - SCROLLBACK_MAX]
        for q in list(self.clients):
            n = self.backlog.get(q, 0) + len(data)
            if n > CLIENT_BACKLOG_MAX:
                self._drop_slow(q)
            else:
                self.backlog[q] = n
                q.put_nowait(data)

    def _drop_slow(self, q: asyncio.Queue) -> None:
        """The backlog goes; the client is told to reopen."""
        self.detach(q)
        while not q.empty():
            q.get_nowait()
        q.put_nowait({"type": "too_slow"})

    def _on_exit(self) -> None:
        if not self.alive:
            return
        self.alive = False
        with self._fd_lock:
            with contextlib.suppress(OSError):
                os.close(self.fd)
            self.fd = -1
        self.exited.set()
        msg = {"type": "exit", "reason": self.exit_reason or "Terminal ended"}
        for q in self.clients:
            q.put_nowait(msg)
        with contextlib.suppress(Exception):
            self.on_exit()

    # ---------- client side ----------

    def attach(self, q: asyncio.Queue) -> bytes:
        """Register a client queue; returns scrollback to replay first."""
        self.clients.add(q)
        self.backlog[q] = 0
        return bytes(self.scrollback)

    def detach(self, q: asyncio.Queue) -> None:
        self.clients.discard(q)
        self.backlog.pop(q, None)

    def sent(self, q: asyncio.Queue, n: int) -> None:
        """n bytes of this client's queue have gone out."""
        if q in self.backlog:
            self.backlog[q] = max(0, self.backlog[q] - n)

    def write(self, data: bytes) -> None:
        if self.alive:
            with contextlib.suppress(OSError):
                os.write(self.fd, data)

    def resize(self, cols: int, rows: int) -> None:
        if self.alive and 0 < cols <= 1000 and 0 < rows <= 1000:
            self._set_winsize(cols, rows)

    def _set_winsize(self, cols: int, rows: int) -> None:
        with contextlib.suppress(OSError):
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ,
                        struct.pack("HHHH", rows, cols, 0, 0))

    def close(self, reason: str = "") -> None:
        """SIGHUP the TUI (clean exit, like closing a terminal window);
        escalate to SIGKILL if it lingers. The reader thread observes the
        death and finishes cleanup in _on_exit."""
        if not self.alive:
            return
        self.exit_reason = reason
        pid = self.pid
        if not self._reaped:
            with contextlib.suppress(OSError, ProcessLookupError):
                os.kill(pid, signal.SIGHUP)

        def _force():
            if self.alive and self.pid == pid and not self._reaped:
                with contextlib.suppress(OSError, ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)
        self.loop.call_later(KILL_GRACE_SECS, _force)


class TerminalManager:
    def __init__(self) -> None:
        self.terms: dict[str, TerminalSession] = {}
        # closed but still exiting (SIGHUP, SIGKILL after KILL_GRACE_SECS):
        # its `claude` may still write the transcript, so nothing may resume
        # the session until it is gone (wait_closed)
        self._closing: dict[str, TerminalSession] = {}
        # set by main.py: a terminal that ends (killed, stopped, /exit or a
        # crash) frees its runner slot, so the session list must hear of it
        self.on_exit = lambda sid: None

    def get(self, sid: str) -> TerminalSession | None:
        term = self.terms.get(sid)
        if term is not None and not term.alive:
            self.terms.pop(sid, None)
            return None
        return term

    def open(self, sid: str, argv: list[str], cwd: str) -> TerminalSession:
        term = self.get(sid)
        if term is not None:
            return term
        term = TerminalSession(sid, argv, cwd, on_exit=lambda: self._exited(sid, term))
        term.spawn()
        self.terms[sid] = term
        return term

    def running(self, sid: str) -> bool:
        """A `claude` process of this session's terminal is alive: open, or
        closed and not yet exited. It holds a runner slot either way."""
        return self.get(sid) is not None or sid in self._closing

    def close(self, sid: str, reason: str = "") -> None:
        term = self.terms.pop(sid, None)
        if term is not None:
            if term.alive:
                self._closing[sid] = term
            term.close(reason)

    async def wait_closed(self, sid: str) -> None:
        """Until a closed terminal's process is gone — before anything
        resumes the session (a chat message, a new terminal)."""
        term = self._closing.get(sid)
        if term is not None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(term.exited.wait(), KILL_GRACE_SECS + 1)

    def _exited(self, sid: str, term: TerminalSession) -> None:
        if self._closing.get(sid) is term:
            del self._closing[sid]
        if self.terms.get(sid) is term:
            del self.terms[sid]
        if sid not in self.terms:   # not when a newer terminal is running
            self.on_exit(sid)

    def close_all(self) -> None:
        for sid in list(self.terms):
            self.close(sid, "Server shutting down")


def quiet_settings(sid: str) -> str:
    """--settings JSON adding the quiet-hours hook (quiet_hook.py) to the
    user's own hooks. -I: the hook runs in the project's folder, and a
    project with its own `server` package must not shadow ours."""
    cmd = shlex.join([sys.executable, "-I", "-m", "clicker4ai.quiet_hook", sid])
    hook = [{"hooks": [{"type": "command", "command": cmd}]}]
    return json.dumps({"hooks": {"UserPromptSubmit": hook,
                                 "UserPromptExpansion": hook}})


def claude_argv(resume_id: str, model: str, mode: str, sid: str,
                new: bool = False) -> list[str]:
    """Command line for a TUI that continues the given claude session with
    the webapp session's model/mode settings; `new`: start a session under
    that id instead (nothing to resume yet)."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", resume_id or ""):
        raise TerminalError("invalid resume session id")
    exe = shutil.which("claude")
    if not exe:
        raise TerminalError("claude CLI not found on PATH")
    argv = [exe, "--session-id" if new else "--resume", resume_id, "--append-system-prompt", PHONE_APPEND,
            "--settings", quiet_settings(sid)]
    if model and model != "default":
        argv += ["--model", model]
    if mode and mode != "default":
        argv += ["--permission-mode", mode]
    return argv
