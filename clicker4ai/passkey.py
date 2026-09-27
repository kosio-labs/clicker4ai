"""WebAuthn passkeys: a second way in, and a confirmation for risky actions.

Two jobs, both on top of the device sessions in auth.py:

  login    a registered passkey creates a new device session without a
           pairing code. That session starts **empty**: no folders, no
           True View, no device management. A passkey proves who you are;
           it never hands out access. Folders and grants come from the
           CLI afterwards, exactly as for a device paired with a code.
  step-up  a fresh Face ID / Touch ID confirmation, valid for a few
           minutes on one device session, required before True View,
           device management and "always allow" (see main.py). A merely
           unlocked, stolen phone is then not enough.
  unlock   the same proof, used to wake a locked device session. The
           cookie never left the browser, so this grants nothing new: it
           takes the cookie *and* the owner to get back in.

A passkey is *not* a device. iCloud Keychain syncs one credential to the
owner's iPhone, iPad and Mac, so the credential is only a way to prove
ownership; grants keep living on the device record. The credential
therefore stores no grants at all: it cannot widen anything, and a key
made on a device with True View does not carry True View to a Mac that
logs in with it. Granting stays CLI-only (auth.py, scope.py).

`used_by` lists the device sessions that registered or logged in with a
credential. Those devices see it in the app and may remove it, as may a
device with `manage_devices`; revoking a device only drops it from that
list. A leftover credential is harmless now — it opens nothing but an
empty session.

Storage: DATA_DIR/passkeys.json (0600, atomic write, the auth.py flock)

  {"user_handle": "<b64url>",          # stable WebAuthn user id (the owner)
   "credentials": [{id, public_key, sign_count, name, created_at,
                    last_used, backed_up, used_by: [device id, ...]}]}

Only public keys are stored, so the file grants no access if it leaks.

Passkeys need a secure context and an RP ID that is a real domain, so
they are offered only behind an https public URL (or on localhost) —
`available()`. Everywhere else the pairing code remains the only login.
"""

from __future__ import annotations

import ipaddress
import json
import secrets
import time
from pathlib import Path
from urllib.parse import urlparse

from webauthn import (
    base64url_to_bytes,
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import bytes_to_base64url
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from .auth import file_lock
from .config import DATA_DIR, write_private

PASSKEYS_PATH = DATA_DIR / "passkeys.json"

RP_NAME = "Clicker4AI"
CHALLENGE_TTL_SECS = 120      # a begin/finish pair must complete within this
MAX_CHALLENGES = 64           # cap per pool (logins, devices) on unfinished challenges
ELEVATION_SECS = 300          # how long one step-up confirmation counts
NAME_MAX = 64

_rp_id: str | None = None     # None = passkeys unavailable on this origin
_origin: str = ""


class PasskeyError(Exception):
    """Anything the caller should turn into a 4xx."""


# ---------------- origin / availability ----------------

def configure(public_url: str | None, port: int) -> None:
    """Pin the RP ID and expected origin (called before serving).

    WebAuthn ties a credential to one domain: the RP ID must be a real
    hostname (bare IPs are rejected by browsers) reached over https, or
    localhost. Anything else leaves passkeys switched off."""
    global _rp_id, _origin
    _rp_id, _origin = None, ""
    url = (public_url or f"http://localhost:{port}").rstrip("/")
    u = urlparse(url)
    host = u.hostname or ""
    if not host:
        return
    try:
        ipaddress.ip_address(host)
        return          # an IP address can never be an RP ID
    except ValueError:
        pass
    if u.scheme != "https" and host != "localhost":
        return          # not a secure context for the browser
    _rp_id, _origin = host, url


def available() -> bool:
    return _rp_id is not None


def rp_id() -> str | None:
    return _rp_id


# ---------------- store ----------------

def _read() -> dict:
    try:
        data = json.loads(PASSKEYS_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _credentials(data: dict | None = None) -> list[dict]:
    data = _read() if data is None else data
    creds = data.get("credentials")
    if not isinstance(creds, list):
        return []
    out = [c for c in creds if isinstance(c, dict)]
    for c in out:
        # Credentials written before grants were dropped carried a single
        # owning device and a copy of its grants. Keep the device as the
        # first user and let the grants die with the old format.
        if "used_by" not in c:
            c["used_by"] = [c["device_id"]] if c.get("device_id") else []
        for stale in ("device_id", "roots", "manage_devices", "terminal"):
            c.pop(stale, None)
    return out


def _write(data: dict) -> None:
    write_private(PASSKEYS_PATH, data)


def _user_handle() -> bytes:
    """The owner's stable WebAuthn user id, created on first use. One
    handle for every passkey, so they group under one entry in the
    keychain instead of piling up as separate accounts."""
    with file_lock():
        data = _read()
        raw = data.get("user_handle")
        if not isinstance(raw, str) or not raw:
            raw = bytes_to_base64url(secrets.token_bytes(32))
            data["user_handle"] = raw
            data.setdefault("credentials", _credentials(data))
            _write(data)
    return base64url_to_bytes(raw)


def _find(cred_id: str) -> dict | None:
    return next((c for c in _credentials() if c.get("id") == cred_id), None)


def _clean_name(name: str, fallback: str) -> str:
    name = " ".join(str(name or "").split())[:NAME_MAX]
    return name or fallback


# Names are unique (case-insensitive) so two keys never look alike in the
# app or `c4ai passkeys`; they are labels only, never an address.
def _name_taken(creds: list[dict], name: str, except_id: str | None = None) -> bool:
    folded = name.casefold()
    return any(c.get("id") != except_id and str(c.get("name") or "").casefold() == folded
               for c in creds)


def _unique_name(creds: list[dict], name: str) -> str:
    """`name`, or `name (2)`, `name (3)`… — shortened to fit NAME_MAX."""
    n = 1
    candidate = name
    while _name_taken(creds, candidate):
        n += 1
        suffix = f" ({n})"
        candidate = name[:NAME_MAX - len(suffix)].rstrip() + suffix
    return candidate


def has_credentials() -> bool:
    return bool(_credentials())


def status(device: dict | None = None) -> dict:
    """What the frontend needs to decide which buttons to show. `rp_id`
    also lets an unauthenticated login screen report an orphaned
    credential back to the keychain (signal_payload)."""
    return {
        "available": available(),
        "has_credentials": has_credentials(),
        "device_passkey": bool(device and device.get("passkey")),
        # the lock and "add a passkey" screens name the device for the CLI
        "device": ({"id": device.get("id"), "name": device.get("name"),
                    "require_passkey": bool(device.get("require_passkey"))}
                   if device else None),
        "elevation_secs": ELEVATION_SECS,
        "rp_id": _rp_id,
    }


def signal_payload() -> dict:
    """Everything the WebAuthn Signal API needs to prune the keychain.

    A server can never delete a credential itself — the private key lives
    in the Secure Enclave. `signalAllAcceptedCredentials` closes that gap:
    given the full set of ids this server still accepts, the system drops
    the rest from the user's keychain, so a passkey removed here (or with
    a device revoke, or from the CLI) stops haunting the phone.

    The ids are deliberately unfiltered: the set is "everything accepted
    for this owner", and signalling a partial list would delete good
    credentials. They are public identifiers, and only a signed-in device
    can ask. Returns user_id None when no passkey was ever registered —
    there is then nothing to signal, and no handle is created."""
    data = _read()
    handle = data.get("user_handle")
    return {
        "rp_id": _rp_id,
        "user_id": handle if isinstance(handle, str) and handle else None,
        "credential_ids": [c["id"] for c in _credentials(data) if c.get("id")],
    }


def list_public() -> list[dict]:
    """Credential list without public keys."""
    return [{"id": c.get("id"), "name": c.get("name"),
             "created_at": c.get("created_at"), "last_used": c.get("last_used"),
             "backed_up": bool(c.get("backed_up")),
             "used_by": list(c.get("used_by") or [])}
            for c in _credentials()]


def may_touch(cred: dict, device: dict) -> bool:
    """Who may see and remove a credential: the devices that have signed
    in with it, and any device allowed to manage devices."""
    return bool(device.get("manage_devices")) or \
        device.get("id") in (cred.get("used_by") or [])


def remove(cred_id: str) -> bool:
    with file_lock():
        data = _read()
        creds = _credentials(data)
        keep = [c for c in creds if c.get("id") != cred_id]
        if len(keep) == len(creds):
            return False
        data["credentials"] = keep
        _write(data)
    return True


def rename(cred_id: str, name: str) -> str:
    """Relabel a credential. The label lives here only: the keychain shows
    the shared user name (RP_NAME) for every key. Refuses another key's
    name. Returns the cleaned name."""
    name = " ".join(str(name or "").split())
    if not name:
        raise PasskeyError("name must not be empty")
    if len(name) > NAME_MAX:
        raise PasskeyError(f"name is longer than {NAME_MAX} characters")
    with file_lock():
        data = _read()
        creds = _credentials(data)
        hit = next((c for c in creds if c.get("id") == cred_id), None)
        if hit is None:
            raise PasskeyError("no such passkey")
        if _name_taken(creds, name, except_id=cred_id):
            raise PasskeyError(f"{name!r} is already another passkey's name")
        hit["name"] = name
        data["credentials"] = creds
        _write(data)
    return name


def devices_with_keys() -> set[str]:
    """Device ids that still have at least one credential."""
    return {d for c in _credentials() for d in (c.get("used_by") or [])}


def record_use(cred_id: str, device_id: str) -> None:
    """Remember that this device session signed in with the credential, so
    the app shows the passkey there and lets it be removed from there."""
    with file_lock():
        data = _read()
        creds = _credentials(data)
        hit = next((c for c in creds if c.get("id") == cred_id), None)
        if hit is None:
            return
        used = list(hit.get("used_by") or [])
        if device_id in used:
            return
        hit["used_by"] = used + [device_id]
        data["credentials"] = creds
        _write(data)


def drop_device(device_id: str) -> None:
    """Forget a revoked device without deleting its credentials: a
    credential grants nothing by itself now — signing in with one yields
    an empty session — and the same key lives on every device the owner's
    keychain syncs it to, so deleting it here would be theatre. It only
    stops being listed on the device that is gone."""
    with file_lock():
        data = _read()
        creds = _credentials(data)
        changed = False
        for c in creds:
            used = list(c.get("used_by") or [])
            if device_id in used:
                c["used_by"] = [d for d in used if d != device_id]
                changed = True
        if changed:
            data["credentials"] = creds
            _write(data)


# ---------------- challenges ----------------

# challenge (b64url) -> (purpose, device_id or None, expires_at). In memory
# only: a restart invalidates pending logins, which is the safe direction.
_challenges: dict[str, tuple[str, str | None, float]] = {}


def _remember(challenge: bytes, purpose: str, device_id: str | None) -> None:
    now = time.time()
    for key, (_p, _d, exp) in list(_challenges.items()):
        if exp <= now:
            del _challenges[key]
    # two pools, each capped on its own: anonymous logins (no device) must
    # not evict a signed-in device's step-up, unlock or registration
    anon = device_id is None
    pool = [k for k, (_p, d, _e) in _challenges.items() if (d is None) == anon]
    for key in pool[:max(0, len(pool) - MAX_CHALLENGES + 1)]:
        del _challenges[key]
    _challenges[bytes_to_base64url(challenge)] = (
        purpose, device_id, now + CHALLENGE_TTL_SECS)


def _take(credential: dict, purpose: str, device_id: str | None) -> bytes:
    """Consume the challenge a response was signed over. Single use, so a
    captured assertion cannot be replayed."""
    try:
        client_data = json.loads(
            base64url_to_bytes(credential["response"]["clientDataJSON"]))
        key = client_data["challenge"]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise PasskeyError("malformed credential")
    hit = _challenges.pop(key, None)
    if hit is None or hit[2] <= time.time():
        raise PasskeyError("challenge expired — try again")
    if hit[0] != purpose or hit[1] != device_id:
        raise PasskeyError("challenge does not match this request")
    return base64url_to_bytes(key)


def _credential_obj(raw) -> dict:
    if not isinstance(raw, dict) or not isinstance(raw.get("response"), dict):
        raise PasskeyError("expected a WebAuthn credential object")
    return raw


def _require_available() -> str:
    if _rp_id is None:
        raise PasskeyError("passkeys need an https address for this server")
    return _rp_id


# ---------------- registration ----------------

def registration_options(device: dict) -> dict:
    """Options for creating a passkey, from an already signed-in device."""
    rp = _require_available()
    opts = generate_registration_options(
        rp_id=rp,
        rp_name=RP_NAME,
        user_id=_user_handle(),
        user_name=RP_NAME,
        user_display_name=RP_NAME,
        authenticator_selection=AuthenticatorSelectionCriteria(
            # discoverable: the phone offers the passkey with no username
            resident_key=ResidentKeyRequirement.REQUIRED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
        exclude_credentials=[
            PublicKeyCredentialDescriptor(id=base64url_to_bytes(c["id"]))
            for c in _credentials() if c.get("id")],
    )
    _remember(opts.challenge, "register", device["id"])
    return json.loads(options_to_json(opts))


def register_finish(device: dict, credential: dict, name: str = "") -> dict:
    """Verify a new passkey and store it with this device's grants."""
    rp = _require_available()
    credential = _credential_obj(credential)
    challenge = _take(credential, "register", device["id"])
    try:
        v = verify_registration_response(
            credential=credential,
            expected_challenge=challenge,
            expected_rp_id=rp,
            expected_origin=_origin,
            require_user_verification=True,
        )
    except Exception as e:
        raise PasskeyError(f"passkey rejected: {e}")
    cred_id = bytes_to_base64url(v.credential_id)
    now = time.time()
    entry = {
        "id": cred_id,
        "public_key": bytes_to_base64url(v.credential_public_key),
        "sign_count": int(v.sign_count),
        "name": _clean_name(name, device.get("name") or "Passkey"),
        "created_at": now,
        "last_used": now,
        "backed_up": bool(v.credential_backed_up),
        # no grants here on purpose — see the module docstring
        "used_by": [device["id"]],
    }
    with file_lock():
        data = _read()
        creds = [c for c in _credentials(data) if c.get("id") != cred_id]
        entry["name"] = _unique_name(creds, entry["name"])
        creds.append(entry)
        data["credentials"] = creds
        _write(data)
    return entry


# ---------------- authentication ----------------

def _assertion_options(purpose: str, device_id: str | None,
                       allow: list[dict] | None) -> dict:
    rp = _require_available()
    opts = generate_authentication_options(
        rp_id=rp,
        user_verification=UserVerificationRequirement.REQUIRED,
        allow_credentials=[
            PublicKeyCredentialDescriptor(id=base64url_to_bytes(c["id"]))
            for c in (allow or []) if c.get("id")] or None,
    )
    _remember(opts.challenge, purpose, device_id)
    return json.loads(options_to_json(opts))


def login_options() -> dict:
    """Options for signing in. No allowCredentials: the browser offers the
    discoverable passkeys it has, and an unauthenticated caller learns
    nothing about which credentials exist."""
    if not has_credentials():
        raise PasskeyError("no passkey is registered on this server")
    return _assertion_options("login", None, None)


def stepup_options(device: dict) -> dict:
    """Options for confirming a risky action on a signed-in device."""
    if not has_credentials():
        raise PasskeyError("no passkey is registered on this server")
    return _assertion_options("stepup", device["id"], None)


def _verify_assertion(credential: dict, purpose: str,
                      device_id: str | None) -> dict:
    rp = _require_available()
    credential = _credential_obj(credential)
    challenge = _take(credential, purpose, device_id)
    cred_id = credential.get("id") or credential.get("rawId")
    stored = _find(cred_id) if isinstance(cred_id, str) else None
    if stored is None:
        raise PasskeyError("unknown passkey")
    try:
        v = verify_authentication_response(
            credential=credential,
            expected_challenge=challenge,
            expected_rp_id=rp,
            expected_origin=_origin,
            credential_public_key=base64url_to_bytes(stored["public_key"]),
            credential_current_sign_count=int(stored.get("sign_count") or 0),
            require_user_verification=True,
        )
    except Exception as e:
        raise PasskeyError(f"passkey rejected: {e}")
    with file_lock():
        data = _read()
        creds = _credentials(data)
        fresh = next((c for c in creds if c.get("id") == stored["id"]), None)
        if fresh is None:
            raise PasskeyError("unknown passkey")   # removed meanwhile
        fresh["sign_count"] = int(v.new_sign_count)
        fresh["last_used"] = time.time()
        fresh["backed_up"] = bool(v.credential_backed_up)
        data["credentials"] = creds
        _write(data)
        stored = dict(fresh)
    return stored


def login_finish(credential: dict) -> dict:
    """Verify a login assertion → the credential, whose grants the caller
    gives to the new device session."""
    return _verify_assertion(credential, "login", None)


# ---------------- step-up elevation ----------------

# device id -> expiry. In memory: a restart (or a revoke) drops every
# elevation, which is the safe direction.
_elevated: dict[str, float] = {}


def stepup_finish(device: dict) -> float:
    """Record a fresh confirmation for this device session → its expiry."""
    until = time.time() + ELEVATION_SECS
    _elevated[device["id"]] = until
    return until


def stepup_verify(device: dict, credential: dict) -> float:
    """Verify a step-up assertion and elevate the device session."""
    _verify_assertion(credential, "stepup", device["id"])
    return stepup_finish(device)


def unlock_options(device: dict) -> dict:
    """Options for waking a locked device session."""
    if not has_credentials():
        raise PasskeyError("no passkey is registered on this server")
    return _assertion_options("unlock", device["id"], None)


def unlock_verify(device: dict, credential: dict) -> float:
    """Verify an unlock assertion; the caller clears the locked flag."""
    _verify_assertion(credential, "unlock", device["id"])
    return stepup_finish(device)   # a fresh confirmation, so also an elevation


def is_elevated(device: dict) -> bool:
    exp = _elevated.get(device.get("id") or "", 0)
    if exp <= time.time():
        _elevated.pop(device.get("id") or "", None)
        return False
    return True


def drop_elevation(device_id: str) -> None:
    _elevated.pop(device_id, None)


def needs_stepup(device: dict) -> bool:
    """Whether a risky action on this device must be confirmed first.

    Only devices that carry a passkey are held to it: a device paired
    before passkeys existed (or on a host where they are unavailable)
    keeps working exactly as before, and can never be locked out of its
    own grants by someone else registering a key."""
    if not available() or not device.get("passkey"):
        return False
    return not is_elevated(device)
