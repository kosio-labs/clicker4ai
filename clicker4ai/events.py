"""Normalize claude-agent-sdk messages into flat UI event dicts.

Every event has a "kind". Events marked transient=True are broadcast live
but not persisted (streaming deltas, status hints) — the final message
supersedes them.
"""

from __future__ import annotations

from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

try:  # newer SDKs
    from claude_agent_sdk import RateLimitEvent
except ImportError:  # pragma: no cover
    RateLimitEvent = None

MAX_TEXT = 24_000
MAX_FIELD = 8_000


# how a refusal names the window that ran out
WINDOW_LABELS = {
    "five_hour": "5-hour",
    "seven_day": "weekly",
    "seven_day_opus": "weekly Opus",
    "seven_day_sonnet": "weekly Sonnet",
    "overage": "overage",
}


def usage_windows(info) -> dict[str, dict]:
    """How much of the plan is gone, per window. The CLI puts a reading for
    every window (five_hour, seven_day, …) under `unifiedWindows` and leaves
    the top-level `utilization` unset, so reading the flat field alone yields
    nothing; that field is the fallback for payloads without the map."""
    out: dict[str, dict] = {}
    for name, w in ((info.raw or {}).get("unifiedWindows") or {}).items():
        if isinstance(w, dict) and w.get("utilization") is not None:
            out[name] = {"utilization": w["utilization"],
                         "resets_at": w.get("resetsAt")}
    if not out and info.utilization is not None:
        out[info.rate_limit_type or "unknown"] = {
            "utilization": info.utilization, "resets_at": info.resets_at}
    return out


def clip(s: str, limit: int = MAX_TEXT) -> str:
    if s is None:
        return ""
    if len(s) <= limit:
        return s
    head = s[: int(limit * 0.75)]
    tail = s[-int(limit * 0.15):]
    return f"{head}\n… [{len(s) - len(head) - len(tail)} chars truncated] …\n{tail}"


def clip_input(tool_input: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for k, v in (tool_input or {}).items():
        if isinstance(v, str):
            out[k] = clip(v, MAX_FIELD)
        else:
            out[k] = v
    return out


def tool_summary(name: str, tool_input: dict[str, Any]) -> str:
    """One-line human summary used in status lines and card headers."""
    i = tool_input or {}

    def base(p):
        return str(p).rsplit("/", 1)[-1] if p else ""

    try:
        if name == "Bash":
            cmd = (i.get("command") or "").strip().splitlines()
            return (i.get("description") or (cmd[0] if cmd else ""))[:90]
        if name in ("Edit", "Write", "NotebookEdit"):
            return base(i.get("file_path") or i.get("notebook_path"))
        if name == "Read":
            return base(i.get("file_path"))
        if name == "Grep":
            return i.get("pattern", "")[:60]
        if name == "Glob":
            return i.get("pattern", "")[:60]
        if name in ("Task", "Agent"):
            return i.get("description", "")[:80]
        if name == "WebFetch":
            return i.get("url", "")[:80]
        if name == "WebSearch":
            return i.get("query", "")[:80]
        if name == "Skill":
            return i.get("skill") or i.get("command", "")
        if name == "TodoWrite":
            todos = i.get("todos") or []
            done = sum(1 for t in todos if t.get("status") == "completed")
            return f"{done}/{len(todos)} done"
        if name == "ExitPlanMode":
            return "plan ready"
        if name == "AskUserQuestion":
            qs = i.get("questions") or []
            return qs[0].get("header") or qs[0].get("question", "")[:60] if qs else ""
        if name.startswith("mcp__"):
            parts = name.split("__")
            return f"{parts[1]}: {parts[-1]}" if len(parts) >= 3 else name
    except Exception:
        pass
    return ""


def _tool_result_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return clip(content)
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif block.get("type") == "image":
                    parts.append("[image]")
            else:
                t = getattr(block, "text", None)
                if t:
                    parts.append(t)
        return clip("\n".join(parts))
    return clip(str(content))


def normalize_message(msg: Any) -> list[dict[str, Any]]:
    """SDK message → list of UI events."""
    evs: list[dict[str, Any]] = []

    if isinstance(msg, StreamEvent):
        e = msg.event or {}
        etype = e.get("type")
        if msg.parent_tool_use_id:
            # Subagent activity: surface as a status hint only.
            if etype == "content_block_start":
                evs.append({"kind": "status_hint", "detail": "agent working",
                            "transient": True})
            return evs
        if etype == "content_block_start":
            btype = (e.get("content_block") or {}).get("type")
            if btype == "thinking":
                evs.append({"kind": "thinking_open", "transient": True})
            elif btype == "text":
                evs.append({"kind": "text_open", "transient": True})
        elif etype == "content_block_delta":
            delta = e.get("delta") or {}
            if delta.get("type") == "text_delta":
                evs.append({"kind": "delta", "text": delta.get("text", ""),
                            "transient": True})
            elif delta.get("type") == "thinking_delta":
                evs.append({"kind": "thinking_delta",
                            "text": delta.get("thinking", ""), "transient": True})
        elif etype == "content_block_stop":
            evs.append({"kind": "block_stop", "transient": True})
        return evs

    if isinstance(msg, AssistantMessage):
        parent = msg.parent_tool_use_id
        for block in msg.content:
            if isinstance(block, TextBlock):
                if not parent:
                    evs.append({"kind": "text", "text": clip(block.text)})
            elif isinstance(block, ThinkingBlock):
                if not parent:
                    evs.append({"kind": "thinking", "text": clip(block.thinking)})
            elif isinstance(block, ToolUseBlock):
                evs.append({
                    "kind": "tool_start",
                    "tool_use_id": block.id,
                    "tool": block.name,
                    "input": clip_input(block.input),
                    "summary": tool_summary(block.name, block.input),
                    "parent_tool_use_id": parent,
                })
        if msg.error:
            evs.append({"kind": "notice", "level": "error",
                        "text": f"Assistant error: {msg.error}"})
        return evs

    if isinstance(msg, UserMessage):
        content = msg.content
        if isinstance(content, list):
            for block in content:
                if isinstance(block, ToolResultBlock):
                    evs.append({
                        "kind": "tool_result",
                        "tool_use_id": block.tool_use_id,
                        "text": _tool_result_text(block.content),
                        "is_error": bool(block.is_error),
                        "parent_tool_use_id": msg.parent_tool_use_id,
                    })
        # Plain-text user messages are echoes (slash-command expansion etc.)
        # — we already record what the user sent, so skip them.
        return evs

    if isinstance(msg, ResultMessage):
        evs.append({
            "kind": "result",
            "subtype": msg.subtype,
            "is_error": msg.is_error,
            "duration_ms": msg.duration_ms,
            "num_turns": msg.num_turns,
            "cost_usd": msg.total_cost_usd,
            "usage": msg.usage or {},
            "stop_reason": msg.stop_reason,
            "errors": msg.errors or [],
            "claude_session_id": msg.session_id,
        })
        return evs

    if RateLimitEvent is not None and isinstance(msg, RateLimitEvent):
        info = msg.rate_limit_info
        windows = usage_windows(info)
        # The numbers ride every request, whatever the status; the app shows
        # them in the chat's status line and the info sheet. Transient: one
        # row per request would bury the log.
        evs.append({
            "kind": "usage",
            "transient": True,
            "rate_limit": info.status,
            "windows": windows,
        })
        if info.status != "rejected":
            # "allowed_warning" is not news: the status line already shows
            # how much is gone, and the warning came back on every resume.
            return evs
        # Only a refusal goes into the chat — it is why the turn did not
        # run — and it says which limit ran out and when it resets.
        window = info.rate_limit_type or max(
            windows, key=lambda k: windows[k]["utilization"], default=None)
        resets = (windows.get(window) or {}).get("resets_at") or info.resets_at
        evs.append({
            "kind": "notice",
            "level": "error",
            "rate_limit": "rejected",
            "window": window,
            "resets_at": resets,
            "text": f"Rate limit reached — {WINDOW_LABELS.get(window, window or 'plan')} limit",
        })
        return evs

    if isinstance(msg, SystemMessage):
        sub = msg.subtype
        data = msg.data or {}
        if sub == "init":
            mcp = data.get("mcp_servers") or []
            evs.append({
                "kind": "meta",
                "claude_session_id": data.get("session_id"),
                "model": data.get("model"),
                "cwd": data.get("cwd"),
                "permission_mode": data.get("permissionMode"),
                "tools_count": len(data.get("tools") or []),
                "slash_commands": data.get("slash_commands") or [],
                "skills": data.get("skills") or [],
                "mcp_servers": [
                    {"name": s.get("name"), "status": s.get("status")}
                    if isinstance(s, dict) else {"name": str(s), "status": ""}
                    for s in mcp
                ],
                "output_style": data.get("output_style"),
                "agents": data.get("agents") or [],
            })
        elif sub == "compact_boundary":
            meta = data.get("compact_metadata") or {}
            evs.append({
                "kind": "compact",
                "trigger": meta.get("trigger") or data.get("trigger", ""),
                "pre_tokens": meta.get("pre_tokens") or data.get("pre_tokens"),
            })
        elif sub in ("hook_started", "hook_response"):
            # Turn-end hooks (e.g. claude-mem's summarize, up to 120s) delay
            # the result message; without feedback the app looks stuck.
            hook = (getattr(msg, "hook_event_name", "")
                    or data.get("hook_event") or "")
            if hook in ("Stop", "SubagentStop"):
                if sub == "hook_started":
                    evs.append({"kind": "status_hint", "transient": True,
                                "detail": "Reply done — running Stop hooks…"})
                else:
                    evs.append({"kind": "status_hint", "transient": True,
                                "detail": "Stop hook finished — wrapping up…"})
        elif sub in ("task_started", "task_progress", "task_notification",
                     "task_updated"):
            summary = data.get("summary") or data.get("status") or sub
            evs.append({"kind": "notice", "level": "info",
                        "text": f"Background task: {summary}"})
        else:
            evs.append({"kind": "system", "subtype": sub,
                        "text": clip(str(data), 500), "transient": True})
        return evs

    evs.append({"kind": "debug", "text": clip(repr(msg), 400), "transient": True})
    return evs
