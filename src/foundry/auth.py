# SPDX-License-Identifier: Apache-2.0
"""Azure CLI authentication.

`foundry` embeds no credential library and stores no secret (SPEC 3): every
token is minted by shelling out to ``az``, which the user has already signed in
to.  Three measured behaviours drive the shape of this module.

1. **Windows.** ``az`` is a ``.cmd`` shim, so the subprocess needs
   ``shell=True`` on win32, and every call must pass
   ``encoding="utf-8", errors="replace"``.  Without the explicit encoding, ``az``
   output containing non-ASCII (a tenant display name such as
   "Diretorio Padrao") raises ``UnicodeDecodeError`` inside subprocess's reader
   thread; the exception is swallowed there and ``stdout`` silently arrives as
   ``None``, which surfaces much later as a baffling ``TypeError``.  Both the
   encoding and a ``None`` guard are applied here, once, for every caller.

2. **Token lifetimes are 72-90 minutes** and a long agent session outlives them,
   so tokens are cached per ``(resource, subscription)`` and re-minted once 80%
   of the lifetime reported in ``expiresOn`` has elapsed.

3. **A stale service principal in the environment breaks Claude Code.** Its
   ``DefaultAzureCredential`` tries ``EnvironmentCredential`` first and fails
   outright -- it does not fall through -- when ``AZURE_CLIENT_ID`` /
   ``AZURE_CLIENT_SECRET`` / ``AZURE_TENANT_ID`` name an expired or
   foreign-tenant SP.  :func:`scrubbed_env` removes them from the child
   environment so the chain reaches ``AzureCliCredential``.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime

from foundry import endpoints


class AuthError(RuntimeError):
    """A user-fixable credential problem. The message always names the fix."""


#: Entra scope current Foundry documentation prefers for data-plane inference.
DATA_RESOURCE = "https://ai.azure.com"
#: Equally valid on the data plane; some tenants issue only this one.
DATA_FALLBACK = "https://cognitiveservices.azure.com"
#: Management plane. An inference token is rejected here, and vice versa.
ARM_RESOURCE = "https://management.azure.com"

#: Pre-minted inference bearer. Short-circuits the data plane only (SPEC 3).
BEARER_ENV = "FOUNDRY_BEARER"
#: Resource API key used as the inference credential instead of an Entra token.
API_KEY_ENV = "FOUNDRY_API_KEY"

#: Environment variables that steer ``EnvironmentCredential``; see module docstring.
SP_ENV_VARS: tuple[str, ...] = ("AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET", "AZURE_TENANT_ID")

_ON_WINDOWS = sys.platform == "win32"

#: Re-mint once this fraction of the token's remaining lifetime has been spent.
_REFRESH_FRACTION = 0.8
#: Used when ``az`` reports an expiry we cannot parse. 30 min sits inside every
#: measured lifetime (72-90 min).
_UNKNOWN_LIFETIME_SECONDS = 1800.0
#: Never hand out a token this close to its own expiry.
_MIN_REMAINING_SECONDS = 120.0
#: An interactive browser or device-code sign-in needs a human; give them time.
_LOGIN_TIMEOUT_SECONDS = 900

_AZ_MISSING = (
    "The Azure CLI (`az`) is not on PATH, and foundry gets every credential from it.\n"
    "Install it from https://aka.ms/InstallAzureCLI, then run `az login`."
)


@dataclass
class _CachedToken:
    value: str
    expires_at: float | None
    refresh_at: float

    def usable(self, now: float) -> bool:
        if now >= self.refresh_at:
            return False
        return self.expires_at is None or now < self.expires_at - _MIN_REMAINING_SECONDS


_CACHE: dict[tuple[str, str | None], _CachedToken] = {}
_CACHE_LOCK = threading.Lock()

# Probe results for ambient service-principal credentials, keyed by tenant,
# client id and a hash of the secret. The secret itself is never retained.
_SP_PROBE: dict[tuple[str, str, str], bool] = {}
_SP_PROBE_LOCK = threading.Lock()


# --------------------------------------------------------------------------
# subprocess boundary
# --------------------------------------------------------------------------


def run_az(args: list[str], *, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    """Run ``az <args>`` and return the completed process.

    ``stdout``/``stderr`` are always ``str`` -- never ``None`` -- and a non-zero
    exit is reported through ``returncode``, not an exception.  Only the two
    conditions the caller cannot act on (``az`` absent, ``az`` hung) raise
    :class:`AuthError`.
    """
    env = dict(os.environ)
    # Keep stdout parseable: suppress az's warning banners and ANSI colour.
    env.setdefault("AZURE_CORE_ONLY_SHOW_ERRORS", "true")
    env.setdefault("AZURE_CORE_NO_COLOR", "true")

    try:
        proc = subprocess.run(
            ["az", *args],
            capture_output=True,
            encoding="utf-8",  # mandatory on Windows; see module docstring
            errors="replace",
            timeout=timeout,
            shell=_ON_WINDOWS,  # az is a .cmd shim there
            stdin=subprocess.DEVNULL,  # az must fail rather than block on a prompt
            env=env,
            check=False,
        )
    except FileNotFoundError as exc:
        raise AuthError(_AZ_MISSING) from exc
    except subprocess.TimeoutExpired as exc:
        raise AuthError(
            f"`az {' '.join(args)}` did not finish within {timeout}s.\n"
            "Check that the Azure CLI works on its own (`az account show`), then retry."
        ) from exc

    if proc.stdout is None:
        proc.stdout = ""
    if proc.stderr is None:
        proc.stderr = ""
    if proc.returncode != 0 and _reports_missing_az(proc.stderr):
        raise AuthError(_AZ_MISSING)
    return proc


def _reports_missing_az(stderr: str) -> bool:
    """True when the *shell*, not az, said the command does not exist."""
    lowered = (stderr or "").lower()
    return (
        "is not recognized as an internal or external" in lowered
        or "command not found" in lowered
        or "cannot find the path" in lowered
    )


def az_json(args: list[str], *, timeout: int = 60) -> dict | list | None:
    """Run ``az <args>`` and parse its stdout as JSON.

    Returns ``None`` when the command failed or printed something unparseable;
    callers that need to explain *why* use :func:`run_az` and read ``stderr``.
    """
    proc = run_az(_with_json_output(args), timeout=timeout)
    if proc.returncode != 0:
        return None
    return _parse_json(proc.stdout)


def _with_json_output(args: list[str]) -> list[str]:
    """Append ``--output json`` unless the caller already chose an output form."""
    if any(a in ("-o", "--output") or a.startswith("--output=") for a in args):
        return list(args)
    return [*args, "--output", "json"]


def _parse_json(text: str) -> dict | list | None:
    body = (text or "").strip().lstrip("﻿")  # az on Windows can emit a BOM
    if not body:
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        pass
    # A stray warning line ahead of the payload: retry from the first bracket.
    starts = [i for i in (body.find("{"), body.find("[")) if i >= 0]
    if not starts:
        return None
    try:
        return json.loads(body[min(starts) :])
    except json.JSONDecodeError:
        return None


# --------------------------------------------------------------------------
# tokens
# --------------------------------------------------------------------------


def token(*, resource: str = DATA_RESOURCE, subscription: str | None = None) -> str:
    """Return a bearer token for *resource*, minting and caching as needed.

    ``FOUNDRY_BEARER`` and ``FOUNDRY_API_KEY`` short-circuit the *data* plane
    only: ARM rejects an inference token, so a management call must always go
    through ``az``.
    """
    if resource in (DATA_RESOURCE, DATA_FALLBACK):
        override = _env_value(BEARER_ENV) or _env_value(API_KEY_ENV)
        if override:
            return override

    key = (resource, subscription)
    now = time.time()
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached is not None and cached.usable(now):
            return cached.value

    value, expires_at, audience = _mint(resource, subscription)
    entry = _CachedToken(value=value, expires_at=expires_at, refresh_at=_refresh_at(expires_at))
    with _CACHE_LOCK:
        _CACHE[key] = entry
        # Remember the audience that actually worked, so a later explicit call
        # for the fallback resource reuses this token instead of re-minting.
        _CACHE[(audience, subscription)] = entry
    return value


def clear_token_cache() -> None:
    """Forget every cached token. Called after a sign-in changes identity."""
    with _CACHE_LOCK:
        _CACHE.clear()


def _env_value(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def _refresh_at(expires_at: float | None) -> float:
    now = time.time()
    if expires_at is None:
        return now + _UNKNOWN_LIFETIME_SECONDS
    remaining = expires_at - now
    if remaining <= 0:
        return now  # already dead: never serve it twice
    return now + remaining * _REFRESH_FRACTION


def _token_args(resource: str, subscription: str | None) -> list[str]:
    args = ["account", "get-access-token", "--resource", resource, "--output", "json"]
    if subscription:
        args += ["--subscription", subscription]
    return args


def _mint(resource: str, subscription: str | None) -> tuple[str, float | None, str]:
    """Mint a token, retrying the data plane against its alternate audience.

    Both data-plane audiences were verified to work; some tenants issue only one
    of them, so a failure against ``ai.azure.com`` is worth one retry against
    ``cognitiveservices.azure.com`` before it becomes the user's problem.
    """
    proc = run_az(_token_args(resource, subscription))
    minted = _read_token(proc)
    if minted is not None:
        return (*minted, resource)

    if resource == DATA_RESOURCE:
        retry = run_az(_token_args(DATA_FALLBACK, subscription))
        minted = _read_token(retry)
        if minted is not None:
            return (*minted, DATA_FALLBACK)

    raise AuthError(_token_error(proc, resource, subscription))


def _read_token(proc: subprocess.CompletedProcess[str]) -> tuple[str, float | None] | None:
    if proc.returncode != 0:
        return None
    data = _parse_json(proc.stdout)
    if not isinstance(data, dict):
        return None
    value = str(data.get("accessToken") or "").strip()
    if not value:
        return None
    return value, _expiry_epoch(data)


def _expiry_epoch(data: dict) -> float | None:
    """Seconds since the epoch at which the token dies, or ``None`` if unclear.

    Newer ``az`` emits ``expires_on`` (a POSIX timestamp); older builds emit only
    ``expiresOn``, a *local*, timezone-naive string.
    """
    raw = data.get("expires_on")
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return float(raw)
    if isinstance(raw, str) and raw.strip().isdigit():
        return float(raw.strip())

    text = data.get("expiresOn")
    if not isinstance(text, str) or not text.strip():
        return None
    stamp = text.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()  # naive means local time, per az
    return parsed.timestamp()


def _token_error(
    proc: subprocess.CompletedProcess[str], resource: str, subscription: str | None
) -> str:
    detail = _first_line(proc.stderr) or _first_line(proc.stdout)
    lowered = f"{proc.stderr} {proc.stdout}".lower()
    where = f" for subscription {subscription}" if subscription else ""

    if "az login" in lowered or "not logged in" in lowered or "please run" in lowered:
        base = f"Not signed in to Azure{where}. Run: az login"
        return f"{base}\n(az said: {detail})" if detail else base

    if subscription and "subscription" in lowered and "not found" in lowered:
        return (
            f"Subscription {subscription} is not available to the signed-in account.\n"
            "List what you can see with `az account list -o table`, or sign in to the tenant "
            "that owns it: az login --tenant <tenant-id>"
        )

    suffix = f"\naz said: {detail}" if detail else ""
    return (
        f"Could not get an Azure token for {resource}{where}.\n"
        "Run `az login` (add `--tenant <tenant-id>` if the resource lives in another tenant) "
        f"and try again.{suffix}"
    )


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


# --------------------------------------------------------------------------
# identity
# --------------------------------------------------------------------------


def account() -> dict | None:
    """``az account show`` -- the active subscription and tenant, or ``None``."""
    data = az_json(["account", "show"])
    return data if isinstance(data, dict) else None


def subscriptions() -> list[dict]:
    """``az account list``, default subscription first.

    Returns ``[]`` when ``az`` cannot answer; the caller decides whether that
    means "not signed in" (see :func:`signed_in`) or "nothing to scan".  The
    default subscription leads because a resource scan that tries it first
    usually stops there.
    """
    data = az_json(["account", "list"], timeout=90)
    if not isinstance(data, list):
        return []
    subs = [item for item in data if isinstance(item, dict)]
    subs.sort(key=lambda s: 0 if s.get("isDefault") else 1)
    return subs


def signed_in() -> bool:
    """True when ``az account show`` works *and* a data-plane token can be minted.

    Both halves matter: an expired refresh token still leaves ``az account show``
    answering happily from the locally cached profile.
    """
    if account() is None:
        return False
    try:
        token()
    except AuthError:
        return False
    return True


def login(*, subscription: str | None = None, device_code: bool = False) -> None:
    """Ensure a usable Azure session exists, signing in only if one does not.

    Re-running ``az login`` over a working session is friction at best and fails
    outright wherever the CLI profile directory is redirected, so a working
    session is left strictly alone (SPEC 3).
    """
    if not signed_in():
        _interactive_login(device_code=device_code)
        clear_token_cache()
        if not signed_in():
            raise AuthError(
                "`az login` finished but no usable session came out of it.\n"
                "Run `az login` yourself (add `--tenant <tenant-id>` if you belong to several "
                "tenants), then re-run foundry."
            )

    if subscription and not _subscription_visible(subscription):
        raise AuthError(
            f"Subscription {subscription} is not visible to the signed-in account.\n"
            "Check the id with `az account list -o table`, or sign in to the tenant that owns "
            "it: az login --tenant <tenant-id>"
        )


def _interactive_login(*, device_code: bool) -> None:
    """Run ``az login`` with the terminal attached.

    Output is deliberately not captured: the browser URL and the device code are
    the whole point, and the user has to be able to read them.
    """
    cmd = ["az", "login", "--output", "none"]
    if device_code:
        cmd.insert(2, "--use-device-code")
    try:
        proc = subprocess.run(
            cmd,
            shell=_ON_WINDOWS,
            timeout=_LOGIN_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError as exc:
        raise AuthError(_AZ_MISSING) from exc
    except subprocess.TimeoutExpired as exc:
        raise AuthError(
            f"`az login` was still waiting after {_LOGIN_TIMEOUT_SECONDS // 60} minutes.\n"
            "Run `az login --use-device-code` in another terminal, then re-run foundry."
        ) from exc

    if proc.returncode != 0:
        raise AuthError(
            f"`az login` exited with status {proc.returncode}.\n"
            "Run `az login` yourself to see the full error, then re-run foundry."
        )


def _subscription_visible(subscription: str) -> bool:
    wanted = subscription.strip().lower()
    for sub in subscriptions():
        known = {str(sub.get("id") or "").lower(), str(sub.get("name") or "").lower()}
        if wanted in known - {""}:
            return True
    return False


# --------------------------------------------------------------------------
# resource API key
# --------------------------------------------------------------------------


def api_key(endpoint: str, subscription: str | None = None) -> str | None:
    """The resource's API key, or ``None`` if it cannot be obtained.

    ``FOUNDRY_API_KEY`` wins outright -- that is the headless escape hatch.
    Otherwise the key is read from ARM through ``az``, which needs the resource
    group, so the account is located first.  Returning ``None`` is a normal
    outcome (the caller falls back to an Entra bearer), not an error.
    """
    override = _env_value(API_KEY_ENV)
    if override:
        return override

    try:
        wanted_root = endpoints.normalize(endpoint)
        wanted_name = endpoints.resource_name(endpoint)
    except ValueError:
        return None

    scope = ["--subscription", subscription] if subscription else []
    listed = az_json(["cognitiveservices", "account", "list", *scope], timeout=120)
    if not isinstance(listed, list):
        return None

    match = _match_account(listed, wanted_root, wanted_name)
    if match is None:
        return None

    name = str(match.get("name") or "")
    group = str(match.get("resourceGroup") or _resource_group_from_id(match.get("id")) or "")
    if not name or not group:
        return None

    keys = az_json(
        [
            "cognitiveservices",
            "account",
            "keys",
            "list",
            "--name",
            name,
            "--resource-group",
            group,
            *scope,
        ]
    )
    if not isinstance(keys, dict):
        return None
    value = keys.get("key1") or keys.get("key2")
    return str(value) if value else None


def _match_account(listed: list, wanted_root: str, wanted_name: str) -> dict | None:
    """Find the account whose name, custom subdomain or endpoint is *wanted*."""
    for item in listed:
        if not isinstance(item, dict):
            continue
        props = item.get("properties")
        props = props if isinstance(props, dict) else {}
        names = {
            str(item.get("name") or "").lower(),
            str(props.get("customSubDomainName") or "").lower(),
        } - {""}
        if wanted_name.lower() in names:
            return item
        published = props.get("endpoint")
        if isinstance(published, str) and published.strip():
            try:
                if endpoints.normalize(published) == wanted_root:
                    return item
            except ValueError:
                continue
    return None


def _resource_group_from_id(resource_id: object) -> str | None:
    """Pull the resource group out of an ARM id, for az builds that omit the field."""
    if not isinstance(resource_id, str):
        return None
    parts = resource_id.split("/")
    for index, part in enumerate(parts):
        if part.lower() == "resourcegroups" and index + 1 < len(parts):
            return parts[index + 1]
    return None


# --------------------------------------------------------------------------
# child environment
# --------------------------------------------------------------------------


def scrubbed_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """A child environment with a stale service principal removed.

    Claude Code's ``DefaultAzureCredential`` tries ``EnvironmentCredential``
    first and, when ``AZURE_CLIENT_ID`` / ``AZURE_CLIENT_SECRET`` /
    ``AZURE_TENANT_ID`` name an expired or foreign-tenant SP, fails the whole
    chain instead of falling through to the Azure CLI credential
    (``AADSTS7000222``, reproduced end to end).  The three are dropped together
    -- a partial set is no use to anything -- unless they are proven to belong
    to the active ``az`` account and to still mint a token.
    """
    env = dict(os.environ if base is None else base)
    if not any(_present(env, name) for name in SP_ENV_VARS):
        return env
    if _sp_env_is_current(env):
        return env
    for name in SP_ENV_VARS:
        env.pop(name, None)
    return env


def _present(env: dict[str, str], name: str) -> bool:
    return bool((env.get(name) or "").strip())


def _sp_env_is_current(env: dict[str, str]) -> bool:
    active = account() or {}
    active_tenant = str(active.get("tenantId") or "").strip()
    if not active_tenant:
        # Cannot verify, so assume not: dropping costs only a credential we did
        # not want, while keeping a bad one costs the whole session.
        return False

    ambient_tenant = (env.get("AZURE_TENANT_ID") or "").strip()
    if ambient_tenant and ambient_tenant.lower() != active_tenant.lower():
        return False

    client_id = (env.get("AZURE_CLIENT_ID") or "").strip()
    secret = (env.get("AZURE_CLIENT_SECRET") or "").strip()
    if not client_id or not secret:
        # Without both halves EnvironmentCredential cannot build a credential at
        # all, so a matching AZURE_TENANT_ID on its own is harmless to keep. A
        # lone client id is not: it also selects a user-assigned managed
        # identity, so it goes.
        return bool(ambient_tenant) and not client_id
    return _sp_token_works(ambient_tenant or active_tenant, client_id, secret)


def _sp_token_works(tenant: str, client_id: str, secret: str) -> bool:
    """Ask Entra whether these client credentials still mint a token.

    This is the same request ``EnvironmentCredential`` would make, so it answers
    the only question that matters: will the agent's credential chain survive?
    Any failure -- expired secret, wrong tenant, no network -- means "drop
    them", which is always safe because the Azure CLI credential remains.
    """
    fingerprint = (tenant.lower(), client_id.lower(), hashlib.sha256(secret.encode()).hexdigest())
    with _SP_PROBE_LOCK:
        cached = _SP_PROBE.get(fingerprint)
    if cached is not None:
        return cached

    authority = (
        os.environ.get("AZURE_AUTHORITY_HOST") or "https://login.microsoftonline.com"
    ).rstrip("/")
    url = f"{authority}/{urllib.parse.quote(tenant)}/oauth2/v2.0/token"
    body = urllib.parse.urlencode(
        {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": secret,
            "scope": f"{DATA_RESOURCE}/.default",
        }
    ).encode()
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            works = response.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        works = False

    with _SP_PROBE_LOCK:
        _SP_PROBE[fingerprint] = works
    return works
