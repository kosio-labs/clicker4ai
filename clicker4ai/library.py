"""Discovery of skills, slash commands, agents and MCP servers.

Reads the same places the Claude Code CLI does:
  - user scope:    ~/.claude/{skills,commands,agents}, ~/.claude.json mcpServers
  - project scope: <cwd>/.claude/{skills,commands,agents}, <cwd>/.mcp.json,
                   ~/.claude.json projects[<cwd>].mcpServers
  - plugins:       ~/.claude/plugins/installed_plugins.json, filtered by
                   install scope and the merged enabledPlugins
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from .config import CLAUDE_DIR as CLAUDE_HOME, CLAUDE_JSON

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)

# ~/.claude.json is large; cache parsed copy keyed by mtime.
_claude_json_cache: tuple[float, dict] | None = None


def _read_claude_json() -> dict:
    global _claude_json_cache
    try:
        mtime = CLAUDE_JSON.stat().st_mtime
    except OSError:
        return {}
    if _claude_json_cache and _claude_json_cache[0] == mtime:
        return _claude_json_cache[1]
    try:
        data = json.loads(CLAUDE_JSON.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    _claude_json_cache = (mtime, data)
    return data


def _parse_frontmatter(text: str) -> dict[str, str]:
    """Tolerant single-level YAML frontmatter parser (no yaml dependency)."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}
    fields: dict[str, str] = {}
    current_key: str | None = None
    for line in m.group(1).splitlines():
        if not line.strip() or line.strip().startswith("#"):
            continue
        km = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", line)
        if km:
            current_key = km.group(1).lower()
            val = km.group(2).strip().strip("\"'")
            # ignore folded/literal markers, accumulate continuation lines
            fields[current_key] = "" if val in (">", ">-", "|", "|-") else val
        elif current_key and line.startswith((" ", "\t")):
            fields[current_key] = (fields[current_key] + " " + line.strip()).strip()
    return fields


def _head(path: Path, limit: int = 16384) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read(limit)
    except OSError:
        return ""


def _scan_skills(base: Path, scope: str, source: str = "") -> list[dict]:
    out = []
    root = base / "skills"
    if not root.is_dir():
        return out
    for skill_md in sorted(root.glob("*/SKILL.md")):
        fm = _parse_frontmatter(_head(skill_md))
        out.append({
            "name": fm.get("name") or skill_md.parent.name,
            "description": fm.get("description", ""),
            "scope": scope,
            "source": source,
            "path": str(skill_md),
        })
    return out


def _scan_commands(base: Path, scope: str, source: str = "") -> list[dict]:
    out = []
    root = base / "commands"
    if not root.is_dir():
        return out
    for md in sorted(root.rglob("*.md")):
        rel = md.relative_to(root)
        name = ":".join([*rel.parts[:-1], rel.stem])
        fm = _parse_frontmatter(_head(md))
        out.append({
            "name": name,
            "description": fm.get("description", ""),
            "argument_hint": fm.get("argument-hint", ""),
            "model": fm.get("model", ""),
            "scope": scope,
            "source": source,
            "path": str(md),
        })
    return out


def _scan_agents(base: Path, scope: str, source: str = "") -> list[dict]:
    out = []
    root = base / "agents"
    if not root.is_dir():
        return out
    for md in sorted(root.glob("*.md")):
        fm = _parse_frontmatter(_head(md))
        out.append({
            "name": fm.get("name") or md.stem,
            "description": fm.get("description", ""),
            "model": fm.get("model", ""),
            "tools": fm.get("tools", ""),
            "scope": scope,
            "source": source,
            "path": str(md),
        })
    return out


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _enabled_plugins(cwd: str | None) -> dict[str, bool]:
    """enabledPlugins merged the way the CLI reads it: user settings, then the
    project's .claude/settings.json, then .claude/settings.local.json; a
    later file overrides an earlier one, false means installed but off."""
    files = [CLAUDE_HOME / "settings.json"]
    if cwd:
        files += [Path(cwd) / ".claude" / "settings.json",
                  Path(cwd) / ".claude" / "settings.local.json"]
    merged: dict[str, bool] = {}
    for f in files:
        ep = _read_json(f).get("enabledPlugins")
        if isinstance(ep, dict):
            merged.update({k: v is True for k, v in ep.items()})
    return merged


def _plugin_roots(cwd: str | None) -> list[tuple[str, Path]]:
    """(plugin_name, root_dir) for the plugins a session in `cwd` loads: an
    installed_plugins.json entry whose scope covers `cwd` (user/managed
    everywhere, project/local only in their projectPath) and that
    enabledPlugins does not switch off."""
    data = _read_json(CLAUDE_HOME / "plugins" / "installed_plugins.json")
    plugins = data.get("plugins") if isinstance(data.get("plugins"), dict) else data
    enabled = _enabled_plugins(cwd)
    here = str(Path(cwd).resolve()) if cwd else None
    roots: list[tuple[str, Path]] = []
    seen: set[Path] = set()
    for key, val in plugins.items():
        if not enabled.get(str(key), False):
            continue
        for entry in val if isinstance(val, list) else [val]:
            if not isinstance(entry, dict):
                continue
            scope = entry.get("scope", "user")
            if scope in ("project", "local"):
                pp = entry.get("projectPath")
                if not here or not isinstance(pp, str) or str(Path(pp).resolve()) != here:
                    continue
            ip = entry.get("installPath")
            if not isinstance(ip, str):
                continue
            path = Path(ip)
            if path.is_dir() and path not in seen:
                seen.add(path)
                roots.append((str(key).split("@")[0], path))
    return roots


def _mcp_transport(cfg: dict) -> str:
    t = cfg.get("type")
    if t:
        return t
    if cfg.get("url"):
        return "http/sse"
    if cfg.get("command"):
        return "stdio"
    return "unknown"


def _dict(val) -> dict:
    return val if isinstance(val, dict) else {}


def _mcp_servers(cwd: str | None) -> list[dict]:
    """Hand-edited files, and .mcp.json comes with whatever repo the folder
    holds: an entry of the wrong shape is skipped, not fatal."""
    out = []

    def add(servers, scope: str) -> None:
        for name, cfg in _dict(servers).items():
            if isinstance(cfg, dict):
                out.append({
                    "name": name, "scope": scope,
                    "transport": _mcp_transport(cfg),
                    "detail": cfg.get("url") or cfg.get("command", ""),
                })

    data = _dict(_read_claude_json())
    add(data.get("mcpServers"), "user")
    if cwd:
        proj = _dict(_dict(data.get("projects")).get(cwd))
        add(proj.get("mcpServers"), "local")
        mcp_json = Path(cwd) / ".mcp.json"
        if mcp_json.exists():
            try:
                pdata = json.loads(mcp_json.read_text())
            except (OSError, json.JSONDecodeError):
                pdata = None
            add(_dict(pdata).get("mcpServers"), "project")
    return out


_library_cache: dict[str, tuple[float, dict]] = {}


def get_library(cwd: str | None = None) -> dict:
    """Full library snapshot for user scope + optional project scope."""
    key = cwd or ""
    cached = _library_cache.get(key)
    if cached and time.monotonic() - cached[0] < 15:
        return cached[1]

    skills = _scan_skills(CLAUDE_HOME, "user")
    commands = _scan_commands(CLAUDE_HOME, "user")
    agents = _scan_agents(CLAUDE_HOME, "user")

    for plugin_name, root in _plugin_roots(cwd):
        skills += _scan_skills(root, "plugin", plugin_name)
        commands += _scan_commands(root, "plugin", plugin_name)
        agents += _scan_agents(root, "plugin", plugin_name)

    if cwd:
        base = Path(cwd) / ".claude"
        skills += _scan_skills(base, "project")
        commands += _scan_commands(base, "project")
        agents += _scan_agents(base, "project")

    result = {
        "skills": skills,
        "commands": commands,
        "agents": agents,
        "mcp_servers": _mcp_servers(cwd),
        "cwd": cwd,
    }
    _library_cache[key] = (time.monotonic(), result)
    return result
