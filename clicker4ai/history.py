"""Convert a past session's transcript into replayable UI events.

Used when a UI session resumes an existing claude session so the phone
shows the full prior conversation, exactly like scrolling up in the
terminal. The SDK's get_session_messages() already filters subagent
sidechains and returns only top-level user/assistant rows — but it stops at
the last compaction, so the rows before it are read from the transcript
(see _pre_compact_rows).
"""

from __future__ import annotations

import json
import re

from claude_agent_sdk import get_session_messages

from .events import _tool_result_text, clip, clip_input, tool_summary

HISTORY_TAIL = 1000  # max transcript rows converted (oldest dropped)

_COMPACT_PREFIX = "This session is being continued from a previous conversation"


def _clean_user_text(text: str) -> str:
    """Strip harness noise from transcript user rows; '' means skip."""
    text = re.sub(r"<system-reminder>.*?</system-reminder>", "", text,
                  flags=re.S).strip()
    if not text or text.startswith("Caveat: The messages below"):
        return ""
    if text.startswith(_COMPACT_PREFIX):
        return ""  # rendered as a compact divider by the caller
    if "<command-name>" in text:
        m = re.search(r"<command-name>(.*?)</command-name>", text, flags=re.S)
        name = (m.group(1).strip() if m else "").strip()
        args = re.search(r"<command-args>(.*?)</command-args>", text, flags=re.S)
        arg = (args.group(1).strip() if args else "")
        return (name + (" " + arg if arg else "")) if name else ""
    if text.startswith(("<local-command", "<session-start-hook")):
        return ""
    return text


def _user_events(content) -> list[dict]:
    evs: list[dict] = []
    if isinstance(content, str):
        if content.startswith(_COMPACT_PREFIX):
            return [{"kind": "compact", "trigger": "past", "pre_tokens": None}]
        cleaned = _clean_user_text(content)
        if cleaned:
            evs.append({"kind": "user_text", "text": clip(cleaned)})
        return evs
    if not isinstance(content, list):
        return evs
    texts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            texts.append(block.get("text", ""))
        elif btype == "tool_result":
            evs.append({
                "kind": "tool_result",
                "tool_use_id": block.get("tool_use_id"),
                "text": _tool_result_text(block.get("content")),
                "is_error": bool(block.get("is_error")),
            })
        elif btype == "image":
            texts.append("[image]")
    joined = "\n".join(t for t in texts if t).strip()
    if joined and joined.startswith(_COMPACT_PREFIX):
        return [{"kind": "compact", "trigger": "past", "pre_tokens": None}] + evs
    cleaned = _clean_user_text(joined) if joined else ""
    if cleaned:
        evs.insert(0, {"kind": "user_text", "text": clip(cleaned)})
    return evs


def _assistant_events(content) -> list[dict]:
    evs: list[dict] = []
    if isinstance(content, str):
        if content.strip():
            evs.append({"kind": "text", "text": clip(content)})
        return evs
    if not isinstance(content, list):
        return evs
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            if (block.get("text") or "").strip():
                evs.append({"kind": "text", "text": clip(block["text"])})
        elif btype == "thinking":
            if (block.get("thinking") or "").strip():
                evs.append({"kind": "thinking", "text": clip(block["thinking"])})
        elif btype == "tool_use":
            name = block.get("name", "")
            tin = block.get("input") or {}
            evs.append({
                "kind": "tool_start",
                "tool_use_id": block.get("id"),
                "tool": name,
                "input": clip_input(tin),
                "summary": tool_summary(name, tin),
                "parent_tool_use_id": None,
            })
    return evs


def _pre_compact_rows(session_id: str, cwd: str | None,
                      first_uuid: str | None) -> list[tuple[str, object]]:
    """(type, content) of the visible rows before the SDK chain starts,
    oldest first. A compaction starts a new chain (parentUuid null) whose
    boundary row keeps the old leaf in logicalParentUuid; follow those
    links back so a resumed session shows what the terminal shows."""
    from .watch import transcript_path   # watch imports this module
    path = transcript_path(session_id, cwd) if first_uuid else None
    if path is None:
        return []
    by_id: dict[str, dict] = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and row.get("uuid"):
                    by_id[row["uuid"]] = row
    except OSError:
        return []
    out: list[tuple[str, object]] = []
    seen: set[str] = set()
    row = by_id.get(first_uuid or "")
    cur = row and (row.get("parentUuid") or row.get("logicalParentUuid"))
    while cur and cur not in seen and cur in by_id:
        seen.add(cur)
        row = by_id[cur]
        msg = row.get("message")
        if (row.get("type") in ("user", "assistant") and isinstance(msg, dict)
                and msg.get("content") is not None and not row.get("isMeta")
                and not row.get("isSidechain") and not row.get("teamName")):
            out.append((row["type"], msg["content"]))
        cur = row.get("parentUuid") or row.get("logicalParentUuid")
    out.reverse()
    return out


def history_events(session_id: str, cwd: str | None,
                   tail: int = HISTORY_TAIL) -> tuple[list[dict], int]:
    """(events, total_transcript_rows) for a past session."""
    try:
        messages = get_session_messages(session_id, directory=cwd)
    except Exception:
        return [], 0
    rows: list[tuple[str, object]] = []
    for m in messages:
        raw = m.message if isinstance(m.message, dict) else {}
        rows.append((m.type, raw.get("content")))
    if messages:
        rows = _pre_compact_rows(session_id, cwd, messages[0].uuid) + rows
    total = len(rows)
    if total > tail:
        rows = rows[-tail:]
    evs: list[dict] = []
    for kind, content in rows:
        if kind == "user":
            evs.extend(_user_events(content))
        elif kind == "assistant":
            evs.extend(_assistant_events(content))
    return evs, total


def chain_prompts(session_id: str, cwd: str | None) -> list[tuple[str, str, str | None]]:
    """(uuid, text, uuid of the entry before it) for each user prompt on the
    session's current chain, oldest first — the text as the chat shows it
    (_user_events), so it can be matched against a user_text event. The
    chain starts at the last compaction; earlier prompts are not listed."""
    try:
        messages = get_session_messages(session_id, directory=cwd)
    except Exception:
        return []
    out: list[tuple[str, str, str | None]] = []
    for i, m in enumerate(messages):
        if m.type != "user":
            continue
        raw = m.message if isinstance(m.message, dict) else {}
        for ev in _user_events(raw.get("content")):
            if ev.get("kind") == "user_text":
                out.append((m.uuid, ev["text"], messages[i - 1].uuid if i else None))
                break
    return out
