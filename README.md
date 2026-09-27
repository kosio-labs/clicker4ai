# Clicker4AI

*Formerly “[Better Claude RC](#license)” (`better-claude-rc`), then briefly
Clicker4Claude — until someone read the trademark guidelines. The “AI” is doing
a lot of legal work here.*

A self-hosted PWA that turns your phone, tablet or laptop into a proper remote
control for [Claude Code](https://claude.com/claude-code) sessions running on
your Linux server.

Start, resume, and drive real Claude Code sessions from any device with a
browser — with a structured event stream (tool cards, diffs, thinking),
tap-to-approve permissions, and live two-way sync with the terminal on the
server. It's a small FastAPI +
[claude-agent-sdk](https://github.com/anthropics/claude-agent-sdk-python)
backend and a dependency-free vanilla-JS frontend. **No build step, no cloud, no
account** — the server runs on your machine and your devices talk to it over
your own network. Several devices can watch and drive the same session; switch
from the phone to the laptop mid-task without stopping anything.

> Built for driving Claude Code away from the keyboard: new sessions from any
> device, structured (not terminal-scraped) output, and a UI that works on a
> phone screen first and scales up to a tablet or desktop browser.

<p align="center">
  <img src="https://raw.githubusercontent.com/kosio-labs/clicker4ai/main/assets/screenshots/demo.gif" width="300" alt="A session waiting for approval: Allow, the tests run, then Tabs">
</p>
<p align="center">
  <img src="https://raw.githubusercontent.com/kosio-labs/clicker4ai/main/assets/screenshots/home.png" width="150" alt="Home: last sessions and projects">
  <img src="https://raw.githubusercontent.com/kosio-labs/clicker4ai/main/assets/screenshots/projects.png" width="150" alt="Projects">
  <img src="https://raw.githubusercontent.com/kosio-labs/clicker4ai/main/assets/screenshots/sessions.png" width="150" alt="Sessions">
  <img src="https://raw.githubusercontent.com/kosio-labs/clicker4ai/main/assets/screenshots/chat.png" width="150" alt="Chat with a diff and an approval card">
  <img src="https://raw.githubusercontent.com/kosio-labs/clicker4ai/main/assets/screenshots/tabs.png" width="150" alt="Tabs">
</p>

*An independent open-source project, not affiliated with Anthropic (see
[Authentication and trademarks](#authentication-and-trademarks)).*

## Features

- **Start & pick projects from any device** — recent projects from `~/.claude`
  plus a directory browser; "New session" spawns a real Claude Code session in
  any working directory, with model and permission-mode choice.
- **Resume & fork with terminal parity** — open any past session and see its
  full history backfilled from the transcript; resume keeps the *same* Claude
  session id (or fork into a new one) and appends to the same transcript file
  the terminal reads.
- **Structured event stream** — text streamed as deltas, tool-call cards (Bash
  output, Edit diffs, TodoWrite checklists), thinking blocks, compact
  boundaries, and cost/context meters. Not terminal scraping — real structured
  messages from the SDK.
- **Tap-to-approve permissions** — remote permission prompts park on the server
  and surface in the app as Allow / Deny (with "always allow" suggestions),
  plan-mode approval, and AskUserQuestion option buttons.
- **Live terminal ↔ webapp sync** — turns typed in the terminal `claude` appear
  in the webapp in real time (transcript tailer); the app's next message
  silently rotates through resume so both sides share full context on one
  session id.
- **Multi-session tabs** — browser-style open-session switcher; jump between
  live sessions, see per-session status, stop a process or rename a session
  without opening it, close individually or all at once (closing a tab asks
  first and leaves the session running).
- **Plan usage always in sight** — the rate-limit reading the SDK returns with
  every request: "21% (5h) · 82% (7d)" at the end of the chat's status line,
  and per window with its reset time and a bar in session info. The chat
  only hears of a refusal, naming the limit that ran out and when it resets.
- **Reconnect with replay** — events are seq-numbered and persisted per session,
  so backgrounding the app on a phone and coming back replays exactly what
  was missed.
- **Incognito chat** — one per device, from Home: a discussion that leaves
  nothing behind on the server. Web search and fetch only (no files, shell,
  MCP or True View), seen only by the device that started it, and erased
  with its transcript when you end it, sign out, or after 24 h without a
  message.
- **Library screens** — browse Skills, Slash commands, Agents, and MCP servers
  (global + per-project) read from `~/.claude/*` and `~/.claude.json` /
  `.mcp.json`, with live MCP status from the session init message.
- **Project files** — browse a project folder without Claude and without
  tokens: text and images open in a viewer, any file downloads, and ↗ on a
  Read/Edit/Write card opens that file. Upload (one file at a time, up to
  50 MB, never overwriting) is a separate per-device grant; both are off by
  default.
- **Physical keyboard** — ↑/↓ (or j/k) and ⏎ through lists, menus and
  sheets, `/` to search, `g h/s/p/t` to jump, `1`–`4` / `y` / `n` on
  approval cards and digits on questions and plans; `?` lists everything.
  With a mouse or trackpad (also an iPad's trackpad keyboard) ⏎ sends the
  message and ⇧⏎ starts a new line, and key hints show; on a phone ⏎ is a
  new line and the ↑ button sends.
- **Installable PWA** — add to the home screen of a phone or tablet for a
  full-screen, app-like experience with icons and manifest. An open page
  notices when the frontend on the server has changed and offers a one-tap
  reload, so a restart is not silently invisible. Tested on iOS/iPadOS
  Safari and desktop browsers; Android uses the same web APIs but is
  untested.

## Works well with

**Tablet + code-server.** Run
[code-server](https://github.com/coder/code-server) (VS Code in the browser) on
the same machine and a tablet becomes a complete workstation. Claude works in
Clicker4AI while the editor shows each change the moment it lands — browse the
tree, read the whole diff, commit, open a terminal. Nothing lives on the tablet:
put it down, pick up the laptop or the phone, and the session and the editor are
exactly where you left them.

**Or skip the editor.** If you'd rather vibe-code, Clicker4AI alone will do:
say what you want, and Claude writes the code, runs the tests and, for a
website or web app, starts a preview you simply check in your browser. You
judge the result, not the diff — and when you do want to see a file, Project
files opens it right in the app.

**One small server is enough.** A modest VPS runs it all: Clicker4AI and your
projects with their previews (plus code-server, if you want an editor). Put it
behind a VPN and that one box becomes your whole development and test
environment, reachable from any device you own.

*Fair warning: this is dangerously addictive. You'll catch yourself shipping
features from the bus stop, the sofa and the dentist's waiting room. Your
posture, your sleep and your loved ones may file complaints — take breaks.
We saw it coming: `c4ai quiet-hours 00:30-08:00` lets the last turn finish
and then says goodnight, and only the server's CLI can talk it out of that.*

## Quickstart

Requires Python ≥ 3.13 and [pipx](https://pipx.pypa.io) (or `uv tool
install` in its place). If pipx's default Python is older, add `--python
python3.13`.

```bash
pipx install clicker4ai==1.0.0rc1     # a release candidate needs its version
c4ai config roots add ~/work          # the folders devices may be given
c4ai serve                            # start the server
```

To run from a clone instead, see [Development](#development).

`c4ai config roots add` writes `allowed_roots`, the one required setting: the
ceiling of what any device may reach, never your home directory or anything
above it (see [Configuration](#configuration)). The server keeps its data
(config, devices, passkeys, session logs, incognito folders) in
`~/.clicker4ai`; `C4AI_DATA_DIR` moves it.

Restarting, upgrading or reinstalling keeps all of this: `c4ai serve` picks up
where it left off. To wipe it, see
[Uninstall and starting over](#uninstall-and-starting-over).

On start, the server prints its URL, a **one-time pairing code** (valid
2 minutes, single use), a **pairing link** and a **QR code**. Scan the QR with
your phone's camera (or open the link, or type the code) to launch the PWA and
pair the device in one step. A new device reaches no folders until you give it
some — see [Devices and grants](#devices-and-grants). The server listens on
**127.0.0.1** only; to reach it from other devices, see
[Running the server](#running-the-server).

## Running the server

`c4ai serve` with no options starts with what `~/.clicker4ai/config.json`
says, and with the defaults for anything it leaves out (`127.0.0.1`, port
`8780`, no public URL). Options on the command line apply to that one run
only and are never written anywhere; to keep a setting, store it with
`c4ai config set` (see [Configuration](#configuration)).

To reach it from other devices, give it the IP address of the interface it
should listen on (e.g. a LAN or VPN address), or put it behind your own
reverse proxy / SSH tunnel.

```bash
c4ai serve --host 192.168.1.10   # listen on this IP
c4ai serve --host 0.0.0.0        # all interfaces (see Security)
c4ai serve --port 9000           # custom port
c4ai serve --no-qr               # skip the QR code
c4ai serve --no-pair             # no pairing code at start (use `pair`)
c4ai serve --public-url https://rc.example.com   # behind a reverse proxy
c4ai config set host 192.168.1.10  # the same, kept for every start
```

To keep it running, start `c4ai serve --no-pair` under a supervisor of your
choice (a systemd user unit, pm2) and pair devices with `c4ai pair`.

`--public-url` is the address your devices use (e.g. a TLS reverse proxy such
as Caddy in front of `127.0.0.1:8780`); the one-tap login link and QR point
there. Scheme + host[:port] only, no path.

Claude Code processes started by the server get
`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` (no telemetry, error reporting
or auto-updater traffic) unless the variable is already set in the
server's environment.

Keeping the Agent SDK and the `claude` CLI up to date:

```bash
c4ai sdk                            # Agent SDK vs the claude CLI, newest on PyPI
c4ai sdk update                     # install it (restart the server afterwards)
c4ai sdk update-cli                 # `claude update` when npm has a newer CLI
```

## Configuration

`~/.clicker4ai/config.json` holds the server's settings — never devices or
passkeys, which live in files of their own. Only `allowed_roots` is required;
[`config.example.json`](https://github.com/kosio-labs/clicker4ai/blob/main/config.example.json)
lists every key with its default (`null` = off). JSON has no comments, so here
is what each one does:


- `allowed_roots` — the folders devices may be given (required; see
  below).
- `host`, `port` — where the server listens (`127.0.0.1`, `8780`); `--host`
  and `--port` on `c4ai serve` win for that run only.
- `public_url` — the https address phones use behind a reverse proxy;
  `--public-url` wins the same way. Passkeys need it (or `localhost`).
- `max_runners` — how many sessions may hold a live `claude` process (6;
  see below).
- `lock_idle_minutes` — a device with a passkey locks after this long
  without a request (15; `0` = never).
- `log_level` — `error`, `warning` (default), `info` or `debug` (see
  below).
- `quiet_hours`, `quiet_tz` — the nightly window and its time zone; set them
  with `c4ai quiet-hours` rather than by hand.

You don't need to edit the file by hand. `c4ai config` checks each value as the
server does and changes only the key you name, leaving the rest of the file as
you wrote it:

```bash
c4ai config                               # every key, and which are defaults
c4ai config set host 192.168.1.10         # listen on this IP from now on
c4ai config set public_url https://rc.example.com
c4ai config set log_level info            # a bad value never reaches the file
c4ai config unset log_level               # back to the default
c4ai config roots add ~/projects          # lists the devices that gain it
c4ai config roots remove ~/projects       # lists the devices that lose it
```

The server reads the file at start, except the quiet hours, which apply at
once. It keeps keys it does not know, so a typo in a name is silently
ignored (`c4ai config` lists such keys as unused).

`allowed_roots` is the ceiling for every device's folders, e.g.
`"allowed_roots": ["~/work"]`. It **must not be `~`** (nor `/`, `/home` or
anything else above your home directory): a device with that root could read
`~/.ssh` and `~/.claude.json` (MCP URLs and tokens) through its sessions and
browse the whole home directory. The server and the CLI refuse to start with
such a root, with a root inside `~/.clicker4ai` or `~/.claude`, or with none. A
root that contains either of them is fine: devices never reach them.

`"max_runners"` in `~/.clicker4ai/config.json` (default 6) caps how many
sessions may hold a live `claude` process at once — each costs roughly 300 MB of
RAM, so set it from the memory you have. Stopped and detached sessions are free
and don't count; an open True View terminal is a `claude` process of its own and
does (opening it on a running chat swaps one process for the other). At the
limit the server refuses to start another and answers with the sessions you
could stop, newest-idle last, each with the size of the context it would have to
rebuild when you return to it **and the model it runs on** — the same context
costs several times more to rebuild on Opus than on Haiku, so tokens alone would
mislead; the app offers that list and retries once you pick one. Sessions that
are working, waiting for your approval, or outside the device's folders are
never offered.

Only what is used takes a slot. Resuming a past session shows its history
(read from the transcript) without starting a process; it starts with the
first message. A new session starts at once, but one nobody wrote to is
stopped after 15 minutes, and at the limit such empty sessions are stopped
first — from any folder, since nothing is lost — before the "stop one" list
is offered.

`"log_level"` in `~/.clicker4ai/config.json` (read at start) sets what the
server writes to stderr — under pm2 that is `pm2 logs clicker4ai --err`:

- `"warning"` (default) — a message `claude` gave no reaction to within
  60 s (also shown in the chat), a process that died or failed to start,
  each with the last lines of its stderr;
- `"info"` — plus every start and stop, every message sent (and how long
  writing it took) and every finished turn;
- `"debug"` — plus every SDK event, every stderr line, and the `claude`
  process's own debug log in `~/.clicker4ai/logs/cli-<sid>.log` (`--debug-file`;
  grows without limit, delete it by hand);
- `"error"` — nothing of the above.

## Devices and grants

Pair more devices with `c4ai pair`; list / sign out devices with
`c4ai devices` and `c4ai revoke <id>` (or in the app: menu → Devices).

Devices carry a name (yours at pairing time, or guessed from the browser)
which you can change later in the app — its own name always, another
device's with `--manage-devices` — or with `devices rename`. Names are
unique, so a second iPhone is stored as "iPhone 2".

Folder scope and device management are granted per device, from the CLI
only (never from the app):

```bash
c4ai pair                            # no folders yet (the default)
c4ai pair --root ~/work/web --root ~/work/lab  # only these folders
c4ai pair --all-roots                # every folder in allowed_roots
c4ai pair --manage-devices          # may list/revoke other devices in the app
c4ai pair --terminal                # may open True View
c4ai pair --no-passkey              # do not make it register a passkey first
c4ai pair --files                   # may view/download project files
c4ai pair --files-upload            # …and upload them
c4ai pair --quiet-exempt            # quiet hours do not apply to it
c4ai devices set <id> --all-roots --manage-devices --terminal  # change later
c4ai devices set <id> --root ~/work/web         # only these folders
c4ai devices set <id> --add-root ~/work/lab     # add to the current folders
c4ai devices set <id> --no-roots                # no folders (a passkey login)
c4ai devices set <id> --no-terminal --no-manage-devices
c4ai devices set <id> --no-passkey              # stop requiring a passkey (CLI only)
c4ai devices set <id> --quiet-exempt            # this device ignores quiet hours
c4ai devices set <id> --files                   # browse, view, download project files
c4ai devices set <id> --files-upload            # …and upload (implies --files)
c4ai devices rename <id> "Work Phone"           # names are unique
c4ai devices lock <id>                          # sleep, keep grants
```

`devices set` takes the flags of `pair` plus their `--no-` opposites and
`--add-root`; whatever you leave out stays as it is, and it prints the device's
grants afterwards. `<id>` may also be the device's name (`devices set mac-mini
--add-root ~/work/lab`, quoted when it has spaces). Names are unique and never
equal another device's id, and an exact id wins.

A device only reaches sessions, projects and files inside its folders. A freshly
paired device has **none** until you give it some (`--root`, `--all-roots`, or
`devices set` later) — the same way a passkey login starts empty, so access is
always a deliberate step. Without `--manage-devices` it sees and signs out only
itself. `bypassPermissions` and `dontAsk` are never available remotely — use a
local terminal for those. A folder scope limits the app, not Claude: a session
may still touch files outside its directory within the permissions you approve.

## Passkeys

Passkeys are set up from the app (menu → Devices → *Add a passkey*).
They need an https address, so they work behind a TLS reverse proxy (or on
`localhost`) and are switched off everywhere else; the pairing code always
remains.

A device paired with a code, or signed in with a passkey, **must carry a
passkey**: until it registers one the app shows only *Add a passkey* (the
server answers everything else with 428, the socket closes with 4428).
`c4ai pair --no-passkey` skips this for a machine that cannot hold one; the app
can switch the requirement on (menu → Devices → this device), and only
`c4ai devices set <id> --no-passkey` switches it off, so a stolen cookie cannot
shed it. Removing the last passkey of such a device warns first — it then
has to add a new one before it can do anything else. `pair` warns when the
server has no https address, since the requirement could never be met.

A device with a passkey is **locked after `lock_idle_minutes`** (in
`~/.clicker4ai/config.json`, default 15, `0` = never) without a request, and the
passkey unlocks it. An open app pings every 25 s, so this bites when the app
is reopened later — or when a stolen cookie is tried. A passkey then does
three things:

- **signs you in without a pairing code** — the session it creates is
  *empty*: no folders, no True View, no device management. A passkey proves
  who you are; it grants nothing. Folders and grants come from the CLI
  afterwards, exactly as for a device paired with a code, so a key made on a
  device with True View cannot carry True View to a Mac that signs in with
  it.
- **confirms risky actions** — on a device that has a passkey, opening True
  View, choosing "Always" on a permission card and signing out another device
  ask for Face ID / Touch ID first. One confirmation counts for 5 minutes.
- **wakes a locked session** — the menu offers *lock* next to *sign out*.
  Locking keeps the cookie and the device's folders, and refuses every request
  until a passkey unlocks it, so getting back in needs the cookie *and* you.
  Signing out still removes the device for good. A locked session does not slide
  its 30-day idle expiry, and `c4ai devices lock|unlock <id>` does the same from
  the CLI — handy for a phone you have mislaid. If the passkey itself is gone
  (deleted from the keychain), `c4ai passkeys remove <passkey-id>` and
  `c4ai devices unlock <id>` on the server let the device add a new one and keep
  its folders; the lock screen says so.

Removing a passkey here cannot delete it from the phone's keychain — the
server never holds the private key. The app therefore tells the browser
which credentials it still accepts (WebAuthn Signal API: Safari 18.4+,
Chrome 132+), and the system prunes the rest by itself; on older browsers
you remove the leftover entry in the password manager (iOS Settings →
Passwords, Google Password Manager).

A passkey is not a device: iCloud Keychain or Google Password Manager syncs
one credential to all your phones, tablets and computers, so grants stay on
the device record and widening them stays CLI-only. A credential remembers
only which device sessions have signed in with it; those devices see it in
the app and may rename or remove it, as may a device with
`--manage-devices` (renaming another device's key then asks for a passkey
confirmation). Revoking a device leaves the credential
alone — it opens nothing but an empty session. `c4ai passkeys`
lists them and `c4ai passkeys remove <id>` deletes one; removing a credential,
in the app or the CLI, clears the passkey mark on every device left without
one, so none of them keeps asking for a key that no longer exists.

```bash
c4ai passkeys                       # list registered passkeys
c4ai passkeys remove <passkey-id>
c4ai passkeys rename <passkey-id> "Shared key"   # the label, not the keychain
```

## Quiet hours

Quiet hours (`quiet-hours`, stored as `"quiet_hours"` in
`~/.clicker4ai/config.json`, may cross midnight) stop every device from
starting a new turn inside the window. The window is in the host's local time
unless `--tz` names a zone (`"quiet_tz"`, e.g. `Europe/London`, which follows
summer time); never in the device's zone, which a phone could change to shift
the window. A turn already running
finishes and its approval cards still work, but the next message or side
question is refused (the app puts the text back in the box); `/compact` still
works, as a last step while the cache is warm. In True View a hook added to the
`claude` it starts refuses the prompt the same way, judged by the device that
typed last. Changes apply without a restart. Only the CLI sets the window or
exempts a device (`c4ai devices set <id> --quiet-exempt`) — the app cannot, so
a phone at 1 a.m. cannot either. A terminal on the host itself is not affected.

```bash
c4ai quiet-hours 00:30-08:00        # no new prompts at night (`off` removes it)
c4ai quiet-hours --tz Europe/London # window in this zone, not host time
```

## Project files

Project files (`--files` on `c4ai pair` or `c4ai devices set <id>`) add
*Files* to a project screen, to a session's ⋯ menu (session list and Tabs) and
to the chat as `/files`: a plain list of the folder, with no Claude and no
tokens involved. Tap a file to see it — text as plain text (the first 1 MB),
images whole on the screen and never above 1:1 with its pixels (a tap shows 1:1)
— or download it (with a keyboard: ↑ ↓ scroll, ← → the next file, `d` downloads,
⏎ closes; back closes just the viewer). In the chat, ↗ on a Read, Edit or Write
card opens that file the same way. Hidden files are listed; `.git`,
`node_modules` and `.venv` are not, and nothing is served through them. The
reach is the device's folders, minus this server's data and `~/.claude`, as
everywhere else. Nothing from a project runs on the app's origin: images come
with `Content-Security-Policy: sandbox` and `nosniff`, every other file only as
a download. `--files-upload` also lets the device upload one file at a time (up
to 50 MB) into the folder on screen; an existing name is never overwritten — the
app asks for another one or cancels.

## True View

True View (the `>_` button in a chat) streams the real Claude Code TUI.
It is off unless the device was paired with `--terminal`. In the chat every
tool call goes through Claude and your approval card in the app; in True
View `!command` runs shell commands directly and `/permissions` can add
allow rules, so it is effectively a shell with your user's rights —
regardless of `--root`, which only decides which sessions it can open.
It opens in a new session too, before the chat's first message: the TUI
then starts the Claude session under an id the server gives it, and the
chat continues that conversation. Closed with nothing typed, it leaves no
conversation, and the chat's first message starts one of its own.

## Reverse proxy (Caddy)

Recommended setup: Caddy on the same host terminates TLS and proxies to
`127.0.0.1:8780`; your devices reach Caddy over a VPN. Store the address
with `c4ai config set public_url https://rc.example.com` so pairing links
point there and the session cookie becomes `Secure` + `__Host-` prefixed.
`--public-url` does the same for one run only.

```caddyfile
rc.example.com {
	# Optional: listen only on the interface VPN traffic arrives on.
	# bind 192.168.1.10

	# Only VPN clients may reach the app. Adjust to your VPN subnet —
	# see the NAT note below before relying on this.
	@outside not remote_ip 10.8.0.0/24
	respond @outside 403

	header {
		Strict-Transport-Security "max-age=31536000"
		Content-Security-Policy "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self' wss://rc.example.com; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
		X-Content-Type-Options "nosniff"
		Referrer-Policy "no-referrer"
		-Server
	}

	reverse_proxy 127.0.0.1:8780
}
```

- **TLS**: use your existing (wildcard) certificate setup for the site block.
- **`remote_ip` and NAT**: `remote_ip` matches the TCP peer. If the VPN
  gateway NATs VPN clients, every VPN request arrives with the *gateway's*
  address (and so may LAN traffic routed through it), which makes the filter
  much weaker. Check the Caddy access log (`log` directive, field
  `request.remote_ip`) while connected over the VPN and set the matcher to
  what you actually see; prefer a routed (non-NAT) VPN subnet.
- **CSP**: `'unsafe-inline'` for styles is needed because the UI and xterm.js
  set inline `style` attributes; scripts stay `'self'` only.
- **Optional mTLS**: require a client certificate on top of the device
  session (Caddy ≥ 2.8 syntax):

  ```caddyfile
  tls {
  	client_auth {
  		mode require_and_verify
  		trust_pool file /etc/caddy/rc-client-ca.pem
  	}
  }
  ```

  Install the client certificate on each device (on iOS/iPadOS as a profile).
  Test the home-screen PWA as well as Safari — client-certificate prompts in
  standalone web apps have been unreliable on iOS.

## Reverse proxy (nginx)

The same setup with nginx. Unlike Caddy it needs to be told three things:
keep the `Host` header (nginx sends `127.0.0.1:8780` otherwise, and the
server's Origin check then refuses every WebSocket and browser `/api/` call),
pass WebSocket upgrades, and accept bodies larger than 1 MB (file uploads).

```nginx
# In the http {} block: the Connection header for WebSocket upgrades.
map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}

server {
    listen 443 ssl;
    server_name rc.example.com;
    ssl_certificate     /etc/ssl/rc.example.com/fullchain.pem;
    ssl_certificate_key /etc/ssl/rc.example.com/privkey.pem;

    # Only VPN clients may reach the app (the NAT note above applies).
    allow 10.8.0.0/24;
    deny  all;

    add_header Strict-Transport-Security "max-age=31536000" always;
    add_header Content-Security-Policy "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self' wss://rc.example.com; frame-ancestors 'none'; base-uri 'none'; form-action 'self'" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header Referrer-Policy "no-referrer" always;
    server_tokens off;

    client_max_body_size 51m;   # file uploads go up to 50 MB

    location / {
        proxy_pass http://127.0.0.1:8780;
        proxy_http_version 1.1;
        proxy_set_header Host $host;   # the Origin check compares against it
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_read_timeout 1h;
    }
}
```

The TLS, `remote_ip`/NAT and CSP notes for Caddy apply here too; `allow`
matches the TCP peer just as `remote_ip` does.

## Requirements

- **Linux** (any POSIX system with `pty`/`termios` should work).
- **Python ≥ 3.13**.
- **Claude Code CLI** installed and authenticated (`claude` on your PATH,
  already logged in). Sessions load your real `~/.claude` config — CLAUDE.md,
  MCP servers, hooks, and custom commands all apply, exactly as in the terminal.

## Uninstall and starting over

The server's data in `~/.clicker4ai` (or `C4AI_DATA_DIR`) survives restarts,
upgrades and reinstalls. To wipe it or remove Clicker4AI:

1. End open incognito chats in the app. Their transcripts live in `~/.claude`
   and only the server erases them; a chat still open when its data is deleted
   stays there for good.
2. Stop the server (and take it out of your supervisor, if you are removing
   it).
3. Delete `~/.clicker4ai/config.json` to reset only the settings, or the whole
   `~/.clicker4ai` directory to also sign out every device and delete passkeys
   and session logs. Your Claude Code sessions stay in `~/.claude`.
4. To remove the program as well: `pipx uninstall clicker4ai` (or delete the
   clone). Passkeys saved on your devices no longer work anywhere — remove
   them from the password manager.

## Authentication and trademarks

- **Clicker4AI never signs in to Anthropic.** It holds no Anthropic
  credentials and proxies no login; it starts the `claude` CLI on your machine,
  which uses whatever authentication you gave Claude Code — an API key, a
  cloud provider, or a claude.ai account.
- **With a claude.ai subscription, compliance is yours.** Anthropic does not
  allow third-party products to offer claude.ai login or subscription rate
  limits unless previously approved (see the note in the
  [Agent SDK overview](https://code.claude.com/docs/en/agent-sdk/overview) and
  [Legal and compliance](https://code.claude.com/docs/en/legal-and-compliance)).
  Check the terms that apply to your account; an API key is the safe choice.
- **One owner.** Pairing is meant for your own phones, tablets and laptops,
  not for sharing your Claude access with other people.
- **Not an Anthropic product.** Clicker4AI is not made, endorsed or supported
  by Anthropic. "Claude" and "Claude Code" are trademarks of Anthropic and are
  used here only to describe what this software works with.

## Security model

This server exposes control of Claude Code sessions on your machine. Treat it
accordingly:

- **Per-device sessions, no master token.** A device logs in once with a
  one-time pairing code (`XXXXX-XXXXX`, 2 minutes, single use) issued by
  `c4ai pair`; the server then sets an **HttpOnly** session cookie
  (`Secure` + `__Host-` prefix behind an https `public_url`, `SameSite=Lax`).
  Sessions expire after 30 idle days; `c4ai revoke <id>` (or logout) ends one at
  once, including its open WebSockets (close 4401 on the next message).
  Only SHA-256 hashes of cookies and codes are stored.
- **Grants per device, set only from the CLI, and empty by default:** folders
  (`--root`/`--all-roots`, inside `allowed_roots`), managing other devices
  (`--manage-devices`), True View (`--terminal` — effectively a shell) and
  project files (`--files`, `--files-upload`). A new device, however it signed
  in, reaches nothing until the CLI says otherwise. `allowed_roots` itself may
  not be the home directory or above, so no grant can reach `~/.ssh` or
  `~/.claude.json`.
- **Passkeys (WebAuthn), where https allows them.** Registered from a signed-in
  device, stored as public keys only, bound to the RP ID of your `public_url`. A
  credential carries **no grants**: signing in with one creates a device with no
  folders at all, so a synced key can never widen access on another machine.
  They add a login path, and a step-up confirmation (user verification required,
  5 minutes) in front of "always allow" and signing out another device, and
  every time True View starts a new terminal (joining one that already runs asks
  nothing) — so an unlocked, stolen phone is not enough. Challenges are
  single-use and held in memory only. A revoked device's passkeys are deleted
  with it.
- **Incognito chats are confined, not just hidden.** Their session has only
  the WebSearch and WebFetch tools (Bash, Read, Edit… do not exist in it)
  and no MCP servers; "always allow" stays in the session; True View is
  refused by the server. The folder lives in the server's data directory
  (`~/.clicker4ai/incognito/<device>/<sid>`) and only the owning device's
  scope reaches it.
- **Folder trust, as in the terminal.** Claude Code skips its "Do you trust
  the files in this folder?" question when it runs non-interactively, as the
  chat's `claude` does, yet still loads the folder's project settings: hooks
  run on their own, `permissions.allow` pre-approves tools, and a command
  with `allowed-tools: Bash(...)` runs its `` !`…` `` lines when typed. So
  before a `claude` process starts (new session, first message, `/compact`,
  `/clear`, True View) in a folder that holds `.claude/settings*.json`,
  `.claude/commands`, `skills` or `agents`, or `.mcp.json` (there or in a
  parent below the home directory), the app lists what it found and asks
  the device to trust the folder, behind a passkey confirmation. Only this
  exact folder counts: trusted in the terminal (`~/.claude.json`) or from
  the app (`~/.clicker4ai/trusted.json`). `c4ai trust` lists both,
  `trust add` and `trust remove` change the app's list; the terminal's trust
  is taken back in `~/.claude.json`. A trusted parent does not count, so a
  repository cloned under a folder trusted long ago still asks; the
  question names that parent. Empty and new folders never ask.

  ```bash
  c4ai trust                          # folders whose project settings run
  c4ai trust add ~/work/web/site      # as "Trust and continue" in the app
  c4ai trust remove ~/work/web/site   # take the app's trust back
  ```
- **No unattended modes remotely:** `bypassPermissions` and `dontAsk` can't
  be selected from the app; every tool call in the chat goes through your
  approval.
- **Hardening:** failed logins throttled (10/min → 429), state-changing `/api/`
  requests must be `application/json` (CSRF), WebSocket Origin allowlist,
  validated model/mode/session ids, `~/.clicker4ai` 0700 with 0600 files
  (looser modes are put back, with a warning on stderr), out of every
  device's reach (as is `~/.claude`) even under an allowed root.
- **The server binds `127.0.0.1` by default.** Expose it through a TLS reverse
  proxy restricted to your VPN (see [Reverse proxy](#reverse-proxy-caddy)).
  **Do not port-forward it to the public internet.**
- **No outbound traffic from the server itself.** The `claude` processes it
  starts talk to the Anthropic API only, with
  `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` (no telemetry/auto-update).
- **Nothing sensitive is in the repository.** Devices, codes, config and
  session logs live in `~/.clicker4ai`, outside the checkout.

## Handoff semantics

### Three things called a "session"

Keeping these apart answers most questions about what may run where:

- **Claude session** — a `claude_session_id` and its transcript file under
  `~/.claude/projects`. This is the conversation itself.
- **Server session (runner)** — this server's record (`sid`) and the one
  `claude` process driving that conversation. The runner is what appends to
  the transcript.
- **Device session** — a phone, tablet or laptop signed in with a pairing code
  or a passkey. Authentication and folder scope; it drives nothing.

Several device sessions may watch and drive **one** runner at the same time:
they are viewers of a single process, so nothing can fork. Switching from the
phone to the tablet and back needs no stopping, closing or handing over — just
start typing. What must not overlap is two *processes* on one Claude session:
the runner and a `claude --resume` in a terminal.

### One driver per turn

A **turn** runs from sending a message until the reply is complete, tool calls
included — the app's status line returns to "Ready", the terminal gets its
prompt back. Inside that window the runner is busy and its transcript tailer
pauses (`clicker4ai/sessions.py`), so anything a second process appends is
invisible to it and the parent chain forks.

Between turns you may switch freely. Type in the terminal, then answer from the
app: the tailer mirrors the terminal's rows into the app, marks the context
stale, and the app's next message silently rotates the session through
`--resume` so both sides share one context on one session id.

The terminal `claude` and the webapp mirror **one** transcript file (the single
source of truth both append to):

- The webapp can mirror a live terminal session and vice versa; opening a
  session on either side backfills its history and stays in sync.
- **One driver at a time.** Two processes actively driving the *same* session id
  simultaneously would fork the parent chain — so hand off, don't co-drive. The
  webapp mirrors a live terminal, but both typing at once is unsupported by
  design. An already-open terminal TUI can't live-refresh mid-session (a CLI
  limitation); it picks up the app's turns on the next `--resume` /
  `--continue`.

## Development

The backend is a normal FastAPI app under `clicker4ai/`; the frontend is a
single vanilla-JS SPA under `clicker4ai/web/` (no build step — edit and
reload).

From a clone, with `venv` (Debian: `python3.13-venv`; it brings the pip
wheel used inside `.venv`, so no system `python3-pip` is needed):

```bash
python3 -m venv .venv                     # create the venv
.venv/bin/pip install -e .                # install deps; the CLI is .venv/bin/c4ai
.venv/bin/pip install httpx               # dev dependency of the test scripts
.venv/bin/python scripts/smoke_api.py     # REST smoke suite
.venv/bin/python scripts/passkey_test.py  # WebAuthn register/login/step-up (software authenticator)
.venv/bin/python scripts/e2e_live.py      # live end-to-end (text / Bash / compact / resume / sync / hooks)
node scripts/kbd_test.js <pairing code>   # keyboard shortcuts (Playwright; test server on 8781)
```

The `scripts/e2e_*.py` files each exercise one slice end-to-end against a live
server (resume parity, permission approve/deny, terminal↔webapp sync, Stop-hook
status feedback).

## License

[AGPL-3.0-only](https://github.com/kosio-labs/clicker4ai/blob/main/LICENSE)
© 2026 [Kosio](https://github.com/kosio-labs).

Based on `better-claude-rc` 0.1.0 © 2026 Rafa Rayes, released under the MIT
License, and reworked into a local-only server; its notice is kept in
[NOTICE](https://github.com/kosio-labs/clicker4ai/blob/main/NOTICE).
