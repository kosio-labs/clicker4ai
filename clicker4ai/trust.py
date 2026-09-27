"""Folder trust: the terminal's "Do you trust the files in this folder?".

Claude Code asks that on the first run in a folder, but not when it runs
non-interactively — and the chat's `claude` does. Yet it loads the
folder's project settings like the terminal: hooks run on their own,
`permissions.allow` pre-approves tools, and a command with
`allowed-tools: Bash(...)` runs its !`…` lines when typed. So before a
`claude` process starts in a folder that holds any of that, the device
confirms trust once.

Trusted = this exact path trusted in the terminal (the CLI's
`hasTrustDialogAccepted` in ~/.claude.json) or from the app
(DATA_DIR/trusted.json). A trusted parent does not count: one "yes" to
~/work long ago would cover every repository cloned there later.
The CLI's file is only read — it rewrites it often, a second writer
could corrupt it.
"""

from __future__ import annotations

import json
from pathlib import Path

from .config import CLAUDE_JSON, DATA_DIR, write_private

TRUST_PATH = DATA_DIR / "trusted.json"

# what Claude Code loads from a project and may run without asking
SETTINGS_FILES = (".claude/settings.json", ".claude/settings.local.json")
MCP_FILE = ".mcp.json"
# settings keys holding a shell command the CLI runs itself
COMMAND_KEYS = ("apiKeyHelper", "awsAuthRefresh", "awsCredentialExport",
                "otelHeadersHelper", "statusLine")
SHOWN = 6            # names listed per entry before "…"
NAME_CUT = 40        # a long Bash(...) rule would fill a phone screen
MAX_FILES = 200      # command/skill files read per folder
MAX_READ = 256 * 1024

_cli_cache: tuple[float, dict] | None = None


class Untrusted(Exception):
    def __init__(self, cwd: str, items: list[dict], parent: dict | None = None):
        super().__init__(f"{cwd} is not trusted")
        self.cwd = cwd
        self.items = items
        self.parent = parent   # nearest trusted parent, shown as a hint


def _names(names: list[str]) -> str:
    more = len(names) - SHOWN
    cut = [n if len(n) <= NAME_CUT else n[:NAME_CUT - 1] + "…" for n in names[:SHOWN]]
    return ", ".join(cut) + (f" +{more}" if more > 0 else "")


def _cli_projects() -> dict:
    global _cli_cache
    try:
        mtime = CLAUDE_JSON.stat().st_mtime
        if _cli_cache is None or _cli_cache[0] != mtime:
            data = json.loads(CLAUDE_JSON.read_text(encoding="utf-8"))
            projects = data.get("projects") if isinstance(data, dict) else None
            _cli_cache = (mtime, projects if isinstance(projects, dict) else {})
        return _cli_cache[1]
    except (OSError, ValueError):
        return {}


def _app_trusted() -> list[str]:
    try:
        data = json.loads(TRUST_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [p for p in data if isinstance(p, str)] if isinstance(data, list) else []


def _trusted_by(cwd: str) -> str | None:
    """Where this exact path was trusted: "terminal", "app" or None."""
    entry = _cli_projects().get(cwd)
    if isinstance(entry, dict) and entry.get("hasTrustDialogAccepted") is True:
        return "terminal"
    return "app" if cwd in _app_trusted() else None


def is_trusted(cwd: str) -> bool:
    return _trusted_by(cwd) is not None


def trusted_parent(cwd: str) -> dict | None:
    """The nearest trusted parent: it does not cover cwd, but the app says
    so, or the question would look like a lost answer."""
    for folder in Path(cwd).parents:
        where = _trusted_by(str(folder))
        if where:
            return {"path": str(folder), "where": where}
    return None


def add(cwd: str) -> None:
    paths = _app_trusted()
    if cwd not in paths:
        write_private(TRUST_PATH, sorted(paths + [cwd]))


def remove(cwd: str) -> bool:
    """Take back the app's trust; the terminal's stays (its file is read
    only here). False when the app never trusted cwd."""
    paths = _app_trusted()
    if cwd not in paths:
        return False
    write_private(TRUST_PATH, [p for p in paths if p != cwd])
    return True


def listing() -> list[dict]:
    """Every trusted path: [{"path", "where": ["terminal", "app"]}]."""
    where: dict[str, list[str]] = {}
    for path, entry in _cli_projects().items():
        if isinstance(entry, dict) and entry.get("hasTrustDialogAccepted") is True:
            where.setdefault(path, []).append("terminal")
    for path in _app_trusted():
        where.setdefault(path, []).append("app")
    return [{"path": p, "where": w} for p, w in sorted(where.items())]


def _settings_note(path: Path) -> str:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("not an object")
    parts = []
    hooks = data.get("hooks")
    if isinstance(hooks, dict) and hooks:
        parts.append("hooks: " + _names(sorted(hooks)))
    perms = data.get("permissions")
    allow = perms.get("allow") if isinstance(perms, dict) else None
    if isinstance(allow, list) and allow:
        parts.append("allows: " + _names([str(a) for a in allow]))
    runs = [k for k in COMMAND_KEYS if data.get(k)]
    if runs:
        parts.append("runs: " + _names(runs))
    plugins = data.get("enabledPlugins")
    if isinstance(plugins, dict) and any(plugins.values()):
        parts.append("plugins: " + _names(sorted(k for k, v in plugins.items() if v)))
    if data.get("enableAllProjectMcpServers"):
        parts.append("enables all MCP servers")
    return "; ".join(parts) or "no hooks, no allow rules"


def _runs_shell(path: Path) -> bool:
    with path.open("rb") as f:
        return b"!`" in f.read(MAX_READ)


def _dir_note(path: Path, kind: str) -> str | None:
    """Count of commands/skills/agents, naming those with !`…` lines."""
    if kind == "skills":
        files = sorted(path.rglob("SKILL.md"))[:MAX_FILES]
        names = [f.parent.relative_to(path).as_posix() for f in files]
    else:
        files = sorted(path.rglob("*.md"))[:MAX_FILES]
        names = [f.relative_to(path).with_suffix("").as_posix() for f in files]
    if not files:
        return None
    note = f"{len(files)} {kind[:-1] if len(files) == 1 else kind}"
    if kind != "agents":
        shell = [n for n, f in zip(names, files) if _runs_shell(f)]
        if shell:
            note += ", runs shell: " + _names(shell)
    return note


def _mcp_note(path: Path) -> str:
    data = json.loads(path.read_text(encoding="utf-8"))
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(servers, dict) or not servers:
        return "no servers"
    shown = []
    for name, cfg in servers.items():
        cfg = cfg if isinstance(cfg, dict) else {}
        what = cfg.get("command") or cfg.get("url") or "?"
        shown.append(f"{name} → {what}")
    return "mcp: " + _names(shown)


def findings(cwd: str) -> list[dict]:
    """What Claude Code would load and could run here: entries in the
    folder and its parents up to (not including) the home directory, whose
    ~/.claude is the user's own. [{"path", "note"}], paths relative to cwd."""
    home = Path.home()
    base = Path(cwd)
    out: list[dict] = []
    for folder in [base, *base.parents]:
        if folder == home or folder == folder.parent:
            break
        prefix = "../" * len(base.relative_to(folder).parts)

        def check(rel: str, note_of) -> None:
            p = folder / rel
            if not p.exists():
                return
            try:
                note = note_of(p)
            except (OSError, ValueError, UnicodeDecodeError):
                note = "could not read"
            if note is not None:
                out.append({"path": prefix + rel + ("/" if p.is_dir() else ""),
                            "note": note})

        for rel in SETTINGS_FILES:
            check(rel, _settings_note)
        for kind in ("commands", "skills", "agents"):
            check(f".claude/{kind}", lambda p, k=kind: _dir_note(p, k))
        check(MCP_FILE, _mcp_note)
    return out


def check(cwd: str) -> None:
    """Raise Untrusted when a `claude` process may not start in cwd yet."""
    if is_trusted(cwd):
        return
    items = findings(cwd)
    if items:
        raise Untrusted(cwd, items, trusted_parent(cwd))
