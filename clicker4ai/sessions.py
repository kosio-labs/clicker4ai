"""Session engine: each UI session owns a ClaudeSDKClient (one `claude`
subprocess) plus a persisted, seq-numbered event log that web clients can
replay after reconnecting.

Incognito chats are ordinary runners with three differences: a fresh folder
of their own (config.INCOGNITO_DIR/<device>/<sid>) that only the owning
device's scope reaches, no tools but WebSearch/WebFetch, and deletion that
takes everything Claude Code kept for that folder with it (`claude project
purge`) — which is also what happens after INCOGNITO_IDLE_HOURS without
activity or once the device is gone (SessionManager.incognito_sweep).
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import logging
import os
import re
import shutil
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    ClaudeAgentOptions,
    AssistantMessage,
    ClaudeSDKClient,
    HookMatcher,
    PermissionResultAllow,
    PermissionResultDeny,
    PermissionUpdate,
    ResultMessage,
    TextBlock,
    ToolPermissionContext,
    rename_session,
)

from .config import (
    CLAUDE_DIR,
    DEFAULT_MAX_RUNNERS,
    INCOGNITO_DIR,
    INCOGNITO_IDLE_HOURS,
    LOGS_DIR,
    SESSIONS_DIR,
    open_private_append,
    secure_dir,
    write_private,
)
from .prompt import PHONE_APPEND
from .events import clip, clip_input, normalize_message, tool_summary
from .history import chain_prompts, history_events
from .transcript import last_activity_ms, short_path, transcript_stats
from .watch import rows_to_events, transcript_path

PERMISSION_MODES = ["default", "acceptEdits", "plan", "dontAsk", "bypassPermissions"]
# Modes a remote device may pick. bypassPermissions / dontAsk run tools
# without asking, so a stolen phone would be a remote shell; they stay
# available only in a local terminal on the server.
REMOTE_MODES = ["default", "acceptEdits", "plan"]
MODEL_CHOICES = ["default", "opus", "sonnet", "haiku"]
# Model ids end up in `claude --model <id>` (True View), so only plain ids /
# aliases: "opus", "claude-opus-5", "claude-opus-5[1m]", "us.anthropic.x:0".
MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:\[\]-]{0,79}")


def valid_model(model: str) -> bool:
    return isinstance(model, str) and MODEL_RE.fullmatch(model) is not None

TAIL_POLL_SECS = 1.0
# Runner states during which our own subprocess writes the transcript —
# the tailer must not read then, or it would re-emit our own turns.
BUSY_STATES = ("starting", "working", "compacting", "awaiting")
# A terminal turn ends with a system "turn_duration" row in the transcript
# (the TUI writes it, the SDK does not). Without one (an interrupted turn,
# an older CLI) the turn counts as over once no new rows have landed for
# this long.
EXT_TURN_QUIET_SECS = 30.0
# One stream-json line from the CLI may not exceed this. The SDK's 1 MB
# default is too small for a Read of a large screenshot (base64 in the
# tool result): the reader died and left the process running unheard.
MAX_BUFFER_BYTES = 32 * 1024 * 1024
# How many of the latest prompts the app may rewind to ("edit from here");
# the terminal's /rewind reaches further back
REWIND_MAX = 5
# /btw: side questions kept per session (in memory only) and replayed with
# the next one, like the terminal's; how long one may take
BTW_KEEP = 20
BTW_TIMEOUT_SECS = 180
BTW_PROMPT = """\
<side-question>
This is a side question, asked while the conversation goes on elsewhere. \
Answer it only from what is already in this conversation; no tool will run. \
Neither this question nor your answer is added to the conversation.
{status}{earlier}Question: {question}
</side-question>"""
# The fork closes an unfinished turn with an interrupted tool call and "No
# response requested"; without this the answer reports the running turn as
# stopped (measured on Haiku, even when told a tool may still be running)
BTW_BUSY = """\
The main session is still working right now ({detail}). An interrupted \
tool call or "No response requested" just before this question comes from \
copying the conversation for this side question, not from the user: that \
work is still going on.
"""
# Tools whose file changes a rewind leaves in place (it rewinds only the
# conversation), named in the confirmation so nobody expects them undone
EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")


def _ext_turn_ended(rows: bytes) -> bool:
    """Whether the last turn in these transcript rows has its end marker,
    with nothing of a new turn written after it."""
    for line in reversed(rows.splitlines()):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("type") == "system" and row.get("subtype") == "turn_duration":
            return True
        if row.get("type") in ("user", "assistant"):
            return False
    return False
# A new session nobody wrote to gives its process back after this long.
EMPTY_IDLE_SECS = 15 * 60
# A sent message with no answer from `claude` this long is logged and shown
# in the chat: the process has stopped reading (seen after a
# resume) and the app would otherwise say "Sending…" forever.
STALL_SECS = 60
# Events that do not show `claude` working on the message: the init message
# and usage readings arrive even when it is stuck.
_NOT_A_REACTION = ("meta", "usage", "commands")

log = logging.getLogger("clicker4ai.session")
# The only tools an incognito chat has: a discussion that may look things up,
# nothing that reads or writes the host.
INCOGNITO_TOOLS = ["WebSearch", "WebFetch"]
INCOGNITO_LABEL = f"incognito · erased after {INCOGNITO_IDLE_HOURS} h idle"


_PATH_RULE_TOOLS = ("Read", "Edit", "Write")


def suggestion_paths(s: PermissionUpdate, cwd: str | None = None) -> list[str]:
    """Absolute paths a permission suggestion opens up: the folders of an
    addDirectories, and the absolute ("//path/**") or home ("~/path")
    rules of the file tools — what the CLI offers when a tool reaches past
    the session's folders (CLI 2.1.280: Read → Read(//dir/**), write →
    addDirectories [dir] + setMode acceptEdits, all for the session).
    Relative rules ("src/**", "./x", "/x", "../x") are taken from `cwd`, the
    session folder, since "../" can reach past it; without `cwd` they are
    not listed."""
    if s.type == "addDirectories":
        return list(s.directories or [])
    if s.type not in ("addRules", "replaceRules"):
        return []
    out = []
    for rule in s.rules or []:
        content = getattr(rule, "rule_content", None) or ""
        if getattr(rule, "tool_name", "") not in _PATH_RULE_TOOLS:
            continue
        path = re.sub(r"\\(.)", r"\1", content)   # the CLI escapes glob chars
        path = re.sub(r"/\*\*$", "", path)
        if path.startswith("//"):
            out.append("/" + path.lstrip("/"))
        elif path.startswith("~/") or path == "~":
            out.append(path)
        elif path and cwd:
            out.append(os.path.normpath(os.path.join(cwd, path.lstrip("/"))))
    return out


class _PermissionUpdate(PermissionUpdate):
    """PermissionUpdate that leaves out an empty ruleContent. The SDK sends
    a rule without content ("WebSearch", a whole tool) as "ruleContent":
    null, which the CLI's validator does not accept: it drops the whole
    update without a word, so "always allow" for such a rule never took
    effect and every call asked again (tested against CLI 2.1.278: null →
    asked twice for two searches, field left out → once)."""

    @classmethod
    def of(cls, u: PermissionUpdate) -> "_PermissionUpdate":
        return cls(**{f.name: getattr(u, f.name) for f in dataclasses.fields(u)})

    def to_dict(self) -> dict[str, Any]:
        d = super().to_dict()
        for rule in d.get("rules") or []:
            if rule.get("ruleContent") is None:
                rule.pop("ruleContent", None)
        return d


def now_ms() -> int:
    return int(time.time() * 1000)


def _msg_snippet(ev: dict) -> dict | None:
    """Card-preview snippet for a message event (user_text / assistant text)."""
    kind = ev.get("kind")
    if kind not in ("user_text", "text"):
        return None
    text = " ".join(str(ev.get("text") or "").split())
    if not text:
        return None
    return {"role": "user" if kind == "user_text" else "assistant",
            "text": text[:280]}


class SessionRunner:
    def __init__(self, manager: "SessionManager", sid: str, cwd: str,
                 title: str = "", model: str = "default",
                 permission_mode: str = "default"):
        self.manager = manager
        self.sid = sid
        self.cwd = cwd
        self.title = title
        # a name set in the app that the transcript does not carry yet (no
        # transcript before the first message); written on the next turn
        self.title_sync = False
        # "Resume as fork" before the process first ran: the first start
        # must fork, not continue the original (cleared once the new
        # claude session id shows up)
        self.fork_pending = False
        # "Edit from here": the next start resumes the conversation only up
        # to {"at": <transcript uuid>}, or starts afresh with {"at": None}
        # (the first prompt was rewound). Kept until the new branch has a
        # turn — before that a plain resume would bring the dropped turns back
        self.rewind_at: dict | None = None
        # incognito chat of device `owner` (see the module docstring)
        self.incognito = False
        self.owner = ""
        self._tx: tuple[str, Path] | None = None   # (claude id, transcript)
        self.model = model or "default"
        self.mode = permission_mode or "default"
        self.created_at = now_ms()
        self.last_active = now_ms()

        self.state = "detached"       # starting|idle|working|awaiting|compacting|detached|error
        self.detail = ""
        self.claude_ids: list[str] = []
        # an id given to a True View opened before the chat's first message
        # (claude --session-id); until something is typed there it has no
        # transcript, so nothing may resume it (unborn())
        self.fresh_id: str | None = None
        self.cost_usd: float | None = None
        self.context: dict | None = None
        self.meta: dict = {}
        self.last_msg: dict | None = None   # {"role", "text"} card preview
        self._last_msg_scanned = True       # False → backfill from events.jsonl

        self.seq = 0
        self.events: list[dict] = []
        self._events_loaded = True    # False when constructed from disk lazily

        self.client: ClaudeSDKClient | None = None
        self._reader: asyncio.Task | None = None
        self._closing = False
        self._starting_lock = asyncio.Lock()
        self._connecting = False      # between make_room and client set
        self._term_opening = False    # True View: between make_room and PTY spawn
        self.pending: dict[str, asyncio.Future] = {}
        self.pending_info: dict[str, dict] = {}
        self._stderr = deque(maxlen=60)
        self._last_reaction = 0.0     # monotonic time of the last SDK event

        # Terminal↔webapp sync: tail the claude transcript for turns
        # appended by other processes (a terminal on the same session).
        self.context_stale = False    # external turns our client hasn't seen
        # bumped by every stop/teardown: a start that was already in flight
        # when the user pressed stop must not install its client afterwards
        self._start_epoch = 0
        # set by delete(): the background start of a new session may not
        # have begun yet, and must not bring a deleted one to life
        self.deleted = False
        self._watcher: asyncio.Task | None = None
        self._tail_ff = True          # fast-forward offset to EOF next tick
        self._ext_turn = False        # a terminal turn is in flight
        self._ext_ts = 0              # last external row seen (ms)
        # /btw side questions: [{id, q, a, state, cost, ts}], memory only
        self.btw: list[dict] = []
        self._btw_task: asyncio.Task | None = None
        self.ensure_watcher()

    # ---------- persistence ----------

    @property
    def dir(self) -> Path:
        return SESSIONS_DIR / self.sid

    def save_meta(self) -> None:
        secure_dir(self.dir)
        write_private(self.dir / "meta.json", json.dumps({
            "sid": self.sid, "cwd": self.cwd, "title": self.title,
            "model": self.model, "mode": self.mode,
            "created_at": self.created_at, "last_active": self.last_active,
            "claude_ids": self.claude_ids, "fresh_id": self.fresh_id,
            "cost_usd": self.cost_usd,
            "seq": self.seq, "last_msg": self.last_msg,
            "title_sync": self.title_sync,
            "fork_pending": self.fork_pending,
            "rewind_at": self.rewind_at,
            "incognito": self.incognito, "owner": self.owner,
        }))

    @classmethod
    def from_disk(cls, manager: "SessionManager", meta: dict) -> "SessionRunner":
        r = cls(manager, meta["sid"], meta.get("cwd", ""),
                meta.get("title", ""), meta.get("model", "default"),
                meta.get("mode", "default"))
        r.created_at = meta.get("created_at", now_ms())
        r.last_active = meta.get("last_active", r.created_at)
        r.claude_ids = meta.get("claude_ids", [])
        r.fresh_id = meta.get("fresh_id")
        r.cost_usd = meta.get("cost_usd")
        r.seq = meta.get("seq", 0)
        r.last_msg = meta.get("last_msg")
        r.title_sync = bool(meta.get("title_sync"))
        r.fork_pending = bool(meta.get("fork_pending"))
        r.rewind_at = meta.get("rewind_at") or None
        r.incognito = bool(meta.get("incognito"))
        r.owner = meta.get("owner") or ""
        r._last_msg_scanned = "last_msg" in meta  # older metas: scan events once
        r._events_loaded = False
        if r.mode not in REMOTE_MODES:
            blocked = r.mode
            r.mode = "default"
            r.notice(f"Permission mode {blocked} is not available in remote "
                     "control — switched to default.", "error")
            r.save_meta()   # after notice: meta carries the new seq
        return r

    def ensure_events_loaded(self) -> None:
        if self._events_loaded:
            return
        self._events_loaded = True
        path = self.dir / "events.jsonl"
        if not path.exists():
            return
        events = []
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        with contextlib.suppress(json.JSONDecodeError):
                            events.append(json.loads(line))
        except OSError:
            pass
        self.events = events
        if events:
            self.seq = max(self.seq, events[-1].get("seq", 0))

    def _scan_last_msg(self) -> None:
        """Backfill last_msg for sessions whose meta.json predates it:
        read the tail of events.jsonl once and take the newest message."""
        self._last_msg_scanned = True
        path = self.dir / "events.jsonl"
        try:
            size = path.stat().st_size
            with open(path, "rb") as f:
                f.seek(max(0, size - 65536))
                tail = f.read().decode("utf-8", errors="replace")
        except OSError:
            return
        for line in reversed(tail.splitlines()):
            with contextlib.suppress(json.JSONDecodeError):
                snip = _msg_snippet(json.loads(line))
                if snip:
                    self.last_msg = snip
                    return

    # ---------- event pipeline ----------

    def emit(self, ev: dict) -> None:
        transient = ev.pop("transient", False)
        if transient:
            self.manager.broadcast_event(self.sid, {**ev, "seq": None})
            return
        self.ensure_events_loaded()
        self.seq += 1
        ev = {"seq": self.seq, "ts": now_ms(), **ev}
        self.events.append(ev)
        self.last_msg = _msg_snippet(ev) or self.last_msg
        try:
            if not self.dir.is_dir():
                secure_dir(self.dir)
            with open_private_append(self.dir / "events.jsonl") as f:
                f.write(json.dumps(ev, default=str) + "\n")
        except OSError:
            pass
        self.manager.broadcast_event(self.sid, ev)

    def set_status(self, state: str, detail: str = "") -> None:
        if state == self.state and detail == self.detail:
            return
        self.state = state
        self.detail = detail
        self.manager.broadcast_event(
            self.sid, {"kind": "status", "state": state, "detail": detail, "seq": None})
        self.manager.schedule_sessions_update()

    def notice(self, text: str, level: str = "info") -> None:
        self.emit({"kind": "notice", "level": level, "text": text})

    # ---------- terminal↔webapp sync ----------

    def ensure_watcher(self) -> None:
        if self._watcher is not None and not self._watcher.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return      # no loop yet (import time, or a test constructing one)
        self._watcher = loop.create_task(self._watch_loop())

    def cancel_watcher(self) -> None:
        if self._watcher is not None:
            self._watcher.cancel()
            self._watcher = None

    async def _watch_loop(self) -> None:
        """Tail this session's claude transcript for rows written by OTHER
        processes (terminal `claude` on the same session id) and mirror
        them into the event log live. Our own subprocess's rows are never
        re-emitted: tailing pauses while a turn runs and the offset
        fast-forwards to EOF when the turn's result lands."""
        cid: str | None = None
        path: Path | None = None
        offset: int | None = None  # None → seek to EOF on next read
        # a True View's fresh id (fresh_id) whose transcript did not exist
        # when first looked up: when it appears, every row in it is the
        # terminal's (the chat never wrote to it), so read from the start
        unborn: bool | None = None
        while True:
            await asyncio.sleep(TAIL_POLL_SECS)
            try:
                if (self._ext_turn
                        and now_ms() - self._ext_ts > EXT_TURN_QUIET_SECS * 1000):
                    self._ext_turn = False
                    if self.client is None and self.state == "working":
                        self.set_status("detached", "Synced from terminal")
                cur = self.claude_ids[-1] if self.claude_ids else None
                if cur is None:
                    continue
                if cur != cid:
                    cid, path, unborn = cur, None, None
                if path is None:
                    path = await asyncio.to_thread(transcript_path, cid, self.cwd)
                    if path is None:
                        if unborn is None:
                            unborn = cid == self.fresh_id
                        continue
                    offset = None
                    if unborn:
                        offset, self._tail_ff = 0, False
                    unborn = False
                if self.state in BUSY_STATES and not self._ext_turn:
                    continue
                if self._tail_ff:
                    self._tail_ff = False
                    offset = None
                try:
                    size = path.stat().st_size
                except OSError:
                    path = None
                    continue
                if offset is None or size < offset:
                    offset = size
                    continue
                if size == offset:
                    continue
                chunk = await asyncio.to_thread(self._read_span, path, offset, size)
                end = chunk.rfind(b"\n")
                if end < 0:
                    continue  # partial line still being written
                offset += end + 1
                if b'"custom-title"' in chunk[:end]:   # /rename in the terminal
                    self.manager.schedule_sessions_update()
                evs = rows_to_events(
                    chunk[:end].decode("utf-8", errors="replace").splitlines())
                ended = (b'"turn_duration"' in chunk[:end]
                         and _ext_turn_ended(chunk[:end]))
                if ended and self._ext_turn:
                    self._ext_ts = 0   # the quiet check above ends it now
                if not evs:
                    continue
                self.context_stale = True
                self.last_active = now_ms()
                for ev in evs:
                    self.emit(ev)
                self._ext_ts = 0 if ended else now_ms()
                if self.client is None and ended:
                    # the whole rest of the turn landed at once: over now
                    self._ext_turn = False
                    self.set_status("detached", "Synced from terminal")
                elif self.client is None:
                    self._ext_turn = True
                    detail = "Running in terminal…"
                    for ev in reversed(evs):
                        if ev.get("kind") == "tool_start":
                            tool, summ = ev.get("tool", ""), ev.get("summary") or ""
                            detail = f"{tool}: {summ}" if summ else f"Running {tool}"
                            break
                    self.set_status("working", detail)
                elif self.state == "idle":
                    self.set_status("idle", "Synced from terminal")
                self.save_meta()
                self.manager.schedule_sessions_update()
            except asyncio.CancelledError:
                raise
            except Exception:
                continue  # never let a tail hiccup kill the mirror

    @staticmethod
    def _read_span(path: Path, start: int, end: int) -> bytes:
        with open(path, "rb") as f:
            f.seek(start)
            return f.read(end - start)

    async def _sync_client(self) -> None:
        """Make the live client's context match the transcript on disk —
        rotate through resume when a terminal appended turns behind us."""
        if self.context_stale and self.client is not None:
            await self._teardown_client()
        self.context_stale = False
        await self.ensure_started()

    # ---------- lifecycle ----------

    def _build_options(self, resume: str | None, fork: bool) -> ClaudeAgentOptions:
        # incognito: the listed tools are all that exist in the session (not
        # merely denied), and no MCP server from the user's settings loads
        extra = ({"tools": INCOGNITO_TOOLS, "mcp_servers": {},
                  "strict_mcp_config": True} if self.incognito else {})
        return ClaudeAgentOptions(
            **extra,
            cwd=self.cwd,
            model=None if self.model in ("", "default") else self.model,
            permission_mode=self.mode,
            include_partial_messages=True,
            include_hook_events=True,
            can_use_tool=self._on_permission,
            stderr=self._on_stderr,
            max_buffer_size=MAX_BUFFER_BYTES,
            resume=resume,
            fork_session=fork,
            resume_session_at=(self.rewind_at or {}).get("at") if resume else None,
            # Keep Claude Code's own prompt; add how to present on a phone.
            system_prompt={"type": "preset", "preset": "claude_code",
                           "append": PHONE_APPEND},
            # Use the system `claude` (same binary True View runs) instead of
            # the CLI bundled in the SDK wheel; None falls back to the SDK's
            # own lookup.
            cli_path=shutil.which("claude"),
            # The SDK labels the process "sdk-py", and the terminal's /resume
            # picker hides every session whose transcript starts with an SDK
            # label (and turns a headless "cli" into "sdk-cli") — so a session
            # begun here could not be found there. A label the CLI does not
            # know is neither hidden nor changes its behaviour.
            env={"CLAUDE_CODE_ENTRYPOINT": "clicker4ai"},
            extra_args=self._debug_args(),
            # setting_sources=None → load user/project/local settings like the
            # terminal CLI, so CLAUDE.md, MCP servers, hooks and plugins match.
        )

    def _on_stderr(self, line: str) -> None:
        self._stderr.append(line)
        log.debug("%s stderr: %s", self.sid, line.rstrip())

    def _debug_args(self) -> dict[str, str | None]:
        """At log_level "debug" the `claude` process keeps its own debug log
        in DATA_DIR/logs/cli-<sid>.log — the only record of what it was doing
        when it stopped answering."""
        if not log.isEnabledFor(logging.DEBUG):
            return {}
        path = LOGS_DIR / f"cli-{self.sid}.log"
        try:
            secure_dir(LOGS_DIR)
            # created 0600 here: the CLI would leave it group-readable, and
            # it holds prompts and tool output
            with open_private_append(path):
                pass
        except OSError:
            return {}
        return {"debug-file": str(path)}

    def _stderr_tail(self, n: int = 8) -> str:
        return "\n".join(list(self._stderr)[-n:])

    async def start(self, resume: str | None = None, fork: bool = False,
                    announce: bool = True) -> None:
        async with self._starting_lock:
            if self.client is not None or self.deleted:
                return
            # read before make_room, which may wait on stopping another
            # session: a stop or delete meanwhile must cancel this start
            epoch = self._start_epoch
            # the single funnel every live process goes through, so the
            # limit is checked here rather than at each caller
            await self.manager.make_room(self)
            if epoch != self._start_epoch or self.deleted:
                return
            # counts as live from here (no await since the check), so two
            # starts racing through connect() cannot both fit under the limit
            self._connecting = True
            try:
                self.ensure_events_loaded()
                self._closing = False
                self._ext_turn = False  # our subprocess owns the transcript now
                self.set_status("starting", "Starting Claude Code…")
                # (not for a chat being deleted: its erase removed the folder)
                if (self.incognito and not Path(self.cwd).is_dir()
                        and self.manager.runners.get(self.sid) is self):
                    # erased under us (a sweep, by hand): the transcript went
                    # with it, so start afresh in a new folder of that name
                    secure_dir(Path(self.cwd).parent)
                    secure_dir(Path(self.cwd))
                    resume, fork = None, False
                    log.warning("%s incognito folder was gone: recreated, "
                                "starting without resume", self.sid)
                log.info("%s start (resume=%s fork=%s cwd=%s)",
                         self.sid, resume, fork, self.cwd)
                t0 = time.monotonic()
                client = ClaudeSDKClient(self._build_options(resume, fork))
                try:
                    await client.connect()
                except Exception as e:
                    log.warning("%s failed to start: %s\n%s",
                                self.sid, e, self._stderr_tail())
                    self.set_status("error", "Failed to start")
                    tail = "\n".join(list(self._stderr)[-8:])
                    self.notice(f"Failed to start session: {e}\n{tail}", "error")
                    raise
                if epoch != self._start_epoch:
                    # stopped while we were connecting — drop it again
                    with contextlib.suppress(Exception):
                        await client.disconnect()
                    return
                self.client = client
                log.info("%s started in %.1f s", self.sid, time.monotonic() - t0)
            finally:
                self._connecting = False
            self._reader = asyncio.create_task(self._read_loop(client))
            self._tail_ff = True  # skip rows our own boot writes (hooks etc.)
            self.set_status("idle", "Ready")
            if resume and announce:
                self.notice("Resumed session" + (" (forked)" if fork else ""))
            self.ensure_watcher()
            asyncio.create_task(self._post_connect())
            if self.is_empty():
                asyncio.create_task(self._stop_if_unused(client))

    def is_empty(self) -> bool:
        """Live but never written to: no conversation to lose, so its
        process may be stopped without asking anyone."""
        return (self.state not in BUSY_STATES and not self.pending
                and not any(ev.get("kind") == "user_text" for ev in self.events))

    async def stop_unused(self, detail: str) -> None:
        await self.stop()
        self.set_status("detached", detail)

    async def _stop_if_unused(self, client: ClaudeSDKClient) -> None:
        await asyncio.sleep(EMPTY_IDLE_SECS)
        if self.client is client and self.is_empty():
            await self.stop_unused("Stopped after 15 min without a message "
                                   "— starts with your first message")

    async def _post_connect(self) -> None:
        client = self.client
        if client is None:
            return
        with contextlib.suppress(Exception):
            info = await client.get_server_info()
            if info:
                cmds = []
                for c in info.get("commands") or []:
                    if isinstance(c, dict):
                        cmds.append({"name": c.get("name", ""),
                                     "description": c.get("description", ""),
                                     "argument_hint": c.get("argumentHint")
                                     or c.get("argument_hint", "")})
                    else:
                        cmds.append({"name": str(c), "description": "",
                                     "argument_hint": ""})
                if cmds:
                    self.meta["commands"] = cmds
                    self.emit({"kind": "commands", "commands": cmds})
        await self.refresh_context()

    async def _read_loop(self, client: ClaudeSDKClient) -> None:
        try:
            async for msg in client.receive_messages():
                if self.client is not client:
                    break
                for ev in normalize_message(msg):
                    self._handle(ev)
        except asyncio.CancelledError:
            return
        except Exception as e:
            if not self._closing and self.client is client:
                tail = self._stderr_tail()
                log.warning("%s process ended: %s\n%s", self.sid, e, tail)
                self.notice(f"Session process ended: {e}"
                            + (f"\n{tail}" if tail.strip() else ""), "error")
        finally:
            if self.client is client:
                self.client = None
                if not self._closing:
                    log.warning("%s process ended while in use (state %s)\n%s",
                                self.sid, self.state, self._stderr_tail())
                    self._fail_pending("Session process ended")
                    # the reader may have died with the process still alive
                    # (a message over the buffer limit): stop it, or it
                    # finishes the turn unheard and its rows come back
                    # through the tailer as a terminal's
                    asyncio.create_task(self._kill_orphan(client))
                    self._tail_ff = True  # rows up to here are our own turn
                    self.set_status("detached", "Process ended — will resume on next message")

    async def _kill_orphan(self, client: ClaudeSDKClient) -> None:
        with contextlib.suppress(Exception):
            await client.disconnect()
        self._tail_ff = True  # skip whatever it wrote before it went

    def _record_usage(self, ev: dict) -> None:
        """Store how much of the plan is gone. The SDK reports it after every
        request, whatever the status, so this is the only place the app can
        learn it — the chat hears only of a refusal (events.py)."""
        status = ev.get("rate_limit")
        changed = False
        for key, w in (ev.get("windows") or {}).items():
            util = w.get("utilization")
            if util is None:
                continue
            prev = self.manager.usage.get(key)
            self.manager.usage[key] = {
                "status": status,
                "utilization": util,
                "resets_at": w.get("resets_at"),
                "ts": now_ms(),
            }
            if prev is None or prev["status"] != status \
                    or round(prev["utilization"], 3) != round(util, 3):
                changed = True
        # one reading per request would otherwise push a session list per
        # request; only a real move is worth telling the apps about
        if changed:
            self.manager.schedule_sessions_update()

    def _handle(self, ev: dict) -> None:
        kind = ev.get("kind")
        if kind not in _NOT_A_REACTION:
            self._last_reaction = time.monotonic()
        if kind not in ("delta", "thinking_delta"):   # one per token
            log.debug("%s event %s", self.sid, kind)
        if kind == "usage":
            self._record_usage(ev)
            return
        if kind == "meta":
            csid = ev.get("claude_session_id")
            if csid and csid not in self.claude_ids:
                self.claude_ids.append(csid)
                self.fork_pending = False
                if self.rewind_at is not None and self.rewind_at.get("at") is None:
                    self.rewind_at = None   # the fresh session exists now
            self.meta.update({k: v for k, v in ev.items() if k != "kind"})
            self.save_meta()
        elif kind == "delta" or kind == "text_open":
            self.set_status("working", "Writing…")
        elif kind == "thinking_delta" or kind == "thinking_open":
            self.set_status("working", "Thinking…")
        elif kind == "status_hint":
            self.set_status("working", ev.get("detail", "Working…"))
            return  # purely transient
        elif kind == "tool_start":
            summary = ev.get("summary") or ""
            tool = ev.get("tool", "")
            self.set_status("working", f"{tool}: {summary}" if summary else f"Running {tool}")
        elif kind == "result":
            self.last_active = now_ms()
            if ev.get("cost_usd") is not None:
                self.cost_usd = ev["cost_usd"]
            csid = ev.get("claude_session_id")
            if csid and csid not in self.claude_ids:
                self.claude_ids.append(csid)
                self.fork_pending = False
            # the new branch has its first turn: a plain resume finds it
            self.rewind_at = None
            if ev.get("is_error"):
                errs = ev.get("errors") or []
                self.set_status("idle", "Turn failed")
                if ev.get("subtype") != "success" or errs:
                    self.notice("Turn ended with error: "
                                + (", ".join(map(str, errs)) or ev.get("subtype", "")),
                                "error")
            else:
                secs = (ev.get("duration_ms") or 0) / 1000
                cost = ev.get("cost_usd")
                log.info("%s turn done in %.1f s", self.sid, secs)
                self.set_status("idle", f"Done in {secs:.0f}s"
                                + (f" · ${cost:.2f} total" if cost else ""))
            self.save_meta()
            self._tail_ff = True  # skip our own turn's transcript rows
            asyncio.create_task(self.refresh_context())
            if self.title_sync:
                asyncio.create_task(self._push_title())
        elif kind == "compact":
            if self.state == "compacting":
                self.set_status("idle", "Compacted")
            self._tail_ff = True
            asyncio.create_task(self.refresh_context())
        self.emit(ev)

    def _fail_pending(self, reason: str) -> None:
        for rid, fut in list(self.pending.items()):
            if not fut.done():
                fut.set_result({"behavior": "deny", "message": reason})
        self.pending.clear()

    # ---------- permissions ----------

    async def _on_permission(self, tool_name: str, tool_input: dict,
                             context: ToolPermissionContext) -> Any:
        rid = uuid.uuid4().hex[:12]
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self.pending[rid] = fut
        client = self.client

        if tool_name == "AskUserQuestion":
            ev = {"kind": "question", "request_id": rid,
                  "questions": tool_input.get("questions") or []}
        elif tool_name == "ExitPlanMode":
            ev = {"kind": "plan_approval", "request_id": rid,
                  "plan": clip(tool_input.get("plan") or "", 20000)}
        else:
            suggestions = []
            for s in (context.suggestions or []):
                with contextlib.suppress(Exception):
                    d = s.to_dict()
                    if self.incognito or not d.get("destination"):
                        d["destination"] = "session"   # as applied below
                    suggestions.append(d)
            ev = {
                "kind": "permission", "request_id": rid,
                "tool": tool_name,
                "input": clip_input(tool_input),
                "summary": tool_summary(tool_name, tool_input),
                "title": context.title or "",
                "display_name": context.display_name or "",
                "description": context.description or "",
                "reason": context.decision_reason or "",
                "suggestions": suggestions,
            }
        self.pending_info[rid] = {"tool": tool_name, "input": tool_input,
                                  "context": context}
        self.emit(ev)
        self.set_status("awaiting", f"Needs your approval: {tool_name}")
        self.manager.schedule_sessions_update()

        try:
            resp: dict = await fut
        finally:
            self.pending.pop(rid, None)
            info = self.pending_info.pop(rid, None)

        # the process may have died with the card open (_read_loop's finally
        # denies it and sets detached before this resumes): leave that state
        if client is not None and self.client is client:
            self.set_status("working", "Continuing…")
        behavior = resp.get("behavior", "deny")
        self.emit({"kind": "permission_resolved", "request_id": rid,
                   "behavior": behavior, "note": resp.get("message", "")})

        if tool_name == "AskUserQuestion" and behavior == "allow":
            return PermissionResultAllow(updated_input={
                "questions": tool_input.get("questions") or [],
                "answers": resp.get("answers") or {},
            })
        if behavior == "allow":
            updated_permissions = None
            picked = resp.get("apply_suggestions") or []
            if picked and info:
                sugg = info["context"].suggestions or []
                chosen = []
                for i in picked:
                    if isinstance(i, int) and 0 <= i < len(sugg):
                        s = sugg[i]
                        if s.type == "setMode":
                            # the CLI switches its mode; keep ours (shown in
                            # the app, saved in meta) in step, and never go
                            # to a mode remote control does not offer
                            if s.mode not in REMOTE_MODES:
                                continue
                            self.mode = s.mode
                            self.save_meta()
                            self.notice(f"Permission mode: {s.mode}")
                            self.manager.schedule_sessions_update()
                        # incognito: "always allow" must not outlive the
                        # chat in the user's or a project's settings
                        if self.incognito or getattr(s, "destination", None) is None:
                            s = dataclasses.replace(s, destination="session")
                        chosen.append(_PermissionUpdate.of(s))
                updated_permissions = chosen or None
            return PermissionResultAllow(
                updated_input=resp.get("updated_input") or None,
                updated_permissions=updated_permissions,
            )
        return PermissionResultDeny(
            message=resp.get("message") or "User denied this action",
            interrupt=bool(resp.get("interrupt", False)),
        )

    def respond_permission(self, request_id: str, resp: dict) -> bool:
        fut = self.pending.get(request_id)
        if fut is None or fut.done():
            return False
        fut.set_result(resp)
        return True

    async def backfill_history(self, claude_id: str) -> None:
        """Replay a past session's transcript into this UI session's log,
        so resuming shows the whole prior conversation like the terminal."""
        self.ensure_events_loaded()
        if any(ev.get("kind") == "resumed" for ev in self.events):
            return
        evs, total = await asyncio.to_thread(history_events, claude_id, self.cwd)
        for ev in evs:
            self.emit(ev)
        self.emit({"kind": "resumed", "claude_session_id": claude_id,
                   "shown": len(evs), "total": total})

    # ---------- user actions ----------

    def unborn(self) -> bool:
        """The last id was given to a True View that nobody typed in: there
        is no transcript behind it to resume or fork."""
        if not self.fresh_id or not self.claude_ids or self.claude_ids[-1] != self.fresh_id:
            return False
        if transcript_path(self.fresh_id, self.cwd) is None:
            return True
        self.fresh_id = None   # the terminal wrote a turn: a session like any
        self.save_meta()
        return False

    def id_for_terminal(self) -> tuple[str, bool]:
        """The claude session id a True View opens, and whether it is new
        (`--session-id`, nothing to resume yet) — a session nobody wrote
        to in the chat gets one here, so the TUI can start before the
        chat's first message."""
        if not self.claude_ids:
            self.fresh_id = str(uuid.uuid4())
            self.claude_ids.append(self.fresh_id)
            self.save_meta()
            return self.fresh_id, True
        return self.claude_ids[-1], self.unborn()

    async def ensure_started(self) -> None:
        if self.client is not None:
            return
        if self.unborn():
            # True View closed with nothing typed: start the chat afresh
            self.claude_ids.pop()
            self.fresh_id = None
            self.save_meta()
        resume = self.claude_ids[-1] if self.claude_ids else None
        if self.rewind_at is not None and self.rewind_at.get("at") is None:
            resume = None   # rewound to before the first prompt: start afresh
        # announce=False: auto-resume (after restart or terminal handoff)
        # should be invisible, like the terminal picking up where it left off.
        await self.start(resume=resume, fork=self.fork_pending, announce=False)

    async def send(self, text: str) -> None:
        text = (text or "").rstrip()
        if not text:
            return
        self.last_active = now_ms()
        if not self.title:
            self.title = text.splitlines()[0][:64]
        self.emit({"kind": "user_text", "text": text})
        self.save_meta()
        await self._sync_client()
        self.set_status("working", "Sending…")
        assert self.client is not None
        sent = time.monotonic()
        log.info("%s send (%d chars)", self.sid, len(text))
        # started before the write: a query() that never returns is a stall too
        asyncio.create_task(self._watch_stall(self.client, sent))
        await self.client.query(text)
        took = time.monotonic() - sent
        (log.warning if took > 5 else log.info)(
            "%s message written to claude in %.2f s", self.sid, took)

    async def _watch_stall(self, client: ClaudeSDKClient, sent: float) -> None:
        await asyncio.sleep(STALL_SECS)
        if self.client is not client or self._last_reaction >= sent \
                or self.state not in BUSY_STATES:
            return
        log.warning("%s no reaction from claude %d s after a message "
                    "(state %s: %s)\n%s", self.sid, STALL_SECS, self.state,
                    self.detail, self._stderr_tail())
        self.notice(f"No reaction from Claude Code for {STALL_SECS} s — its "
                    "process may be stuck. Stop the session and send again "
                    "(the message is kept in the transcript only if Claude "
                    "Code took it).", "warn")

    async def rename(self, title: str) -> None:
        """Set the title by hand. The automatic one is the first line the user
        ever sent, which rarely still describes the session an hour later.
        The name also goes into the transcript, as /rename in the terminal
        does, so `claude --resume` lists it under the same name."""
        self.title = title
        self.title_sync = True
        self.save_meta()
        await self._push_title()
        self.manager.schedule_sessions_update()

    async def _push_title(self) -> None:
        cid = self.claude_ids[-1] if self.claude_ids else None
        if not cid or not self.title_sync:
            return
        try:
            await asyncio.to_thread(rename_session, cid, self.title, self.cwd)
        except (OSError, ValueError):
            return   # no transcript yet: retried after the next turn
        self.title_sync = False
        self.save_meta()

    def transcript_stats(self) -> dict:
        """Context size, model and user-given name from the transcript."""
        cid = self.claude_ids[-1] if self.claude_ids else None
        if cid is None:
            return transcript_stats(None)
        if self._tx is None or self._tx[0] != cid:
            path = transcript_path(cid, self.cwd)
            if path is None:
                return transcript_stats(None)
            self._tx = (cid, path)
        return transcript_stats(self._tx[1])

    def display_title(self, stats: dict | None = None) -> str:
        """The transcript's name (set by /rename in a terminal or by us)
        unless an app rename is still waiting to be written there."""
        if not self.title_sync:
            custom = (stats or self.transcript_stats())["custom_title"]
            if custom:
                return custom
        return self.title or "(new session)"

    async def interrupt(self) -> None:
        self._fail_pending("Interrupted by user")
        if self.client is None:
            self.set_status("detached", "")
            return
        try:
            await self.client.interrupt()
            self.notice("Interrupted")
            self.set_status("idle", "Interrupted")
        except Exception as e:
            self.notice(f"Interrupt failed: {e}", "warn")

    async def clear(self) -> None:
        """/clear — fresh context in the same chat thread."""
        self._fail_pending("Conversation cleared")
        await self._teardown_client()
        self.emit({"kind": "cleared"})
        self.context = None
        self.context_stale = False
        await self.start()
        self.notice("Conversation cleared — fresh context started")

    async def rewind(self, seq: int, has_terminal: bool, dry: bool = False) -> dict:
        """"Edit from here": drop the prompt with event `seq` and everything
        after it from the conversation. Only the conversation — files the
        dropped turns changed stay as they are. The process stops; the next
        message resumes the transcript up to the entry before that prompt
        (resume_session_at), so the dropped turns leave the context. Returns
        the prompt's text (back into the message box) and the files edited
        in the dropped turns; `dry` only reports them, for the confirmation.
        Raises ValueError with the reason to show."""
        self.ensure_events_loaded()
        if self.state in BUSY_STATES or self.pending:
            raise ValueError("Wait for the turn to finish, or stop it first")
        if has_terminal:
            raise ValueError("End True View first — or rewind there (Esc Esc)")
        idx = next((i for i, ev in enumerate(self.events)
                    if ev.get("seq") == seq and ev.get("kind") == "user_text"), None)
        if idx is None:
            raise ValueError("That message is no longer in the conversation")
        prompts = [ev for ev in self.events[idx:] if ev.get("kind") == "user_text"]
        if len(prompts) > REWIND_MAX:
            raise ValueError(f"Only the last {REWIND_MAX} prompts can be edited "
                             "here — True View can go further back (Esc Esc)")
        if not self.claude_ids:
            raise ValueError("No conversation to rewind yet")
        # the prompt in the transcript: same text, same count from the end
        text = self.events[idx].get("text") or ""
        want = clip(text.strip())
        same = sum(1 for ev in prompts if clip((ev.get("text") or "").strip()) == want)
        chain = await asyncio.to_thread(chain_prompts, self.claude_ids[-1], self.cwd)
        if self.rewind_at is not None:
            # rewound before and not sent yet: the transcript still ends in
            # the dropped turns — only what comes before the cut counts
            cut = next((i for i, p in enumerate(chain) if p[2] == self.rewind_at["at"]),
                       0 if self.rewind_at["at"] is None else len(chain))
            chain = chain[:cut]
        hits = [p for p in chain if p[1] == want]
        if len(hits) < same:
            raise ValueError("That prompt is not in the current conversation "
                             "(cleared or compacted since)")
        _, _, before = hits[-same]
        dropped = self.events[idx:]
        files = sorted({(ev.get("input") or {}).get("file_path")
                        or (ev.get("input") or {}).get("notebook_path") or ""
                        for ev in dropped
                        if ev.get("kind") == "tool_start" and ev.get("tool") in EDIT_TOOLS} - {""})
        if dry:
            return {"text": text, "prompts": len(prompts), "files": files}

        self._fail_pending("Conversation rewound")
        await self._teardown_client()
        self.rewind_at = {"at": before}
        self.events = self.events[:idx]
        try:
            write_private(self.dir / "events.jsonl",
                          "".join(json.dumps(ev, default=str) + "\n" for ev in self.events))
        except OSError:
            pass
        self.last_msg = next((snip for ev in reversed(self.events)
                              if (snip := _msg_snippet(ev))), None)
        self.context = None
        self.context_stale = False
        # clients drop what they hold from this seq on (also a client that
        # reconnects later: it gets this event with the rest)
        self.emit({"kind": "rewound", "from_seq": seq, "prompts": len(prompts)})
        self.set_status("detached", "Rewound — send the edited prompt")
        self.save_meta()
        self.manager.schedule_sessions_update()
        log.info("%s rewound %d prompt(s) to %s", self.sid, len(prompts), before)
        return {"text": text, "prompts": len(prompts), "files": files}

    async def compact(self, instructions: str = "") -> None:
        await self._sync_client()
        self.set_status("compacting", "Compacting conversation…")
        self.notice("Compacting conversation…")
        assert self.client is not None
        await self.client.query("/compact" + (f" {instructions}" if instructions else ""))

    # ---------- side questions (/btw) ----------

    def _push_btw(self) -> None:
        self.manager.broadcast_event(
            self.sid, {"kind": "btw", "items": self.btw, "seq": None})

    def ask_btw(self, question: str) -> None:
        """Answer a question from the conversation so far without adding to
        it: a one-off `claude` forked from the transcript, not persisted,
        with every tool refused. Its own process, so the session's turn goes
        on; not counted against max_runners (it lives for seconds)."""
        question = (question or "").strip()
        if not question:
            raise ValueError("Ask a question after /btw")
        if not self.claude_ids or self.unborn() or (
                self.rewind_at is not None and self.rewind_at.get("at") is None):
            raise ValueError("Nothing to ask about yet: the conversation is empty")
        if self._btw_task is not None and not self._btw_task.done():
            raise ValueError("A side question is already running")
        earlier = [x for x in self.btw if x["state"] == "done"]
        item = {"id": uuid.uuid4().hex[:8], "q": question, "a": "",
                "state": "asking", "cost": None, "ts": now_ms()}
        self.btw = (self.btw + [item])[-BTW_KEEP:]
        self._push_btw()
        self._btw_task = asyncio.create_task(self._run_btw(item, earlier))

    def clear_btw(self) -> None:
        self.cancel_btw()
        self.btw = []
        self._push_btw()

    def cancel_btw(self) -> None:
        if self._btw_task is not None:
            self._btw_task.cancel()

    async def _run_btw(self, item: dict, earlier: list[dict]) -> None:
        async def refuse_tool(tool_name, tool_input, context):
            return PermissionResultDeny(message="Side question: no tools")

        tried: list[str] = []

        async def block_tool(inp, tool_use_id, context):
            # a hook, unlike can_use_tool, also stops the tools the user's
            # allow rules or the permission mode would let through unasked
            tried.append(inp.get("tool_name") or "a tool")
            log.info("%s side question tried %s: blocked", self.sid,
                     inp.get("tool_name"))
            return {"hookSpecificOutput": {
                "hookEventName": "PreToolUse", "permissionDecision": "deny",
                "permissionDecisionReason": "Side question: no tools"}}

        opts = self._build_options(self.claude_ids[-1], fork=True)
        # Same model, prompt, tools and MCP servers as the session, so the
        # request reuses its prompt cache; the user's hooks stay off (a
        # memory plugin would keep the question, a Stop hook delays the
        # answer). Measured on Haiku, 31k context: 169 tokens written.
        opts = dataclasses.replace(
            opts, permission_mode="default", can_use_tool=refuse_tool,
            include_partial_messages=False, include_hook_events=False,
            hooks={"PreToolUse": [HookMatcher(hooks=[block_tool])]},
            settings=json.dumps({"disableAllHooks": True}),
            stderr=lambda line: None, max_turns=1,
            extra_args={**opts.extra_args, "no-session-persistence": None})
        prior = "".join(f"Earlier side question: {x['q']}\nYour answer: {x['a']}\n\n"
                        for x in earlier[-BTW_KEEP:])
        busy = self.state in BUSY_STATES or self._ext_turn
        status = BTW_BUSY.format(detail=self.detail or self.state) if busy else ""
        prompt = BTW_PROMPT.format(status=status, earlier=prior, question=item["q"])
        texts: list[str] = []
        try:
            async with asyncio.timeout(BTW_TIMEOUT_SECS):
                async with ClaudeSDKClient(opts) as client:
                    await client.query(prompt)
                    async for msg in client.receive_response():
                        if isinstance(msg, AssistantMessage):
                            texts += [b.text for b in msg.content
                                      if isinstance(b, TextBlock)]
                        elif isinstance(msg, ResultMessage):
                            item["cost"] = msg.total_cost_usd
            item["a"] = "\n\n".join(t.strip() for t in texts if t.strip())
            item["state"] = "done"
            if not item["a"]:
                item["state"] = "error"
                item["a"] = (f"No answer: it tried to use {tried[0]}, which a "
                             "side question cannot" if tried else "No answer")
        except asyncio.CancelledError:
            item["state"], item["a"] = "error", "Cancelled"
            raise
        except TimeoutError:
            item["state"], item["a"] = "error", "No answer in time"
        except Exception as e:
            log.warning("%s side question failed: %s", self.sid, e)
            item["state"], item["a"] = "error", f"Failed: {e}"
        finally:
            self._push_btw()

    async def set_model(self, model: str) -> None:
        model = model or "default"
        if not valid_model(model):
            raise ValueError(f"invalid model {model}")
        self.model = model
        if self.client is not None:
            await self.client.set_model(None if self.model == "default" else self.model)
        self.save_meta()
        self.notice(f"Model set to {self.model}"
                    + ("" if self.client else " (applies when session starts)"))
        self.manager.schedule_sessions_update()

    async def set_mode(self, mode: str) -> None:
        if mode not in REMOTE_MODES:
            raise ValueError(f"permission mode {mode} is not available in remote control")
        self.mode = mode
        if self.client is not None:
            await self.client.set_permission_mode(mode)
        self.save_meta()
        self.notice(f"Permission mode: {mode}")
        self.manager.schedule_sessions_update()

    async def refresh_context(self) -> None:
        client = self.client
        if client is None:
            return
        with contextlib.suppress(Exception):
            usage = await client.get_context_usage()
            self.context = {
                "total_tokens": usage.get("totalTokens"),
                "max_tokens": usage.get("maxTokens"),
                "percentage": round(usage.get("percentage") or 0, 1),
                "model": usage.get("model"),
                "categories": [
                    {"name": c.get("name"), "tokens": c.get("tokens")}
                    for c in (usage.get("categories") or [])
                ],
            }
            self.manager.broadcast_event(
                self.sid, {"kind": "context", **self.context, "seq": None})
            self.manager.schedule_sessions_update()

    def resume_tokens(self) -> int | None:
        """Roughly what stopping this session would cost to undo: the
        context that would have to be rebuilt on the next message. The
        live figure when we have one, else the transcript's last turn,
        else four bytes per token of the event log — an upper bound, and
        enough to compare sessions."""
        live = (self.context or {}).get("total_tokens")
        if isinstance(live, int) and live > 0:
            return live
        stored = self.transcript_stats()["context_tokens"]
        if stored:
            return stored
        try:
            return (self.dir / "events.jsonl").stat().st_size // 4
        except OSError:
            return None

    def stop_candidate(self) -> dict:
        """One row of the "stop something to make room" list. The model
        rides along because tokens alone mislead — the same context costs
        five times more to rebuild on Opus than on Haiku."""
        return {
            "sid": self.sid,
            "title": self.display_title(),
            "cwd": self.cwd,
            "last_active": self.last_active,
            "resume_tokens": self.resume_tokens(),
            # what the session is really running (from the SDK's init
            # message) rather than the "default" stored at creation
            "model": (self.meta or {}).get("model") or self.model,
            # its process is a True View terminal, whose turn we cannot see
            "terminal": self.manager.terminal_open(self.sid),
        }

    async def _teardown_client(self) -> None:
        if self.client is not None:
            log.info("%s stop (state %s)", self.sid, self.state)
        self._start_epoch += 1   # invalidate a start still in flight
        self._connecting = False   # ...which then no longer holds a slot
        client, self.client = self.client, None
        self._closing = True
        if self._reader:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader
            self._reader = None
        if client is not None:
            with contextlib.suppress(Exception):
                await client.disconnect()
        self._closing = False

    async def detach_for_terminal(self) -> None:
        """Hand the claude session over to a True View terminal: stop our
        subprocess so the TUI owns the transcript. The watcher keeps
        mirroring its turns into the chat log, and the next chat message
        resumes our client via the context_stale rotation."""
        self._fail_pending("Handed off to terminal view")
        await self._teardown_client()
        self._tail_ff = True  # mirror only rows the terminal writes from now
        self.set_status("detached", "In terminal view")
        self.save_meta()

    async def stop(self) -> None:
        """Stop the subprocess — and the session's True View terminal, which
        holds a runner slot too; session stays resumable."""
        self._fail_pending("Session stopped")
        self.manager.close_terminal(self.sid, "Session stopped")
        await self._teardown_client()
        self.set_status("detached", "Stopped — resumes on next message")
        self.notice("Session process stopped")
        self.save_meta()

    def snapshot(self) -> dict:
        if self.last_msg is None and not self._last_msg_scanned:
            self._scan_last_msg()
        proj = Path(self.cwd)
        stats = self.transcript_stats()
        ctx = self.context or {}
        live_tokens, pct = ctx.get("total_tokens"), ctx.get("percentage")
        if self.context_stale and stats["context_tokens"]:
            # turns from a terminal: the SDK figure is behind, the
            # transcript's last turn is not (against the window we last knew)
            live_tokens = stats["context_tokens"]
            mx = ctx.get("max_tokens")
            pct = round(100 * live_tokens / mx, 1) if mx else None
        model = self.meta.get("model") or self.model
        return {
            "sid": self.sid,
            "title": self.display_title(stats),
            "cwd": self.cwd,
            "cwd_short": INCOGNITO_LABEL if self.incognito else short_path(self.cwd),
            # deleted or moved folder: the runner can no longer start there
            # (an incognito folder comes back with the next start)
            "cwd_missing": not self.incognito and not proj.is_dir(),
            "project": "incognito" if self.incognito else (proj.name or self.cwd),
            "incognito": self.incognito,
            "state": self.state,
            "detail": self.detail,
            "model": stats["model"] if model == "default" and stats["model"] else model,
            "context_tokens": live_tokens or stats["context_tokens"],
            "mode": self.mode,
            "cost_usd": self.cost_usd,
            "context_pct": pct,
            # none while only an untouched True View holds one: the app
            # offers fork and /btw by it
            "claude_session_id": (self.claude_ids[-1]
                                  if self.claude_ids and not self.unborn() else None),
            "created_at": self.created_at,
            "last_active": self.last_active,
            # a True View terminal is a live process too (and holds a slot)
            "live": self.client is not None or self.manager.terminal_open(self.sid),
            "terminal": self.manager.terminal_open(self.sid),
            "pending_permissions": len(self.pending),
            "rewind_pending": self.rewind_at is not None,
            "last_msg": self.last_msg,
            "usage": self.manager.usage or None,
        }


async def erase_incognito_dir(cwd: str) -> None:
    """Everything Claude Code kept for an incognito folder, then the folder.
    `claude project purge` covers the transcripts, file history, the
    ~/.claude.json entry and the prompts in history.jsonl. It works on a
    whole project, which is why every incognito chat has a folder of its
    own — and why anything that is not exactly INCOGNITO_DIR/<device>/<sid>
    is refused here."""
    path = Path(cwd).resolve()
    if not path.is_relative_to(INCOGNITO_DIR) \
            or len(path.relative_to(INCOGNITO_DIR).parts) != 2:
        return
    claude = shutil.which("claude")
    if claude:
        with contextlib.suppress(Exception):
            proc = await asyncio.create_subprocess_exec(
                claude, "project", "purge", "-y", str(path),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL)
            try:
                await asyncio.wait_for(proc.wait(), 60)
            except asyncio.TimeoutError:
                proc.kill()
    # no `claude` or the purge failed: at least the transcripts go
    shutil.rmtree(CLAUDE_DIR / "projects" / re.sub(r"[^a-zA-Z0-9]", "-", str(path)),
                  ignore_errors=True)
    shutil.rmtree(path, ignore_errors=True)


class RunnerLimit(Exception):
    """Too many sessions already hold a live `claude` process. Carries the
    sessions the caller could stop to make room, so the app can ask
    instead of just refusing."""

    def __init__(self, limit: int, candidates: list[dict]):
        super().__init__(f"at most {limit} sessions can run at once")
        self.limit = limit
        self.candidates = candidates


class SessionManager:
    def __init__(self, max_runners: int = DEFAULT_MAX_RUNNERS) -> None:
        self.runners: dict[str, SessionRunner] = {}
        self.conns: set[Any] = set()   # WSConn objects (main.py)
        # set by main.py: closes the session's True View terminal, which
        # would otherwise outlive it with no way left to reach it
        self.on_delete = lambda sid: None
        # set by main.py: a live True View terminal is a `claude` process
        # too, so it holds a runner slot, and stopping the session ends it
        self.terminal_open = lambda sid: False
        # …and one being closed too, until its process has exited
        self.terminal_running = lambda sid: False
        self.close_terminal = lambda sid, reason: None
        self.max_runners = max_runners
        # newest rate-limit reading per window (five_hour, seven_day, …). The
        # limit is the account's, not the session's, so whichever session hears
        # it first answers for all of them. Memory only: after a restart it
        # stays empty until some session makes a request.
        self.usage: dict[str, dict] = {}
        self._sessions_update_handle: asyncio.Task | None = None
        self._incognito_lock = asyncio.Lock()
        self.load_from_disk()

    # ---------- how many may run at once ----------

    def live(self) -> list["SessionRunner"]:
        """Sessions holding a live process. A stopped or detached session
        costs nothing, so it does not count against the limit. One still
        connecting does: its process is already spawned. So does an open
        True View terminal (its own `claude`), one being opened and one
        closed but not yet exited."""
        return [r for r in self.runners.values()
                if r.client is not None or r._connecting or r._term_opening
                or self.terminal_running(r.sid)]

    def check_capacity(self, starting: "SessionRunner") -> None:
        """Raise RunnerLimit unless another process may be started now.

        The candidates are every runner that could be stopped safely —
        idle, nothing pending. They are NOT filtered by directory scope
        here: that happens once, at the boundary (rpc.runner_limit_error),
        so no call path can leak a session a device may not see."""
        live = [r for r in self.live() if r.sid != starting.sid]
        if len(live) < self.max_runners:
            return
        free = [r for r in live if r.state not in BUSY_STATES and not r.pending]
        free.sort(key=lambda r: r.last_active)
        raise RunnerLimit(self.max_runners, [r.stop_candidate() for r in free])

    async def make_room(self, starting: "SessionRunner") -> None:
        """check_capacity, after first stopping live sessions nobody ever
        wrote to, then idle incognito chats (oldest first in each group).
        Those are stopped whatever their folder: nothing is lost (a stopped
        incognito chat resumes, and its short context is cheap to reload),
        and a device limited to other folders could not pick them from the
        "stop one" list — it would be stuck."""
        while True:
            try:
                self.check_capacity(starting)
                return
            except RunnerLimit:
                others = [r for r in self.live() if r.sid != starting.sid]
                # never a True View terminal: someone may be typing in it
                empty = [r for r in others if r.is_empty()
                         and not self.terminal_open(r.sid)]
                if empty:
                    victim = min(empty, key=lambda r: r.last_active)
                    await victim.stop_unused(
                        "Stopped to free a slot — starts with your first message")
                else:
                    idle = [r for r in others if r.incognito
                            and not self.terminal_open(r.sid)
                            and r.state not in BUSY_STATES and not r.pending]
                    if not idle:
                        raise
                    victim = min(idle, key=lambda r: r.last_active)
                    await victim.stop_unused(
                        "Stopped to free a slot for another session — resumes "
                        "with your next message")
                if victim in self.live():
                    raise   # stopping freed nothing: refuse, never spin

    def terminal_closed(self, sid: str) -> None:
        """A True View terminal ended: the chat's "In terminal view" would
        otherwise stay, and the session list still show it as running."""
        r = self.runners.get(sid)
        if (r is not None and r.client is None and not r._connecting
                and r.state == "detached"):
            r.set_status("detached", "Stopped — resumes on next message")
        self.schedule_sessions_update()

    def load_from_disk(self) -> None:
        secure_dir(SESSIONS_DIR)
        for meta_path in SESSIONS_DIR.glob("*/meta.json"):
            try:
                meta = json.loads(meta_path.read_text())
                runner = SessionRunner.from_disk(self, meta)
                self.runners[runner.sid] = runner
            except (OSError, json.JSONDecodeError, KeyError):
                continue

    # ---------- session ops ----------

    async def create(self, cwd: str, model: str = "default",
                     mode: str = "default", resume: str | None = None,
                     fork: bool = False, title: str = "") -> SessionRunner:
        sid = uuid.uuid4().hex[:10]
        runner = SessionRunner(self, sid, cwd, title=title, model=model,
                               permission_mode=mode)
        if resume:
            # Resuming only shows the history, read from the transcript; the
            # process starts with the first message (the send path checks
            # the limit then). Opening an old session just to look at it
            # must not take one of the max_runners slots.
            runner.claude_ids.append(resume)
            runner.fork_pending = fork
            # Opening is not activity: the entry takes the time of the
            # transcript's last message, so a days-old session does not jump
            # to the top of the list until its first message (send() sets
            # last_active).
            ts = last_activity_ms(transcript_path(resume, cwd))
            if ts:
                runner.last_active = ts
            runner.set_status("detached", "Not started — starts with your first message")
            self.runners[sid] = runner
            runner.save_meta()
            self.schedule_sessions_update()
            with contextlib.suppress(Exception):
                await runner.backfill_history(resume)
            return runner
        await self._launch(runner)
        return runner

    async def _launch(self, runner: SessionRunner) -> None:
        # A new session starts right away, in the background below where an
        # exception would be swallowed — so the limit is checked here, while
        # the caller can still be told (rpc turns this into the "stop one to
        # make room" list).
        await self.make_room(runner)
        self.runners[runner.sid] = runner
        runner.save_meta()
        self.schedule_sessions_update()

        async def _bg_start():
            try:
                await runner.start()
            except RunnerLimit:
                # another start took the last slot after the check above
                runner.set_status("detached", "Not started — starts with your first message")
                runner.notice("Not started: the session limit was reached "
                              "meanwhile. It starts with your first message.", "error")
            except Exception:
                pass
        asyncio.create_task(_bg_start())

    async def create_incognito(self, owner: str, home: Path,
                               model: str = "default") -> SessionRunner:
        """The device's incognito chat: the one it already has, else a new
        one in a fresh folder under `home` (its INCOGNITO_DIR/<id>). One per
        device, so forgotten ones cannot pile up."""
        async with self._incognito_lock:
            mine = [r for r in self.runners.values()
                    if r.incognito and r.owner == owner]
            if mine:
                return mine[0]
            sid = uuid.uuid4().hex[:10]
            secure_dir(INCOGNITO_DIR)
            secure_dir(home)
            folder = secure_dir(home / sid)
            runner = SessionRunner(self, sid, str(folder), title="Incognito",
                                   model=model)
            runner.incognito = True
            runner.owner = owner
            try:
                await self._launch(runner)
            except BaseException:
                runner.cancel_watcher()
                shutil.rmtree(folder, ignore_errors=True)
                raise
            return runner

    def get(self, sid: str) -> SessionRunner | None:
        return self.runners.get(sid)

    async def delete(self, sid: str) -> None:
        runner = self.runners.pop(sid, None)
        if runner is None:
            return
        runner.deleted = True
        runner.cancel_watcher()
        runner.cancel_btw()
        self.on_delete(sid)
        await runner.stop()
        # the whole folder, whatever it holds: a meta.json left behind would
        # bring the session back at the next start
        try:
            await asyncio.to_thread(shutil.rmtree, runner.dir)
        except FileNotFoundError:
            pass
        except OSError as e:
            log.warning("%s deleted, but its folder was not fully removed: %s",
                        sid, e)
        if runner.incognito:
            await erase_incognito_dir(runner.cwd)
        self.schedule_sessions_update()

    async def erase_owner(self, owner: str) -> None:
        """A device signed out or was revoked: its incognito chat goes too."""
        for r in [r for r in self.runners.values()
                  if r.incognito and r.owner == owner]:
            await self.delete(r.sid)

    async def incognito_sweep(self, known_devices: set[str]) -> None:
        """Erase incognito chats idle for INCOGNITO_IDLE_HOURS or whose device
        is gone (also a revoke from the CLI, which runs in another process),
        then folders under INCOGNITO_DIR no chat owns any more — left by a
        crash halfway through an erase. Under _incognito_lock: a chat being
        created has its folder before it is in self.runners."""
        async with self._incognito_lock:
            await self._incognito_sweep(known_devices)

    async def _incognito_sweep(self, known_devices: set[str]) -> None:
        cutoff = now_ms() - INCOGNITO_IDLE_HOURS * 3600 * 1000
        for r in [r for r in self.runners.values() if r.incognito]:
            if r.owner not in known_devices or r.last_active < cutoff:
                await self.delete(r.sid)
        used = {Path(r.cwd) for r in self.runners.values() if r.incognito}
        try:
            homes = [d for d in INCOGNITO_DIR.iterdir() if d.is_dir()]
        except OSError:
            return
        for home in homes:
            # only this server's devices: another server on the same host
            # (a test one with its own data dir) has none of our chats and
            # would take every folder for abandoned
            if home.name not in known_devices:
                # a revoked device's chats went above; its empty folder
                # goes too, and a folder with anything left stays
                with contextlib.suppress(OSError):
                    home.rmdir()
                continue
            with contextlib.suppress(OSError):
                for folder in [d for d in home.iterdir() if d.is_dir()]:
                    if folder not in used:
                        await erase_incognito_dir(str(folder))
                home.rmdir()   # only once empty

    async def shutdown(self) -> None:
        await asyncio.gather(*(r.stop() for r in self.runners.values()
                               if r.client is not None),
                             return_exceptions=True)

    def _warm_transcript_stats(self) -> None:
        for r in list(self.runners.values()):
            with contextlib.suppress(Exception):
                r.transcript_stats()

    def snapshots(self) -> list[dict]:
        snaps = [r.snapshot() for r in self.runners.values()]
        snaps.sort(key=lambda s: s["last_active"], reverse=True)
        return snaps

    # ---------- broadcast ----------

    def broadcast_event(self, sid: str, ev: dict) -> None:
        for conn in list(self.conns):
            if sid in conn.subs:
                conn.push({"type": "event", "session_id": sid, "ev": ev})

    def schedule_sessions_update(self) -> None:
        if self._sessions_update_handle and not self._sessions_update_handle.done():
            return

        async def _fire():
            await asyncio.sleep(0.15)
            # a transcript grows all through a turn, and re-reading its tail
            # (up to TAIL_MAX) would stall the loop: warm the stats cache in
            # a thread so snapshot() only has to stat the file
            await asyncio.to_thread(self._warm_transcript_stats)
            snaps = self.snapshots()
            for conn in list(self.conns):
                conn.push_sessions(snaps)   # filtered to the conn's scope
        try:
            self._sessions_update_handle = asyncio.create_task(_fire())
        except RuntimeError:
            pass
