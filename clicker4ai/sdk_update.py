"""Keeping claude-agent-sdk in step with the `claude` CLI.

The runners drive the system `claude` (sessions.py, cli_path). It updates
itself only when started outside this server: every `claude` the server
starts has non-essential traffic, the auto-updater included, switched off
(main.py). The SDK in this environment stays put until someone installs a
newer one. Each SDK release is tested against one CLI version
(`__cli_version__`, the CLI it bundles), so the gap between the two is what
this module watches:

- once at start and then every CHECK_SECS it asks PyPI for the newest SDK
  inside SDK_RANGE (the same bound as pyproject.toml), npm for the newest
  CLI (CLI_CHANNEL) and `claude -v` for the CLI the runners use;
- `install()` runs pip in this environment, then imports the server's SDK
  users in a fresh interpreter; if that fails it puts the old version back,
  so a bad release never survives to the next restart;
- `update_cli()` runs `claude update` with the auto-updater allowed. New
  runners pick the new CLI up at once; running ones keep theirs until they
  stop, so no server restart is involved;
- the new SDK only takes effect after a restart. `restart_now()` asks the
  process to shut down gracefully (SIGTERM, the same as `pm2 restart`) and
  counts on a supervisor to start it again, so it is offered only under one
  (pm2 or systemd); `restart_when_idle` waits until no session is in a turn
  and no True View terminal is open.

Nothing here installs or restarts by itself: the app (manage grant + a fresh
passkey confirmation) or the CLI (`c4ai sdk update`) asks for it.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import importlib.util
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import claude_agent_sdk

PACKAGE = "claude-agent-sdk"
# Keep in step with pyproject.toml. The upper bound is where the SDK may
# change its API; a release past it needs a new clicker4ai, not a pip run.
SDK_MIN = (0, 2, 111)
SDK_BELOW = (0, 3)
SDK_RANGE = ">=0.2.111,<0.3"
PYPI_URL = f"https://pypi.org/pypi/{PACKAGE}/json"
CHECK_SECS = 24 * 3600
# a "check now" from the app or a restarted server asks PyPI at most this often
MIN_CHECK_SECS = 60
CLI_CACHE_SECS = 60
IDLE_POLL_SECS = 15
PIP_TIMEOUT_SECS = 300
# The npm dist-tag `claude update` follows by default ("stable" lags behind;
# a host on autoUpdatesChannel "stable" would be offered updates that
# `claude update` does not install, which update_cli() then reports).
CLI_CHANNEL = "latest"
NPM_TAGS_URL = "https://registry.npmjs.org/-/package/@anthropic-ai/claude-code/dist-tags"
CLI_UPDATE_TIMEOUT_SECS = 300
# what main.py sets for every `claude`; `claude update` needs them unset
UPDATE_BLOCKERS = ("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "DISABLE_AUTOUPDATER")
# the server's own modules that import the SDK: loading them in a fresh
# interpreter proves the new release still has every name they use
IMPORT_CHECK = ("import claude_agent_sdk, clicker4ai.sessions, clicker4ai.events, "
                "clicker4ai.projects, clicker4ai.history, clicker4ai.rpc")

# what this process imported at start; the version on disk may be newer
LOADED = getattr(claude_agent_sdk, "__version__", "") or ""


def _vtuple(v: str) -> tuple[int, ...] | None:
    """Final releases only ("0.2.158"); pre-releases and odd tags → None."""
    if not re.fullmatch(r"\d+(\.\d+)*", v or ""):
        return None
    return tuple(int(x) for x in v.split("."))


def in_range(v: str) -> bool:
    t = _vtuple(v)
    return t is not None and SDK_MIN <= t < SDK_BELOW


def newer(a: str, b: str) -> bool:
    ta, tb = _vtuple(a), _vtuple(b)
    return ta is not None and (tb is None or ta > tb)


def installed() -> str:
    """The version on disk now (differs from LOADED after an install)."""
    try:
        return importlib.metadata.version(PACKAGE)
    except importlib.metadata.PackageNotFoundError:
        return ""


def bundled_cli() -> str:
    """The CLI version the installed SDK was released with. Read from the
    file, not the imported module, so it follows an install too."""
    try:
        spec = importlib.util.find_spec("claude_agent_sdk")
        path = Path(spec.origin).parent / "_cli_version.py" if spec and spec.origin else None
        m = re.search(r'__cli_version__\s*=\s*"([^"]+)"', path.read_text()) if path else None
        return m.group(1) if m else ""
    except OSError:
        return ""


def system_cli() -> str:
    """`claude -v` of the CLI the runners start ("" if not found)."""
    exe = shutil.which("claude")
    if not exe:
        return ""
    try:
        out = subprocess.run([exe, "-v"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    m = re.match(r"(\d+\.\d+\.\d+)", out.stdout.strip())
    return m.group(1) if m else ""


def pypi_latest() -> str:
    """Newest final, non-yanked release inside SDK_RANGE."""
    req = urllib.request.Request(PYPI_URL, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.load(r)
    best = ""
    for v, files in (data.get("releases") or {}).items():
        if not files or all(f.get("yanked") for f in files):
            continue
        if in_range(v) and newer(v, best):
            best = v
    return best


def npm_latest_cli() -> str:
    """Newest CLI on CLI_CHANNEL ("" if the tag is missing or odd)."""
    req = urllib.request.Request(NPM_TAGS_URL, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        v = str(json.load(r).get(CLI_CHANNEL) or "")
    return v if _vtuple(v) else ""


def update_cli() -> dict:
    """`claude update`, then read the version back. Blocking (run it in a
    thread). → {"ok", "version", "message"}."""
    exe = shutil.which("claude")
    if not exe:
        return {"ok": False, "message": "no claude CLI on PATH"}
    before = system_cli()
    env = {k: v for k, v in os.environ.items() if k not in UPDATE_BLOCKERS}
    try:
        r = subprocess.run([exe, "update"], capture_output=True, text=True,
                           timeout=CLI_UPDATE_TIMEOUT_SECS, env=env,
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"ok": False, "message": f"claude update failed: {e}"}
    out = (r.stderr or r.stdout).strip()[-600:]
    if r.returncode != 0:
        return {"ok": False, "message": f"claude update failed: {out}"}
    after = system_cli()
    if after == before:
        return {"ok": False, "version": after,
                "message": f"claude update left the CLI at {after}: {out}"}
    return {"ok": True, "version": after,
            "message": f"CLI {before} → {after}; new sessions use it, "
                       "running ones once they stop"}


def can_install() -> bool:
    """pip runs only inside a virtual environment that has it (a venv or
    pipx); a system Python is the distribution's to manage."""
    return sys.prefix != sys.base_prefix and importlib.util.find_spec("pip") is not None


def pip_command(version: str) -> list[str]:
    return [sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
            f"{PACKAGE}=={version}"]


def hint_command(version: str) -> str:
    """What to run by hand where install() is not available."""
    if can_install():
        return " ".join(pip_command(version))
    if shutil.which("uv"):
        return f"uv pip install --python {sys.executable} {PACKAGE}=={version}"
    return f"{sys.executable} -m pip install {PACKAGE}=={version}"


def supervised() -> bool:
    """Whether a supervisor starts the server again after it exits: pm2
    (pm_id) or systemd (INVOCATION_ID). C4AI_SUPERVISED=1/0 overrides."""
    forced = os.environ.get("C4AI_SUPERVISED")
    if forced is not None:
        return forced == "1"
    return "pm_id" in os.environ or "INVOCATION_ID" in os.environ


def _import_check() -> tuple[bool, str]:
    root = Path(__file__).resolve().parent.parent
    try:
        r = subprocess.run([sys.executable, "-c", IMPORT_CHECK], cwd=root,
                           capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    return r.returncode == 0, (r.stderr or r.stdout).strip()[-600:]


def _pip(version: str) -> tuple[bool, str]:
    try:
        r = subprocess.run(pip_command(version), capture_output=True, text=True,
                           timeout=PIP_TIMEOUT_SECS)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    return r.returncode == 0, (r.stderr or r.stdout).strip()[-600:]


def install(version: str) -> dict:
    """pip install `version`, prove the server still imports, else roll
    back. Blocking (run it in a thread). → {"ok", "version", "message"}."""
    if not can_install():
        return {"ok": False, "message": "no pip in this environment; run: "
                + hint_command(version)}
    if not in_range(version):
        return {"ok": False, "message": f"{version} is outside {SDK_RANGE}"}
    before = installed()
    ok, out = _pip(version)
    if not ok:
        return {"ok": False, "message": f"pip failed: {out}"}
    ok, out = _import_check()
    if ok:
        return {"ok": True, "version": installed(),
                "message": f"installed {version}; restart to use it"}
    back_ok, back_out = _pip(before) if before else (False, "nothing to go back to")
    return {"ok": False,
            "message": f"{version} breaks the server ({out}); "
            + (f"put {before} back" if back_ok else f"could NOT put {before} back: {back_out}")}


class SdkWatch:
    """The server's view: last check, an install in progress, a pending
    restart. One per process (main.py)."""

    def __init__(self) -> None:
        self.latest = ""
        self.checked_at = 0.0
        self.error = ""
        self._cli = ("", 0.0)
        self.installing = ""
        self.last_install: dict | None = None
        self.cli_latest = ""
        self.cli_updating = False
        self.last_cli_update: dict | None = None
        self.restart_when_idle = False
        # set by main.py: sessions in a turn + open True View terminals
        self.busy = lambda: 0
        self._lock = asyncio.Lock()

    def cli(self) -> str:
        v, at = self._cli
        if time.time() - at > CLI_CACHE_SECS:
            v = system_cli()
            self._cli = (v, time.time())
        return v

    async def check(self, force: bool = False) -> None:
        if not force and time.time() - self.checked_at < MIN_CHECK_SECS:
            return
        self.checked_at = time.time()
        try:
            self.latest = await asyncio.to_thread(pypi_latest)
            self.error = ""
        except Exception as e:
            self.error = f"PyPI check failed: {e}"
        try:
            self.cli_latest = await asyncio.to_thread(npm_latest_cli)
        except Exception as e:
            self.error = " ".join(filter(None, (self.error, f"npm check failed: {e}")))
        self._cli = ("", 0.0)
        await asyncio.to_thread(self.cli)

    async def loop(self) -> None:
        while True:
            await self.check(force=True)
            await asyncio.sleep(CHECK_SECS)

    async def idle_restarter(self) -> None:
        """Carry out a "restart when idle" once nothing is running."""
        while True:
            await asyncio.sleep(IDLE_POLL_SECS)
            if (self.restart_when_idle and not self.installing and not self.cli_updating
                    and self.restart_pending() and supervised() and self.busy() == 0):
                restart_now()
                return

    def update_available(self) -> bool:
        return bool(self.latest) and newer(self.latest, installed())

    def cli_update_available(self) -> bool:
        cli = self.cli()
        return bool(cli) and bool(self.cli_latest) and newer(self.cli_latest, cli)

    def restart_pending(self) -> bool:
        return installed() != LOADED

    def attention(self) -> bool:
        """Worth a dot on the menu: something to install or to restart for."""
        return self.update_available() or self.cli_update_available() or self.restart_pending()

    def status(self, busy: int) -> dict:
        inst = installed()
        target = self.latest if self.update_available() else ""
        return {
            "loaded": LOADED,
            "installed": inst,
            "latest": self.latest,
            "range": SDK_RANGE,
            "bundled_cli": bundled_cli(),
            "system_cli": self.cli(),
            "checked_at": self.checked_at,
            "error": self.error,
            "update_available": bool(target),
            "can_install": can_install(),
            "hint": hint_command(target) if target else "",
            "installing": self.installing,
            "last_install": self.last_install,
            "cli_latest": self.cli_latest,
            "cli_channel": CLI_CHANNEL,
            "cli_update_available": self.cli_update_available(),
            "cli_updating": self.cli_updating,
            "last_cli_update": self.last_cli_update,
            "restart_pending": inst != LOADED,
            "can_restart": supervised(),
            "restart_when_idle": self.restart_when_idle,
            "busy": busy,
        }

    async def install(self, version: str) -> None:
        """Background task: the rpc returns at once, the app polls status."""
        if self._lock.locked():
            return
        async with self._lock:
            self.installing = version
            self.last_install = None
            try:
                self.last_install = await asyncio.to_thread(install, version)
            except Exception as e:
                self.last_install = {"ok": False, "message": str(e)}
            finally:
                self.installing = ""

    async def update_cli(self) -> None:
        """Background task like install(); shares its lock, so pip and
        `claude update` never run at once."""
        if self._lock.locked():
            return
        async with self._lock:
            self.cli_updating = True
            self.last_cli_update = None
            try:
                self.last_cli_update = await asyncio.to_thread(update_cli)
            except Exception as e:
                self.last_cli_update = {"ok": False, "message": str(e)}
            finally:
                self._cli = ("", 0.0)
                self.cli_updating = False


def restart_now() -> None:
    """Graceful shutdown (lifespan stops runners and terminals); the
    supervisor starts the server again on the new SDK."""
    os.kill(os.getpid(), signal.SIGTERM)


WATCH = SdkWatch()
