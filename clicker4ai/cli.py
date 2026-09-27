"""Command line entry point:

    c4ai [serve] [--host IP] [--port N] [--public-url URL] [--no-qr] [--no-pair]
    c4ai pair [--root DIR ... | --all-roots] [--manage-devices] [--terminal]
              [--no-passkey] [--quiet-exempt] [--files] [--files-upload]
              [--public-url URL] [--no-qr]
    c4ai devices
    c4ai devices set <device> [--root DIR ... | --add-root DIR ... | --all-roots
                     | --no-roots] [--[no-]manage-devices] [--[no-]terminal]
                     [--[no-]passkey] [--[no-]quiet-exempt]
                     [--[no-]files] [--[no-]files-upload]
    c4ai devices lock|unlock <device>
    c4ai devices rename <device> <name>
    c4ai revoke <device>
    c4ai passkeys [remove <passkey-id> | rename <passkey-id> <name>]
    c4ai trust [add <folder> | remove <folder>]
    c4ai quiet-hours [HH:MM-HH:MM | off] [--tz ZONE | --tz host]
    c4ai config [set <key> <value> | unset <key> | roots add|remove <folder>]
    c4ai sdk [update [--yes]]

<device> is a device id or its name (names are unique and never equal
another device's id; an exact id wins).

`serve` (the default) runs the local server. Binds 127.0.0.1 unless --host
(or "host" in ~/.clicker4ai/config.json) says otherwise. --public-url (or
"public_url" in ~/.clicker4ai/config.json) is the address phones use, e.g. behind a
TLS reverse proxy; pairing links and QR codes point there.

Login is per device: `pair` (and every server start) prints a one-time
pairing code, link and QR; `devices` / `revoke` manage signed-in devices.
The server never prints a long-lived credential.

Grants are set here only, never from the app. A new device starts with
**no folders**: `--root` gives it one (repeatable, inside "allowed_roots"
in ~/.clicker4ai/config.json) and `--all-roots` gives it every one of them;
`devices set` changes this later, with the same flags. `--manage-devices` lets it list and revoke other
devices in the app (without it a device only sees and signs out itself)
and `--terminal` allows True View — the real Claude Code TUI, where `!cmd`
runs shell commands directly, so it is off by default.

`devices lock` puts a device session to sleep without touching its grants:
its cookie stays valid but every request is refused until a passkey wakes
it in the app (or `devices unlock` here). Useful for a phone you have
mislaid but do not want to re-pair. A locked session stops sliding its
30-day idle expiry.

`trust` lists the folders whose project settings (hooks, allow rules,
commands with !`…` lines, .mcp.json) may run without asking — trusted in
the terminal (~/.claude.json, read only here) or from the app
(trust.py). `trust add` does what "Trust and continue" does in the app;
`trust remove` takes the app's trust back, so the app asks again.

Passkeys (passkey.py) are registered from the app, not here; `passkeys`
lists them, `passkeys remove` deletes one and `passkeys rename` relabels
one (the label is the server's own; the keychain shows "Clicker4AI" for
every key). A passkey carries no grants
at all: signing in with one creates a device with no folders, which then
needs `devices set` here like any other — that is the whole point of
grants being CLI-only.

A device paired by code or signed in with a passkey must carry a passkey:
until it registers one the app offers nothing else (`pair --no-passkey`
skips that, e.g. for a desktop without a platform authenticator). The
app can switch the requirement on; only `devices set <device> --no-passkey`
switches it off. A device with a passkey is locked after
"lock_idle_minutes" (~/.clicker4ai/config.json, default 15, 0 = never) without a
request.

`--files` (on `pair` or `devices set`) lets a device list, view and download
the files under its folders in the app (no Claude involved); `--files-upload`
also lets it upload (up to 50 MB, never over an existing file) and turns
`--files` on with it; `devices set <device> --no-files` turns both off
(files.py).

`quiet-hours` sets a nightly window (e.g. 00:30-08:00; host's local time,
or `--tz Europe/London`) in which no device may start a new turn — a running one finishes. It is
CLI-only on purpose, so the phone cannot lift it; `--quiet-exempt` (on
`pair` or `devices set`) leaves one device out (quiet.py).

`config` shows ~/.clicker4ai/config.json key by key; `config set` and
`config unset` change one key, checked as the server checks it, and leave
the rest of the file as written. `--host`, `--port` and `--public-url` on
`serve` are for that one run; `config set` keeps them. `config roots
add|remove` changes allowed_roots and names the devices that gain or lose
folders by it. All of it applies when the server restarts.

`sdk` compares claude-agent-sdk with the `claude` CLI the runners use and
asks PyPI for a newer SDK inside the supported range (sdk_update.py);
`sdk update` installs it into this environment, checks the server still
imports and otherwise puts the old one back. The running server keeps the
old SDK until it is restarted. `sdk update-cli` runs `claude update` when
npm has a newer CLI (the CLI does not update itself from inside the
server); new sessions use it without a restart. The app offers both to a
device with the manage grant, after a passkey confirmation.
"""

from __future__ import annotations

import argparse
import functools
import logging
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

WILDCARD_HOSTS = {"0.0.0.0", "::"}


def _public_url(raw: str) -> str:
    """argparse type: absolute http(s) URL without path, trailing / removed."""
    url = raw.strip().rstrip("/")
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.netloc or u.path or u.query or u.fragment:
        raise argparse.ArgumentTypeError(
            "expected http(s)://host[:port] without a path, e.g. https://rc.example.com")
    return url


def _resolve_public(args: argparse.Namespace, cfg) -> str | None:
    if args.public_url is not None:
        return args.public_url
    if cfg.extra.get("public_url"):
        try:
            return _public_url(str(cfg.extra["public_url"]))
        except argparse.ArgumentTypeError as e:
            sys.exit(f"config.json public_url: {e}")
    return None


def _urls(cfg, public: str | None) -> list[tuple[str, str]]:
    from .config import lan_ip

    urls = []
    if public:
        urls.append(("Public URL", public))
    if cfg.host in WILDCARD_HOSTS:
        lan = lan_ip()
        if lan:
            urls.append(("LAN (all interfaces)", f"http://{lan}:{cfg.port}"))
        urls.append(("This machine", f"http://localhost:{cfg.port}"))
    else:
        host = f"[{cfg.host}]" if ":" in cfg.host else cfg.host
        urls.append(("Listening on", f"http://{host}:{cfg.port}"))
    return urls


def _print_pairing(base_url: str, no_qr: bool, roots: list[str] | None = None,
                   manage_devices: bool = False, terminal: bool = False,
                   require_passkey: bool = True, passkeys_ok: bool = True,
                   quiet_exempt: bool = False, files: bool = False,
                   files_upload: bool = False) -> None:
    """Issue a one-time pairing code and print it with a link + QR. The code
    travels in the URL *fragment* — never in a query string."""
    from .auth import PAIRING_TTL_SECS, issue_pairing_code

    code = issue_pairing_code(roots=roots, manage_devices=manage_devices,
                              terminal=terminal, require_passkey=require_passkey,
                              quiet_exempt=quiet_exempt, files=files,
                              files_upload=files_upload)
    link = f"{base_url}/#login={code}"
    mins = int(PAIRING_TTL_SECS // 60)
    print(f"\n  Pairing code (valid {mins} min, single use): {code}")
    folders = ", ".join(roots) if roots else (
        "all allowed_roots" if roots is None else "none (grant with `devices set`)")
    print(f"  Folders: {folders}"
          f" · manage devices: {'yes' if manage_devices else 'no'}"
          f" · True View: {'yes' if terminal else 'no'}"
          f" · passkey: {'required' if require_passkey else 'optional'}")
    print(f"  Files: {'upload' if files_upload else 'view' if files else 'no'}"
          f" · quiet hours: {'exempt' if quiet_exempt else 'apply'}")
    if require_passkey and not passkeys_ok:
        print("  Warning: passkeys are off here (they need an https public URL or\n"
              "  localhost), so this device could never meet the requirement —\n"
              "  pair with --no-passkey, or set public_url.")
    print(f"  Pairing link:\n  {link}\n")
    if not no_qr:
        try:
            import qrcode
            qr = qrcode.QRCode(border=1)
            qr.add_data(link)
            qr.make(fit=True)
            qr.print_ascii(invert=True)
            print("  Scan with the phone camera to open + pair this device.\n")
        except Exception:
            pass


def _setup_logging(level: str) -> None:
    """The server's own log ("clicker4ai.*"), apart from uvicorn's."""
    log = logging.getLogger("clicker4ai")
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S"))
    log.addHandler(handler)
    log.setLevel(level.upper())
    log.propagate = False


def _serve(args: argparse.Namespace) -> None:
    import uvicorn

    from .config import Config

    cfg = Config.load()
    _setup_logging(cfg.log_level)
    if args.host:
        cfg.host = args.host
    if args.port:
        cfg.port = args.port
    public = _resolve_public(args, cfg)
    urls = _urls(cfg, public)

    print()
    print("  ┌─────────────────────────────────────────────┐")
    print("  │  Clicker4AI — remote control server         │")
    print("  └─────────────────────────────────────────────┘")
    for label, url in urls:
        print(f"  {label:28s} {url}")

    from . import main as main_mod
    from . import passkey
    main_mod.configure(public, cfg.port)
    if not args.no_pair:
        _print_pairing(urls[0][1], args.no_qr, passkeys_ok=passkey.available())
    print("  Pair devices with `c4ai pair`")

    if passkey.available():
        print(f"  Passkeys enabled for {passkey.rp_id()}\n")
    else:
        print("  Passkeys off: they need an https public URL "
              "(--public-url) or localhost\n")
    uvicorn.run(main_mod.app, host=cfg.host, port=cfg.port, log_level="warning")


def _checked_roots(cfg, raws: list[str]) -> list[str]:
    from .scope import ScopeError, validate_roots

    try:
        return validate_roots(cfg.allowed_roots, raws)
    except ScopeError as e:
        sys.exit(f"--root: {e}")


def _pair(args: argparse.Namespace) -> None:
    from . import passkey
    from .config import Config

    cfg = Config.load()
    if args.all_roots and args.root:
        sys.exit("--all-roots takes no --root")
    roots = _checked_roots(cfg, args.root or [])
    # Default: no folders. A device is paired first and given access
    # deliberately, the same way a passkey login starts empty.
    value: list[str] | None = None if args.all_roots else roots
    public = _resolve_public(args, cfg)
    passkey.configure(public, cfg.port)
    _print_pairing(_urls(cfg, public)[0][1], args.no_qr,
                   roots=value, manage_devices=args.manage_devices,
                   terminal=args.terminal, require_passkey=not args.no_passkey,
                   passkeys_ok=passkey.available(), quiet_exempt=args.quiet_exempt,
                   files=args.files, files_upload=args.files_upload)


def _fmt_ts(ts: float | None) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "-"


def _devices(args: argparse.Namespace) -> None:
    from .auth import DeviceStore
    from .config import Config

    cfg = Config.load()  # creates/secures the data dir
    devs = sorted(DeviceStore().list(), key=lambda d: d.get("last_seen") or 0,
                  reverse=True)
    print(f"allowed_roots: {', '.join(cfg.allowed_roots)}")
    print(f"quiet hours: {_quiet_label(cfg.extra.get('quiet_hours'), cfg.extra.get('quiet_tz'))}")
    if not devs:
        print("No signed-in devices. Pair one with `c4ai pair`.")
        return
    print(f"{'ID':10s} {'NAME':24s} {'PAIRED':17s} {'LAST SEEN':17s} "
          f"{'MANAGE':7s} {'TERM':5s} {'KEY':4s} {'REQ':4s} {'QUIET':6s} "
          f"{'FILES':6s} "
          f"{'STATE':7s} ROOTS")
    for d in devs:
        own = d.get("roots")
        roots = ", ".join(own) if own else ("(none)" if own == [] else "(all roots)")
        print(f"{d['id']:10s} {str(d['name'])[:24]:24s} "
              f"{_fmt_ts(d.get('created_at')):17s} {_fmt_ts(d.get('last_seen')):17s} "
              f"{'yes' if d.get('manage_devices') else 'no':7s} "
              f"{'yes' if d.get('terminal') else 'no':5s} "
              f"{'yes' if d.get('passkey') else 'no':4s} "
              f"{'yes' if d.get('require_passkey') else 'no':4s} "
              f"{'exempt' if d.get('quiet_exempt') else 'yes':6s} "
              f"{_files_label(d):6s} "
              f"{'locked' if d.get('locked') else 'active':7s} {roots}")


def _files_label(d: dict) -> str:
    return "upload" if d.get("files_upload") else "view" if d.get("files") else "no"


class _OnOff(argparse.BooleanOptionalAction):
    """--x / --no-x that refuses both (or either twice) in one command."""
    def __call__(self, parser, namespace, values, option_string=None):
        if getattr(namespace, self.dest) is not None:
            parser.error(f"{' / '.join(self.option_strings)}: give only one")
        super().__call__(parser, namespace, values, option_string)


def _set_grants(args: argparse.Namespace) -> None:
    from .auth import DeviceStore
    from .config import Config

    cfg = Config.load()
    picked = [f for f, on in (("--root", args.root), ("--add-root", args.add_root),
                              ("--all-roots", args.all_roots),
                              ("--no-roots", args.no_roots)) if on]
    if len(picked) > 1:
        sys.exit(f"{' and '.join(picked)} cannot be combined")
    changes: dict = {}
    if args.root:
        changes["roots"] = _checked_roots(cfg, args.root)
    elif args.all_roots:
        changes["roots"] = None
    elif args.no_roots:
        changes["roots"] = []
    elif args.add_root:
        changes["add_roots"] = _checked_roots(cfg, args.add_root)
    for key, val in (("manage", args.manage_devices), ("terminal", args.terminal),
                     ("require_passkey", args.passkey),
                     ("quiet_exempt", args.quiet_exempt)):
        if val is not None:
            changes[key] = val
    # upload needs viewing; no viewing means no upload either
    if args.files_upload and args.files is False:
        sys.exit("--files-upload and --no-files cannot be combined")
    if args.files_upload:
        changes["files"] = changes["files_upload"] = True
    elif args.files_upload is False:
        changes["files_upload"] = False
    if args.files is not None:
        changes["files"] = args.files
        if not args.files:
            changes["files_upload"] = False
    if not changes:
        sys.exit("Nothing to change: give at least one option (see --help)")
    store = DeviceStore()
    before = next((d for d in store.list() if d["id"] == args.device_id), {})
    dev = store.set_grants(args.device_id, **changes)
    if dev is None:
        sys.exit(f"No such device: {args.device_id}")
    if args.add_root and before.get("roots") is None:
        print("  It already has all allowed_roots; folders left as they were.")
    own = dev.get("roots")
    folders = ", ".join(own) if own else ("none" if own == [] else "all allowed_roots")
    print(f"Device {args.device_id} ({dev.get('name')}): folders = {folders}"
          f" · manage devices: {'yes' if dev.get('manage_devices') else 'no'}"
          f" · True View: {'yes' if dev.get('terminal') else 'no'}"
          f" · passkey: {'required' if dev.get('require_passkey') else 'optional'}"
          f" · quiet hours: {'exempt' if dev.get('quiet_exempt') else 'apply'}"
          f" · files: {_files_label(dev)}")
    if dev.get("require_passkey") and not dev.get("passkey"):
        print("  It has no passkey yet: the app offers nothing but adding one.")


def _passkeys(args: argparse.Namespace) -> None:
    from .config import Config
    from .passkey import list_public

    Config.load()
    keys = sorted(list_public(), key=lambda k: k.get("created_at") or 0, reverse=True)
    if not keys:
        print("No passkeys. Register one in the app: menu → Devices → Add passkey.")
        return
    print(f"{'ID':24s} {'NAME':24s} {'CREATED':17s} {'LAST USED':17s} "
          f"{'SYNCED':7s} USED BY")
    for k in keys:
        print(f"{str(k['id'])[:22]:24s} {str(k['name'])[:24]:24s} "
              f"{_fmt_ts(k.get('created_at')):17s} {_fmt_ts(k.get('last_used')):17s} "
              f"{'yes' if k.get('backed_up') else 'no':7s} "
              f"{', '.join(k.get('used_by') or []) or '-'}")
    print("\nIDs are shortened for display; `passkeys remove` accepts a prefix.")


def _passkeys_remove(args: argparse.Namespace) -> None:
    from .auth import DeviceStore
    from .config import Config
    from .passkey import devices_with_keys, list_public, remove

    Config.load()
    hits = [k for k in list_public() if str(k["id"]).startswith(args.passkey_id)]
    if not hits:
        sys.exit(f"No such passkey: {args.passkey_id}")
    if len(hits) > 1:
        sys.exit(f"{args.passkey_id} matches {len(hits)} passkeys — use a longer prefix")
    remove(hits[0]["id"])
    # devices left without a key stop asking for it (step-up, unlock)
    DeviceStore().clear_passkey(hits[0]["used_by"], devices_with_keys())
    print(f"Removed passkey {hits[0]['name']} ({hits[0]['id'][:22]}).")


def _passkeys_rename(args: argparse.Namespace) -> None:
    from .config import Config
    from .passkey import PasskeyError, list_public, rename

    Config.load()
    hits = [k for k in list_public() if str(k["id"]).startswith(args.passkey_id)]
    if not hits:
        sys.exit(f"No such passkey: {args.passkey_id}")
    if len(hits) > 1:
        sys.exit(f"{args.passkey_id} matches {len(hits)} passkeys — use a longer prefix")
    try:
        name = rename(hits[0]["id"], " ".join(args.name))
    except PasskeyError as e:
        sys.exit(str(e))
    print(f"Passkey {hits[0]['id'][:22]}: name = {name}")


def _trust_path(raw: str) -> str:
    return str(Path(raw).expanduser().resolve())


def _trust(args: argparse.Namespace) -> None:
    from . import trust

    rows = trust.listing()
    if not rows:
        print("No trusted folders.")
        return
    print(f"{'WHERE':14s} FOLDER")
    for r in rows:
        print(f"{' + '.join(r['where']):14s} {r['path']}")
    print("\nOnly the exact folder counts, never its subfolders. `trust remove`\n"
          "takes back the app's trust; the terminal's lives in ~/.claude.json.")


def _trust_add(args: argparse.Namespace) -> None:
    from . import trust

    cwd = _trust_path(args.folder)
    if not Path(cwd).is_dir():
        sys.exit(f"Not a folder: {cwd}")
    trust.add(cwd)
    print(f"Trusted: {cwd}")


def _trust_remove(args: argparse.Namespace) -> None:
    from . import trust

    cwd = _trust_path(args.folder)
    removed = trust.remove(cwd)
    print(f"No longer trusted from the app: {cwd}" if removed
          else f"Not trusted from the app: {cwd}")
    if trust.is_trusted(cwd):
        sys.exit("It is still trusted in the terminal: set \"hasTrustDialogAccepted\"\n"
                 "to false under its path in ~/.claude.json while no claude runs.")
    if not removed:
        sys.exit(1)


def _rename(args: argparse.Namespace) -> None:
    from .auth import DeviceStore, _clean_name
    from .config import Config

    Config.load()
    store = DeviceStore()
    name = " ".join(args.name)
    if store.name_taken(name, except_id=args.device_id):
        sys.exit(f"{name!r} is already another device's name or id.")
    if not store.set_name(args.device_id, name):
        sys.exit(f"No such device (or empty name): {args.device_id}")
    print(f"Device {args.device_id}: name = {_clean_name(name)}")   # as stored


def _set_lock(args: argparse.Namespace) -> None:
    from .auth import DeviceStore
    from .config import Config

    Config.load()
    on = args.devices_command == "lock"
    if not DeviceStore().set_locked(args.device_id, on):
        sys.exit(f"No such device: {args.device_id}")
    print(f"Device {args.device_id}: {'locked' if on else 'active'}"
          + (" — its passkey (or `devices unlock`) wakes it" if on else ""))


def _revoke(args: argparse.Namespace) -> None:
    from .auth import DeviceStore
    from .config import Config
    from .passkey import drop_device

    Config.load()
    if DeviceStore().revoke(args.device_id):
        drop_device(args.device_id)   # unlist it; the credential stays
        print(f"Revoked device {args.device_id}.")
    else:
        sys.exit(f"No such device: {args.device_id}")


def _quiet_label(raw, tz=None) -> str:
    from .quiet import fmt, parse_window, parse_zone

    if not raw:
        return "off"
    try:
        start, end = parse_window(raw)
    except ValueError as e:
        return f"{raw!r} is not valid ({e}), so off"
    where = "host time"
    if tz:
        try:
            parse_zone(tz)
            where = tz
        except ValueError:
            where = f"host time — {tz!r} is not a known zone"
    return f"{fmt(start)}-{fmt(end)} ({where})"


def _quiet_hours(args: argparse.Namespace) -> None:
    """Show or set config.json "quiet_hours" and "quiet_tz". Only those keys
    change: the file is the user's own, so nothing else is rewritten or
    added. "off" keeps the zone for the next window."""
    import json

    from .config import CONFIG_PATH, Config, write_private
    from .quiet import fmt, parse_window, parse_zone

    cfg = Config.load()
    if args.window is None and args.tz is None:
        label = _quiet_label(cfg.extra.get("quiet_hours"), cfg.extra.get("quiet_tz"))
        print(f"Quiet hours: {label}")
        return
    raw = json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}
    if args.tz is not None:
        if args.tz.lower() == "host":
            raw.pop("quiet_tz", None)
        else:
            try:
                parse_zone(args.tz)
            except ValueError as e:
                sys.exit(str(e))
            raw["quiet_tz"] = args.tz
    if args.window is None:
        pass
    elif args.window.lower() == "off":
        raw.pop("quiet_hours", None)
    else:
        try:
            start, end = parse_window(args.window)
        except ValueError as e:
            sys.exit(str(e))
        raw["quiet_hours"] = f"{fmt(start)}-{fmt(end)}"
    write_private(CONFIG_PATH, raw)
    print(f"Quiet hours: {_quiet_label(raw.get('quiet_hours'), raw.get('quiet_tz'))}"
          " — applies at once, no restart needed")


def _cfg_host(raw) -> str:
    from .config import _parse_host
    if raw is None:
        raise ValueError("host must be an IP address or a host name")
    return _parse_host(raw)


def _cfg_port(raw) -> int:
    from .config import _parse_port
    if raw is None:
        raise ValueError("port must be a number")
    return _parse_port(raw)


def _cfg_public_url(raw) -> str | None:
    if raw is None or raw == "":
        return None
    try:
        return _public_url(str(raw))
    except argparse.ArgumentTypeError as e:
        raise ValueError(f"public_url: {e}")


@functools.cache
def _cfg_keys() -> dict:
    """`config set` keys: name -> (check and normalise, default). Each check
    is the one the server applies, so a bad value never reaches the file.
    Built once, on first use; callers only read it."""
    from .config import (DEFAULT_HOST, DEFAULT_LOCK_IDLE_MINUTES,
                         DEFAULT_LOG_LEVEL, DEFAULT_MAX_RUNNERS, DEFAULT_PORT,
                         _parse_lock_idle, _parse_log_level, _parse_max_runners)
    return {
        "host": (_cfg_host, DEFAULT_HOST),
        "port": (_cfg_port, DEFAULT_PORT),
        "public_url": (_cfg_public_url, None),
        "max_runners": (_parse_max_runners, DEFAULT_MAX_RUNNERS),
        "lock_idle_minutes": (_parse_lock_idle, DEFAULT_LOCK_IDLE_MINUTES),
        "log_level": (_parse_log_level, DEFAULT_LOG_LEVEL),
    }


_CFG_ELSEWHERE = {
    "allowed_roots": "`c4ai config roots add|remove`",
    "quiet_hours": "`c4ai quiet-hours`",
    "quiet_tz": "`c4ai quiet-hours --tz`",
}
_CFG_RESTART = "Takes effect when the server restarts."


def _cfg_raw() -> dict:
    import json

    from .config import CONFIG_PATH
    if not CONFIG_PATH.exists():
        return {}
    try:
        raw = json.loads(CONFIG_PATH.read_text())
    except ValueError as e:
        sys.exit(f"{CONFIG_PATH} is not valid JSON ({e}); fix it by hand")
    if not isinstance(raw, dict):
        sys.exit(f"{CONFIG_PATH} must hold a JSON object; fix it by hand")
    return raw


def _cfg_key(name: str) -> str:
    key = name.replace("-", "_")
    if key in _CFG_ELSEWHERE:
        sys.exit(f"{key} is set with {_CFG_ELSEWHERE[key]}")
    if key not in _cfg_keys():
        sys.exit(f"Unknown key {name!r}; one of: {', '.join(_cfg_keys())}")
    return key


def _config(args: argparse.Namespace) -> None:
    """Show config.json: every key, its value and whether it is the default.
    The file is read as it is, so a broken value shows up here instead of
    stopping the command."""
    from .config import CONFIG_PATH, _parse_roots

    raw = _cfg_raw()
    print(f"{CONFIG_PATH}{'' if CONFIG_PATH.exists() else ' (not there yet)'}")
    roots = raw.get("allowed_roots")
    try:
        _parse_roots(roots)
        label = ", ".join(roots)
    except ValueError as e:
        label = f"{roots!r} — invalid: {str(e).removeprefix('config.json: ')}" if roots is not None else "not set (required)"
    print(f"  {'allowed_roots':18s} {label}")
    for key, (check, default) in _cfg_keys().items():
        if key not in raw:
            label = f"{default if default is not None else '-'} (default)"
        else:
            try:
                value = check(raw[key])
                label = "-" if value is None else str(value)
            except ValueError as e:
                label = f"{raw[key]!r} — invalid: {str(e).removeprefix('config.json: ')}"
        print(f"  {key:18s} {label}")
    print(f"  {'quiet_hours':18s} "
          f"{_quiet_label(raw.get('quiet_hours'), raw.get('quiet_tz'))}")
    known = {"allowed_roots", "quiet_hours", "quiet_tz", *_cfg_keys()}
    other = [k for k in raw if k not in known]
    if other:
        print(f"  other keys (unused): {', '.join(other)}")
    print("Changes take effect when the server restarts; quiet hours at once.")


def _config_set(args: argparse.Namespace) -> None:
    from .config import CONFIG_PATH, write_private

    key = _cfg_key(args.key)
    check, _ = _cfg_keys()[key]
    try:
        value = check(args.value)
    except ValueError as e:
        sys.exit(str(e).removeprefix("config.json: "))
    raw = _cfg_raw()
    if value is None:
        raw.pop(key, None)
    else:
        raw[key] = value
    write_private(CONFIG_PATH, raw)
    print(f"{key}: {'-' if value is None else value}. {_CFG_RESTART}")


def _config_unset(args: argparse.Namespace) -> None:
    from .config import CONFIG_PATH, write_private

    key = _cfg_key(args.key)
    raw = _cfg_raw()
    if key not in raw:
        print(f"{key} is not in config.json; the default applies.")
        return
    del raw[key]
    write_private(CONFIG_PATH, raw)
    default = _cfg_keys()[key][1]
    print(f"{key}: back to the default ({'-' if default is None else default}). "
          f"{_CFG_RESTART}")


def _cfg_roots(raw: dict) -> list[str]:
    roots = raw.get("allowed_roots", [])
    if not isinstance(roots, list) or not all(isinstance(r, str) for r in roots):
        sys.exit("allowed_roots in config.json is not a list of paths; fix it by hand")
    return roots


def _config_roots_add(args: argparse.Namespace) -> None:
    """Add a folder to allowed_roots, the ceiling of every device's grants.
    Devices that reach more because of it are named: those granted "all
    roots", and those whose own grants lie under it (cut off earlier by
    `roots remove`)."""
    from .auth import DeviceStore
    from .config import CONFIG_PATH, _parse_roots, write_private

    raw = _cfg_raw()
    roots = _cfg_roots(raw)
    new = Path(args.folder).expanduser().resolve()
    if not new.is_dir():
        sys.exit(f"Not a folder: {new}")
    ceiling = [Path(r).expanduser().resolve() for r in roots]
    for r, c in zip(roots, ceiling):
        if new.is_relative_to(c):
            sys.exit(f"{new} is already inside allowed root {r}")
    home = Path.home().resolve()
    entry = f"~/{new.relative_to(home)}" if new.is_relative_to(home) else str(new)
    try:
        _parse_roots(roots + [entry])
    except ValueError as e:
        sys.exit(str(e).removeprefix("config.json: "))
    for r, c in zip(roots, ceiling):
        if c.is_relative_to(new):
            # Overlapping roots would make `roots remove` report losses
            # that the wider root still covers.
            sys.exit(f"{new} contains allowed root {r}; "
                     f"remove {r} first, then add {new}")
    raw["allowed_roots"] = roots + [entry]
    write_private(CONFIG_PATH, raw)
    print(f"Added {entry} to allowed_roots. {_CFG_RESTART}")
    for d in DeviceStore().list():
        own = d.get("roots")
        if own is None:
            print(f"  {d['name']} gets {entry} (all folders)")
            continue
        back = [r for r in own
                if Path(r).expanduser().resolve().is_relative_to(new)]
        if back:
            print(f"  {d['name']} gets back {', '.join(back)}")


def _config_roots_remove(args: argparse.Namespace) -> None:
    """Take a folder out of allowed_roots. Device grants under it stay in
    devices.json but reach nothing (scope.device_scope cuts every grant to
    the ceiling), and come back if the folder is added again."""
    from .auth import DeviceStore
    from .config import CONFIG_PATH, write_private

    raw = _cfg_raw()
    roots = _cfg_roots(raw)
    gone = Path(args.folder).expanduser().resolve()
    match = [r for r in roots if Path(r).expanduser().resolve() == gone]
    if not match:
        sys.exit(f"{gone} is not in allowed_roots ({', '.join(roots) or 'empty'})")
    left = [r for r in roots if r not in match]
    if not left:
        sys.exit("allowed_roots needs at least one folder; add the new one first")
    raw["allowed_roots"] = left
    write_private(CONFIG_PATH, raw)
    print(f"Removed {match[0]} from allowed_roots. {_CFG_RESTART}")
    ceiling = [Path(r).expanduser().resolve() for r in left]
    for d in DeviceStore().list():
        own = d.get("roots")
        if own is None:
            lost = [match[0]]
        else:
            lost = [r for r in own
                    if Path(r).expanduser().resolve().is_relative_to(gone)
                    and not any(Path(r).expanduser().resolve().is_relative_to(c)
                                for c in ceiling)]
        if lost:
            print(f"  {d['name']} loses {', '.join(lost)}")


def _sdk_status(args: argparse.Namespace) -> dict:
    from . import sdk_update as su

    latest = ""
    try:
        latest = su.pypi_latest()
    except Exception as e:
        print(f"  PyPI check failed: {e}")
    cli_latest = ""
    try:
        cli_latest = su.npm_latest_cli()
    except Exception as e:
        print(f"  npm check failed: {e}")
    inst, cli, bundled = su.installed(), su.system_cli(), su.bundled_cli()
    print(f"  claude CLI (runners):   {cli or 'not found'}")
    print(f"  newest CLI ({su.CLI_CHANNEL}):    {cli_latest or '?'}")
    print(f"  SDK installed:          {inst or 'none'}"
          f"  (released with CLI {bundled or '?'})")
    print(f"  newest SDK {su.SDK_RANGE}: {latest or '?'}")
    if cli and bundled and cli != bundled:
        print(f"  The CLI is {cli}, the SDK was tested with {bundled}.")
    return {"latest": latest, "installed": inst, "cli": cli, "cli_latest": cli_latest}


def _sdk(args: argparse.Namespace) -> None:
    from . import sdk_update as su

    st = _sdk_status(args)
    if st["latest"] and su.newer(st["latest"], st["installed"]):
        print(f"\n  Update available: `c4ai sdk update` "
              f"({st['installed']} → {st['latest']})")
    elif st["latest"]:
        print("\n  The SDK is up to date.")
    if st["cli"] and st["cli_latest"] and su.newer(st["cli_latest"], st["cli"]):
        print(f"  CLI update available: `c4ai sdk update-cli` "
              f"({st['cli']} → {st['cli_latest']})")
    elif st["cli"] and st["cli_latest"]:
        print("  The CLI is up to date.")


def _sdk_update_cli(args: argparse.Namespace) -> None:
    from . import sdk_update as su

    st = _sdk_status(args)
    if not (st["cli"] and st["cli_latest"] and su.newer(st["cli_latest"], st["cli"])):
        print("\n  Nothing to update.")
        return
    if not args.yes:
        ans = input(f"\n  Run `claude update` ({st['cli']} → {st['cli_latest']})? [y/N] ")
        if ans.strip().lower() not in ("y", "yes"):
            sys.exit("  Cancelled.")
    print("  Updating…")
    res = su.update_cli()
    print(f"  {res['message']}")
    if not res["ok"]:
        sys.exit(1)


def _sdk_update(args: argparse.Namespace) -> None:
    from . import sdk_update as su

    st = _sdk_status(args)
    target = st["latest"]
    if not target or not su.newer(target, st["installed"]):
        print("\n  Nothing to update.")
        return
    if not su.can_install():
        sys.exit(f"\n  No pip in this environment; run:\n  {su.hint_command(target)}")
    if not args.yes:
        ans = input(f"\n  Install claude-agent-sdk {target} into {sys.prefix}? [y/N] ")
        if ans.strip().lower() not in ("y", "yes"):
            sys.exit("  Cancelled.")
    print("  Installing…")
    res = su.install(target)
    print(f"  {res['message']}")
    if not res["ok"]:
        sys.exit(1)
    print("  Restart the server to use it (pm2: `pm2 restart clicker4ai`);\n"
          "  that stops every running session.")


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    commands = ("serve", "pair", "devices", "revoke", "passkeys", "trust", "quiet-hours",
                "config", "sdk")
    # `serve` is the default: bare flags (or nothing) mean serve.
    if not argv or (argv[0] not in commands and argv[0] not in ("-h", "--help")):
        argv.insert(0, "serve")
    # A passkey id is base64url, so about one in 64 starts with "-", and a
    # passkey or device name may too ("-old"); argparse would take either for
    # an option. These subcommands take no options, so everything after them
    # is data.
    for cmd in (["passkeys", "remove"], ["passkeys", "rename"], ["devices", "rename"],
                ["devices", "lock"], ["devices", "unlock"], ["revoke"]):
        n = len(cmd)
        if argv[:n] == cmd and not {"-h", "--help", "--"} & set(argv[n:]):
            argv.insert(n, "--")
            break

    parser = argparse.ArgumentParser(
        prog="c4ai",
        description="Mobile PWA remote control for Claude Code",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    public_help = ("URL phones use to reach the server (e.g. "
                   "https://rc.example.com behind a reverse proxy); "
                   "used for pairing links and QR codes")

    p = sub.add_parser("serve", help="run the server (default)")
    p.add_argument("--host", default=None,
                   help="IP address to bind (default: 127.0.0.1; "
                        "0.0.0.0 = all interfaces)")
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--public-url", type=_public_url, default=None, help=public_help)
    p.add_argument("--no-qr", action="store_true")
    p.add_argument("--no-pair", action="store_true",
                   help="don't issue a pairing code at start (e.g. under pm2, "
                        "whose logs would keep it); use `pair` instead")
    p.set_defaults(func=_serve)

    p = sub.add_parser("pair", help="issue a one-time pairing code (link + QR)")
    p.add_argument("--root", action="append", metavar="DIR",
                   help="give the device this folder (repeatable; must be "
                        "inside allowed_roots; default: none at all)")
    p.add_argument("--all-roots", action="store_true",
                   help="give the device every folder in allowed_roots")
    p.add_argument("--manage-devices", action="store_true",
                   help="let the device list and sign out other devices in the app")
    p.add_argument("--terminal", action="store_true",
                   help="allow True View (full Claude Code TUI; `!cmd` runs shell "
                        "commands directly, regardless of --root)")
    p.add_argument("--no-passkey", action="store_true",
                   help="do not require the device to register a passkey first "
                        "(e.g. a desktop without Touch ID)")
    p.add_argument("--quiet-exempt", action="store_true",
                   help="may start turns during quiet hours")
    p.add_argument("--files", action="store_true",
                   help="may list, view and download files under its folders")
    p.add_argument("--files-upload", action="store_true",
                   help="may also upload files (implies --files)")
    p.add_argument("--public-url", type=_public_url, default=None, help=public_help)
    p.add_argument("--no-qr", action="store_true")
    p.set_defaults(func=_pair)

    p = sub.add_parser("devices", help="list signed-in devices (or change grants)")
    p.set_defaults(func=_devices)
    dsub = p.add_subparsers(dest="devices_command")
    q = dsub.add_parser("set", help="change a device's grants (the flags of `pair`; "
                                    "what you leave out stays as it is)")
    q.add_argument("device_id", metavar="DEVICE")
    q.add_argument("--root", action="append", metavar="DIR",
                   help="only these folders (repeatable; replaces the current ones)")
    q.add_argument("--add-root", action="append", metavar="DIR",
                   help="add this folder to the current ones (repeatable)")
    q.add_argument("--all-roots", action="store_true",
                   help="every folder in allowed_roots")
    q.add_argument("--no-roots", action="store_true",
                   help="no folders at all (what a passkey login starts with)")
    q.add_argument("--manage-devices", action=_OnOff,
                   default=None, help="may list and sign out other devices in the app")
    q.add_argument("--terminal", action=_OnOff, default=None,
                   help="may open True View")
    q.add_argument("--passkey", action=_OnOff, default=None,
                   help="must carry a passkey (the app can only switch this on)")
    q.add_argument("--quiet-exempt", action=_OnOff, default=None,
                   help="may start turns during quiet hours")
    q.add_argument("--files", action=_OnOff, default=None,
                   help="may list, view and download files under its folders")
    q.add_argument("--files-upload", action=_OnOff, default=None,
                   help="may also upload files (implies --files)")
    q.set_defaults(func=_set_grants)
    for name, helptext in (("lock", "refuse its requests until a passkey wakes it"),
                           ("unlock", "wake a locked device")):
        q = dsub.add_parser(name, help=helptext)
        q.add_argument("device_id", metavar="DEVICE")
        q.set_defaults(func=_set_lock)
    q = dsub.add_parser("rename", help="give a device a different name "
                                       "(must be unique)")
    q.add_argument("device_id", metavar="DEVICE")
    q.add_argument("name", nargs="+", metavar="NAME")
    q.set_defaults(func=_rename)

    p = sub.add_parser("passkeys", help="list registered passkeys")
    p.set_defaults(func=_passkeys)
    ksub = p.add_subparsers(dest="passkeys_command")
    q = ksub.add_parser("remove", help="delete a passkey (id or a prefix of it)")
    q.add_argument("passkey_id")
    q.set_defaults(func=_passkeys_remove)
    q = ksub.add_parser("rename", help="give a passkey a different name "
                                       "(the label here and in the app)")
    q.add_argument("passkey_id")
    q.add_argument("name", nargs="+", metavar="NAME")
    q.set_defaults(func=_passkeys_rename)

    p = sub.add_parser("trust", help="list folders trusted to run their project "
                                     "settings (hooks, allow rules, commands)")
    p.set_defaults(func=_trust)
    tsub = p.add_subparsers(dest="trust_command")
    q = tsub.add_parser("add", help="trust a folder, as \"Trust and continue\" does")
    q.add_argument("folder")
    q.set_defaults(func=_trust_add)
    q = tsub.add_parser("remove", help="take back the app's trust in a folder")
    q.add_argument("folder")
    q.set_defaults(func=_trust_remove)

    p = sub.add_parser("quiet-hours", help="show or set the nightly window with "
                                           "no new prompts")
    p.add_argument("window", nargs="?", metavar="HH:MM-HH:MM|off",
                   help="e.g. 00:30-08:00; off removes it")
    p.add_argument("--tz", metavar="ZONE|host",
                   help="time zone of the window, e.g. Europe/London; "
                        "host (the default) = the host's local time")
    p.set_defaults(func=_quiet_hours)

    p = sub.add_parser("config", help="show config.json (or change one key)")
    p.set_defaults(func=_config)
    csub = p.add_subparsers(dest="config_command")
    q = csub.add_parser("set", help="set a key, checked as the server checks it")
    q.add_argument("key", help="host, port, public_url, max_runners, "
                               "lock_idle_minutes or log_level")
    q.add_argument("value")
    q.set_defaults(func=_config_set)
    q = csub.add_parser("unset", help="remove a key, so its default applies")
    q.add_argument("key")
    q.set_defaults(func=_config_unset)
    q = csub.add_parser("roots", help="change allowed_roots, the ceiling of "
                                      "every device's folders")
    rsub = q.add_subparsers(dest="roots_command", required=True)
    r = rsub.add_parser("add", help="add a folder (not your home or above)")
    r.add_argument("folder")
    r.set_defaults(func=_config_roots_add)
    r = rsub.add_parser("remove", help="remove a folder; lists the devices "
                                       "that lose it")
    r.add_argument("folder")
    r.set_defaults(func=_config_roots_remove)

    p = sub.add_parser("sdk", help="compare the Agent SDK with the claude CLI")
    p.set_defaults(func=_sdk)
    ssub = p.add_subparsers(dest="sdk_command")
    q = ssub.add_parser("update", help="install the newest SDK in the supported range")
    q.add_argument("--yes", action="store_true", help="do not ask")
    q.set_defaults(func=_sdk_update)
    q = ssub.add_parser("update-cli", help="run `claude update` when npm has a newer CLI")
    q.add_argument("--yes", action="store_true", help="do not ask")
    q.set_defaults(func=_sdk_update_cli)

    p = sub.add_parser("revoke", help="sign out a device")
    p.add_argument("device_id", metavar="DEVICE")
    p.set_defaults(func=_revoke)

    args = parser.parse_args(argv)
    if getattr(args, "device_id", None):
        from .auth import DeviceStore
        ref = args.device_id
        args.device_id = DeviceStore().resolve(ref)
        if args.device_id is None:
            sys.exit(f"No such device: {ref}")
    args.func(args)


if __name__ == "__main__":
    main()
