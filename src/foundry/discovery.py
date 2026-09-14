# SPDX-License-Identifier: Apache-2.0
"""Microsoft Foundry resource and deployment discovery (SPEC §4).

Deployments cannot be read from the data plane: ``GET {endpoint}/openai/v1/models``
answers with the *regional catalogue* (~400 entries of what could be deployed), not
with what this resource actually publishes.  The account's real deployments live in
Azure Resource Manager, which needs subscription + resource group + account name --
none of which the endpoint host alone supplies.  So this module scans the
subscriptions visible to ``az`` and matches the resource by its custom subdomain.

The scan is the slow part of a first run, which is why it is concurrent, why it has
an overall deadline, and above all why every subscription that could *not* be
searched is recorded.  Telling a user "no resource found" when the truth is "three
of your five subscriptions refused a token" sends them hunting the wrong problem.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from . import auth, endpoints

# ARM api-version measured live; 2024-10-01 also answers 200, but stick to one.
API_VERSION = "2025-06-01"

#: Only these account kinds serve model inference; everything else in
#: Microsoft.CognitiveServices (Speech, Vision, ContentSafety, ...) is noise.
INFERENCE_KINDS = frozenset({"aiservices", "openai"})

#: Wall-clock budget for a whole multi-subscription scan.  A subscription that has
#: not answered by then is reported as unreachable rather than silently dropped.
SCAN_TIMEOUT = 45.0

#: Per-HTTP-request timeout.  Must stay well under SCAN_TIMEOUT so a single slow
#: subscription cannot consume the entire budget.
HTTP_TIMEOUT = 20.0

MAX_WORKERS = 8

#: Model-name markers for deployments that are not chat models.  Live listings mix
#: speech-to-text and image generation in with chat; without this filter they get
#: offered to the user as models to code against.  Measured on real accounts:
#: whisper, text-embedding-3-small/large, gpt-4o-realtime, FLUX, dall-e.
NON_CHAT_MARKERS: tuple[str, ...] = (
    "embed",
    "embedding",
    "rerank",
    "whisper",
    "transcribe",
    "audio",
    "realtime",
    "tts",
    "image",
    "dall-e",
    "sora",
    "flux",
    "diffusion",
    "moderation",
    "ocr",
)

# A marker only counts at the start of a name segment, so "reimagined" is not an
# image model and "text-embedding-3-small" still matches "embed".
_NON_CHAT_RE = re.compile(
    r"(?:^|[^a-z0-9])(?:" + "|".join(re.escape(m) for m in NON_CHAT_MARKERS) + r")"
)

_ANTHROPIC_FAMILIES = ("opus", "sonnet", "haiku")

# Name-only hints, used solely when properties.model.format is missing or unknown.
_OPENAI_NAME_RE = re.compile(r"(?:^|[^a-z0-9])(gpt|model-router|o[1-9](?:-|$)|davinci|codex)")

#: Version-ish number runs: "5.4" stays one segment, "4-7" becomes two.
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)*")

#: How many version components sort_key compares before falling back to the name.
_VERSION_SLOTS = 4


@dataclass(frozen=True)
class Deployment:
    """One published model on a Foundry account.

    ``name`` is the value that goes in a request's ``model`` field.  It is chosen
    freely by whoever published the deployment and is *not* necessarily the model's
    name -- which is exactly why ``model``/``fmt`` are carried alongside it and why
    classification never guesses from ``name`` when ARM metadata is available.
    """

    name: str
    model: str
    fmt: str
    version: str
    sku: str


@dataclass(frozen=True)
class Account:
    """A Cognitive Services / Foundry account located in ARM."""

    subscription: str
    resource_group: str
    name: str
    endpoint: str
    location: str


@dataclass(frozen=True)
class ScanReport:
    """What a subscription scan actually managed to look at.

    ``unreachable`` maps subscription id -> why it could not be searched (token mint
    refused, 403, timed out).  It is the difference between "your resource is not
    there" and "we never got to look".
    """

    searched: tuple[str, ...]
    unreachable: dict[str, str]
    names: dict[str, str]

    def describe(self) -> str:
        """Render the searched/unreachable list for an error message."""
        lines: list[str] = []
        for sub in self.searched:
            label = self.names.get(sub, sub)
            reason = self.unreachable.get(sub)
            if reason:
                lines.append(f"  - {label} [{sub}] -- NOT searched: {reason}")
            else:
                lines.append(f"  - {label} [{sub}]")
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# HTTP                                                                          #
# --------------------------------------------------------------------------- #


def _get(url: str, headers: dict[str, str], *, timeout: float | None = None) -> tuple[int, bytes]:
    """GET a URL, returning (status, body) instead of raising on 4xx/5xx.

    Callers need the status code to produce the actionable message SPEC §9 demands,
    so an HTTPError is unwrapped rather than propagated.  The timeout is read from
    the module constant at call time so a test can shorten it.
    """
    timeout = HTTP_TIMEOUT if timeout is None else timeout
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return int(response.status), response.read()
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read()
        except Exception:  # pragma: no cover - body already consumed
            body = b""
        return int(exc.code), body


def _arm_pages(url: str, token: str) -> Iterator[dict]:
    """Yield every ``value[]`` item of an ARM list, following ``nextLink``.

    ARM pages silently at 100-ish items; a resource on page two is invisible to a
    single-GET implementation.
    """
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    seen: set[str] = set()
    while url and url not in seen:
        seen.add(url)
        status, body = _get(url, headers)
        if status != 200:
            raise auth.AuthError(_arm_failure(status, url, body))
        try:
            payload = json.loads(body.decode("utf-8", "replace") or "{}")
        except json.JSONDecodeError as exc:
            raise auth.AuthError(
                f"Azure Resource Manager returned unparseable JSON for {url}: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            return
        for item in payload.get("value") or []:
            if isinstance(item, dict):
                yield item
        url = payload.get("nextLink") or ""


def _arm_failure(status: int, url: str, body: bytes) -> str:
    """Turn an ARM status code into a sentence that names the fix."""
    detail = _error_message(body)
    if status in (401, 403):
        return (
            f"Azure Resource Manager refused this request ({status}). "
            "Your account can sign in but is not authorised to read Cognitive Services "
            "resources here -- ask for the 'Reader' role on the subscription or "
            "resource group." + (f" Azure said: {detail}" if detail else "")
        )
    if status == 404:
        return f"Azure Resource Manager returned 404 for {url}. The resource group or account name is wrong."
    if status == 429:
        return "Azure Resource Manager is throttling this client (429). Wait a moment and retry."
    return f"Azure Resource Manager returned HTTP {status} for {url}." + (
        f" Azure said: {detail}" if detail else ""
    )


def _error_message(body: bytes) -> str:
    """Best-effort extraction of ARM's own error text."""
    try:
        payload = json.loads(body.decode("utf-8", "replace"))
    except Exception:
        return ""
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str):
                return message.strip()
        if isinstance(error, str):
            return error.strip()
        message = payload.get("message")
        if isinstance(message, str):
            return message.strip()
    return ""


# --------------------------------------------------------------------------- #
# Subscription scan                                                             #
# --------------------------------------------------------------------------- #


def _subscription_index(explicit: str | None) -> tuple[list[str], dict[str, str]]:
    """Return (subscription ids to scan, id -> display name).

    An explicit ``--subscription`` short-circuits the whole scan; that is the entire
    point of the flag we tell users about when the scan fails.
    """
    listed = auth.subscriptions() or []
    names: dict[str, str] = {}
    ordered: list[str] = []
    for entry in listed:
        if not isinstance(entry, dict):
            continue
        sub_id = str(entry.get("id") or "").strip()
        if not sub_id:
            continue
        names[sub_id] = str(entry.get("name") or sub_id)
        state = str(entry.get("state") or "Enabled")
        if state.lower() != "enabled":
            continue
        if sub_id not in ordered:
            ordered.append(sub_id)

    if explicit:
        wanted = explicit.strip()
        # Accept a display name as well as an id: users copy either one.
        for sub_id, name in names.items():
            if name.lower() == wanted.lower():
                wanted = sub_id
                break
        names.setdefault(wanted, wanted)
        return [wanted], names

    if not ordered:
        raise auth.AuthError(
            "No enabled Azure subscriptions are visible to the Azure CLI. "
            "Run: az login   (then `az account list --output table` to confirm)."
        )
    return ordered, names


def _accounts_in_subscription(subscription: str) -> list[Account]:
    """List the inference-capable accounts of one subscription."""
    token = auth.token(resource=auth.ARM_RESOURCE, subscription=subscription)
    url = (
        f"{auth.ARM_RESOURCE.rstrip('/')}/subscriptions/{urllib.parse.quote(subscription, safe='')}"
        f"/providers/Microsoft.CognitiveServices/accounts?api-version={API_VERSION}"
    )
    found: list[Account] = []
    for item in _arm_pages(url, token):
        account = _account_from_arm(item, subscription)
        if account is not None:
            found.append(account)
    return found


def _account_from_arm(item: dict, subscription: str) -> Account | None:
    """Build an Account from one ARM ``accounts`` entry, or None if it cannot serve models."""
    kind = str(item.get("kind") or "").strip().lower()
    if kind and kind not in INFERENCE_KINDS:
        return None
    name = str(item.get("name") or "").strip()
    if not name:
        return None
    properties = item.get("properties") if isinstance(item.get("properties"), dict) else {}
    raw = _account_endpoint(properties, name)
    try:
        endpoint = endpoints.normalize(raw)
    except Exception:
        # A resource with no usable endpoint cannot be launched against; skip it
        # rather than surfacing a half-built Account to the picker.
        return None
    return Account(
        subscription=subscription,
        resource_group=_resource_group(str(item.get("id") or "")),
        name=name,
        endpoint=endpoint,
        location=str(item.get("location") or ""),
    )


def _account_endpoint(properties: dict, name: str) -> str:
    """Pick the endpoint to normalise, preferring the AI Foundry host."""
    endpoint_map = properties.get("endpoints")
    if isinstance(endpoint_map, dict):
        for key in ("AI Foundry API", "Azure AI Model Inference API"):
            value = endpoint_map.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    subdomain = properties.get("customSubDomainName")
    if isinstance(subdomain, str) and subdomain.strip():
        return subdomain.strip()
    endpoint = properties.get("endpoint")
    if isinstance(endpoint, str) and endpoint.strip():
        return endpoint.strip()
    return name


def _resource_group(resource_id: str) -> str:
    """Pull the resource group out of an ARM resource id (ARM does not field it separately)."""
    match = re.search(r"/resourceGroups/([^/]+)", resource_id, re.IGNORECASE)
    return match.group(1) if match else ""


def _scan(
    subscription: str | None,
    *,
    timeout: float | None = None,
    stop_when: Callable[[Account], bool] | None = None,
) -> tuple[list[Account], ScanReport]:
    """Scan subscriptions concurrently, under one overall deadline.

    Returns everything found plus a report of what could not be searched.  Failures
    are collected, never raised: one dead subscription must not hide the resource
    sitting in the next one.
    """
    timeout = SCAN_TIMEOUT if timeout is None else timeout
    subs, names = _subscription_index(subscription)
    found: list[Account] = []
    unreachable: dict[str, str] = {}

    pool = ThreadPoolExecutor(max_workers=max(1, min(MAX_WORKERS, len(subs))))
    futures = {pool.submit(_accounts_in_subscription, sub): sub for sub in subs}
    deadline = time.monotonic() + timeout
    try:
        for future in as_completed(futures, timeout=max(0.1, deadline - time.monotonic())):
            sub = futures[future]
            try:
                accounts = future.result()
            except auth.AuthError as exc:
                unreachable[sub] = str(exc)
                continue
            except Exception as exc:  # network, DNS, TLS, malformed payload
                unreachable[sub] = f"{type(exc).__name__}: {exc}"
                continue
            found.extend(accounts)
            if stop_when is not None and any(stop_when(a) for a in accounts):
                break
    except TimeoutError:
        pass
    finally:
        for future, sub in futures.items():
            if not future.done():
                future.cancel()
                unreachable.setdefault(
                    sub, f"timed out after {timeout:.0f}s (pass --subscription to search just one)"
                )
        # Do not block on threads whose HTTP call is still in flight; each one is
        # already bounded by HTTP_TIMEOUT.
        pool.shutdown(wait=False, cancel_futures=True)

    report = ScanReport(searched=tuple(subs), unreachable=unreachable, names=names)
    return found, report


# --------------------------------------------------------------------------- #
# Public discovery API                                                          #
# --------------------------------------------------------------------------- #


def scan_accounts(subscription: str | None = None) -> tuple[list[Account], ScanReport]:
    """Like :func:`list_accounts`, but also returns what could not be searched.

    Exposed because a picker that lists two resources should be able to say "and
    one subscription was unreachable" instead of pretending the list is complete.
    """
    accounts, report = _scan(subscription)
    return _dedupe(accounts), report


def list_accounts(subscription: str | None = None) -> list[Account]:
    """Every Foundry/OpenAI account visible to the signed-in identity."""
    return scan_accounts(subscription)[0]


def find_account(endpoint: str, subscription: str | None = None) -> Account | None:
    """Locate the ARM account behind an endpoint.

    Raises :class:`auth.AuthError` when the account cannot be located, with a message
    that lists every subscription searched and every subscription that could not be
    searched, and points at ``--subscription``.  It never returns ``None`` for a
    failed lookup: "not found" and "we could not look" are different answers, and
    only an exception can carry the difference.  The ``| None`` in the signature is
    kept for interface compatibility.
    """
    root = endpoints.normalize(endpoint)
    target = endpoints.resource_name(root).lower()

    def matches(account: Account) -> bool:
        return (
            account.name.lower() == target
            or endpoints.resource_name(account.endpoint).lower() == target
        )

    accounts, report = _scan(subscription, stop_when=matches)
    for account in _dedupe(accounts):
        if matches(account):
            return account

    raise auth.AuthError(_not_found_message(target, root, report))


def _not_found_message(target: str, root: str, report: ScanReport) -> str:
    """SPEC §9: name what was searched, what was not, and the flag that fixes it."""
    blocked = [s for s in report.searched if s in report.unreachable]
    head = f"No Microsoft Foundry resource named '{target}' ({root}) was found."
    body = [
        head,
        "",
        f"Searched {len(report.searched)} subscription(s):",
        report.describe(),
    ]
    if blocked:
        body += [
            "",
            f"{len(blocked)} of them could NOT be searched, so the resource may well exist "
            "in one of those. Fix the access problem above, or search the right one directly:",
        ]
    else:
        body += [
            "",
            "All of them answered, and none of them holds that resource. Check the endpoint "
            "host for a typo, or search a subscription this identity cannot list:",
        ]
    body += [
        "  foundry configure --subscription <subscription-id> --endpoint <endpoint>",
        "  (az account list --output table lists the subscriptions you can see)",
    ]
    return "\n".join(body)


def _dedupe(accounts: list[Account]) -> list[Account]:
    """Drop duplicates while keeping discovery order stable for pickers."""
    seen: set[tuple[str, str, str]] = set()
    unique: list[Account] = []
    for account in accounts:
        key = (account.subscription, account.resource_group.lower(), account.name.lower())
        if key in seen:
            continue
        seen.add(key)
        unique.append(account)
    return unique


def deployments(account: Account) -> list[Deployment]:
    """List the account's published deployments, newest first.

    Only ``provisioningState == Succeeded`` survives: a Creating or Failed deployment
    answers 404 at inference time, which would look like a `foundry` bug.
    Non-chat deployments are *kept* here -- filtering is :func:`is_chat_model`'s job,
    so a caller that wants the whole picture can still have it.
    """
    if not account.resource_group:
        raise auth.AuthError(
            f"Account '{account.name}' has no resource group recorded, so its deployments "
            "cannot be listed. Re-run `foundry configure` to rediscover it."
        )
    token = auth.token(resource=auth.ARM_RESOURCE, subscription=account.subscription)
    url = (
        f"{auth.ARM_RESOURCE.rstrip('/')}"
        f"/subscriptions/{urllib.parse.quote(account.subscription, safe='')}"
        f"/resourceGroups/{urllib.parse.quote(account.resource_group, safe='')}"
        f"/providers/Microsoft.CognitiveServices/accounts/{urllib.parse.quote(account.name, safe='')}"
        f"/deployments?api-version={API_VERSION}"
    )
    found: list[Deployment] = []
    for item in _arm_pages(url, token):
        deployment = _deployment_from_arm(item)
        if deployment is not None:
            found.append(deployment)
    found.sort(key=lambda d: sort_key(d.model or d.name))
    return found


def _deployment_from_arm(item: dict) -> Deployment | None:
    """Build a Deployment from one ARM ``deployments`` entry."""
    name = str(item.get("name") or "").strip()
    if not name:
        return None
    properties = item.get("properties") if isinstance(item.get("properties"), dict) else {}
    state = str(properties.get("provisioningState") or "Succeeded")
    if state.lower() != "succeeded":
        return None
    model = properties.get("model") if isinstance(properties.get("model"), dict) else {}
    sku = item.get("sku") if isinstance(item.get("sku"), dict) else {}
    return Deployment(
        name=name,
        model=str(model.get("name") or ""),
        fmt=str(model.get("format") or ""),
        version=str(model.get("version") or ""),
        sku=str(sku.get("name") or properties.get("skuName") or ""),
    )


# --------------------------------------------------------------------------- #
# Classification and ordering                                                   #
# --------------------------------------------------------------------------- #


def is_chat_model(model_name: str) -> bool:
    """False for deployments that cannot serve a coding agent.

    Real accounts publish whisper, text-embedding-3-*, gpt-4o-realtime, FLUX and
    dall-e beside their chat models; offered in a model picker they look like valid
    choices and fail only at the first request.
    """
    name = (model_name or "").strip().lower()
    if not name:
        return False
    return _NON_CHAT_RE.search(name) is None


def family(d: Deployment) -> str | None:
    """Classify a deployment: ``opus``/``sonnet``/``haiku``/``openai``/``other``.

    Driven by ``properties.model.format`` first, because a deployment name is chosen
    freely by whoever published it -- "prod-fast" says nothing, while format
    ``Anthropic`` is authoritative.  The name is consulted only when the format is
    missing or unrecognised.  ``None`` means "not a chat model".
    """
    model_name = (d.model or d.name or "").strip().lower()
    if not is_chat_model(model_name):
        return None

    fmt = (d.fmt or "").strip().lower()
    if fmt == "anthropic":
        return _anthropic_family(model_name) or _anthropic_family(d.name.lower()) or "other"
    if fmt == "openai":
        return "openai"
    if fmt:
        # A known-but-different vendor (Cohere, Mistral AI, Meta, ...). Still chat.
        guess = _family_from_name(model_name) or _family_from_name(d.name.lower())
        return guess or "other"

    # No format at all: name matching is the only signal left.
    return _family_from_name(model_name) or _family_from_name(d.name.lower()) or "other"


def _anthropic_family(name: str) -> str | None:
    for tier in _ANTHROPIC_FAMILIES:
        if tier in name:
            return tier
    return None


def _family_from_name(name: str) -> str | None:
    """Name-only classification; used strictly as a fallback to the ARM format."""
    if not name:
        return None
    tier = _anthropic_family(name)
    if tier:
        return tier
    if "claude" in name:
        return "other"
    if _OPENAI_NAME_RE.search(name):
        return "openai"
    return None


def sort_key(name: str) -> tuple:
    """Sort key placing the newest model first under plain ``sorted()``.

    Version segments appear in both dotted and dashed form on the same account --
    ``gpt-5.4-nano`` and ``claude-opus-4-7`` -- so both are parsed into the same
    numeric tuple.  A comparator that understands only dashes ranks ``gpt-4.1``
    above ``gpt-5.4`` and hands the user the oldest model as their default.

    Components are negated so ascending order is newest-first, padded to a fixed
    width so ``gpt-4.1`` (4, 1) beats ``gpt-4`` (4, 0), and preceded by a flag that
    pushes unversioned names (``model-router``) to the end rather than the front.
    """
    text = (name or "").strip().lower()
    numbers: list[int] = []
    for run in _NUMBER_RE.findall(text):
        for part in run.split("."):
            if part:
                numbers.append(int(part))
    padded = (numbers + [0] * _VERSION_SLOTS)[:_VERSION_SLOTS]
    return (0 if numbers else 1, tuple(-n for n in padded), text)


# --------------------------------------------------------------------------- #
# Reachability                                                                  #
# --------------------------------------------------------------------------- #


def reachable(endpoint: str) -> None:
    """Prove the endpoint exists and this identity may call it.

    Uses ``GET {endpoint}/openai/v1/models``, which answers the *regional catalogue*
    rather than this account's deployments -- useless as a model list, but the
    cheapest honest proof of reachability and authorisation.  Raises
    :class:`auth.AuthError` with the SPEC §9 message for the observed status.
    """
    root = endpoints.normalize(endpoint)
    url = f"{endpoints.openai_base(root)}/models"

    key = os.environ.get("FOUNDRY_API_KEY", "").strip()
    if key:
        # Headless escape hatch: a resource key, no `az` involved.
        headers = {"api-key": key, "Authorization": f"Bearer {key}", "Accept": "application/json"}
    else:
        headers = {"Authorization": f"Bearer {auth.token()}", "Accept": "application/json"}

    try:
        status, body = _get(url, headers)
    except urllib.error.URLError as exc:
        raise auth.AuthError(
            f"Could not reach {root}: {exc.reason}. "
            "Check the endpoint host and your network/proxy settings."
        ) from exc
    except TimeoutError as exc:
        raise auth.AuthError(
            f"Timed out after {HTTP_TIMEOUT:.0f}s reaching {root}. "
            "Check the endpoint host and your network/proxy settings."
        ) from exc

    if status == 200:
        return

    detail = _error_message(body)
    # Own line: these messages are multi-line command recipes, and Azure's own text
    # tacked onto the end of one would read as part of the command.
    suffix = f"\n  Azure said: {detail}" if detail else ""

    if status == 401:
        raise auth.AuthError(
            f"{root} rejected the credential (401). Your Azure sign-in is missing or expired.\n"
            "  Run: az login" + suffix
        )
    if status == 403:
        raise auth.AuthError(
            f"{root} refused this identity (403). You are signed in but lack data-plane access.\n"
            "  Grant the 'Azure AI User' role on the resource:\n"
            "    az role assignment create --assignee <your-upn> \\\n"
            f'      --role "Azure AI User" --scope <resource-id-of-{endpoints.resource_name(root)}>'
            + suffix
        )
    if status == 404:
        raise auth.AuthError(
            f"{url} returned 404 -- this is not a Microsoft Foundry endpoint.\n"
            "  Check the host: it should be https://<resource>.services.ai.azure.com" + suffix
        )
    if status == 429:
        raise auth.AuthError(
            f"{root} is throttling this client (429). Wait a moment and retry." + suffix
        )
    raise auth.AuthError(f"{url} returned HTTP {status}." + suffix)
