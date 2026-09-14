# SPDX-License-Identifier: Apache-2.0
"""Endpoint normalisation and route construction.

Azure publishes the same Foundry account under several hostnames
(``services.ai.azure.com``, the legacy ``openai.azure.com``, the
``cognitiveservices.azure.com`` one) and users habitually paste whichever the
portal showed them -- or a *project* endpoint, which carries an
``/api/projects/<project>`` suffix that serves no inference.  Everything here
collapses those forms onto the single resource root that SPEC 2 mandates, so
that no other module ever has to ask "which shape is this?".
"""

from __future__ import annotations

import re

#: The only host family inference URLs are built from (foundry-api-notes, "Endpoint shapes").
RESOURCE_SUFFIX = "services.ai.azure.com"

#: Host suffixes that denote the *same* Cognitive Services account as
#: ``<name>.services.ai.azure.com``.  Anything else is left alone, so a
#: sovereign cloud or a custom CNAME survives normalisation intact.
_ALIAS_SUFFIXES: tuple[str, ...] = (
    RESOURCE_SUFFIX,
    "openai.azure.com",
    "cognitiveservices.azure.com",
    "inference.ai.azure.com",
    "api.cognitive.microsoft.com",
)

_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://")

#: Azure resource names are alphanumerics and hyphens.  Used only to reject
#: obvious junk ("what?", "C:\path") before it becomes a bogus URL.
_BARE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9\-]*$")

_HINT = "Pass the resource name or its endpoint URL, e.g. `--endpoint my-resource` or `--endpoint https://my-resource.services.ai.azure.com`."


def normalize(value: str) -> str:
    """Return the canonical resource root for *value*, without a trailing slash.

    Accepts a bare resource name, any of the account's published hosts, a full
    URL with or without scheme, and a project endpoint.  Raises ``ValueError``
    with an actionable message when *value* cannot be an endpoint at all.
    """
    raw = (value or "").strip().strip("\"'").strip()
    if not raw:
        raise ValueError(f"No Foundry endpoint given. {_HINT}")

    host = _SCHEME_RE.sub("", raw).lstrip("/")  # also swallows a protocol-relative //host
    # Drop path (a project endpoint lands here), query and fragment, remembering
    # what came off: it is what tells a resource name apart from a typo.
    trailing = ""
    for sep in ("/", "?", "#"):
        if sep in host:
            host, rest = host.split(sep, 1)
            trailing += sep + rest
    if "@" in host:  # user:pass@host
        host = host.rsplit("@", 1)[1]
    port = ""
    if ":" in host:
        host, port = host.split(":", 1)
    host = host.strip().rstrip(".").lower()

    if not host:
        raise ValueError(f"{value!r} is not a Foundry endpoint. {_HINT}")

    if "." not in host:
        # A bare resource name is the only dotless form there is, and it has to be
        # the WHOLE input. `what?`, `localhost:8080` and `C:\path\to\thing` each
        # reduce to a plausible-looking label once the query/port/path is stripped;
        # turning those into `https://what.services.ai.azure.com` would hide a typo
        # behind a DNS failure several steps later instead of naming it here.
        if port or trailing.strip("/") or not _BARE_NAME_RE.match(host):
            raise ValueError(f"{value!r} is not a Foundry endpoint or resource name. {_HINT}")
        return f"https://{host}.{RESOURCE_SUFFIX}"

    for suffix in _ALIAS_SUFFIXES:
        if host.endswith("." + suffix):
            name = host.split(".", 1)[0]
            if not name:
                raise ValueError(f"{value!r} is not a Foundry endpoint. {_HINT}")
            return f"https://{name}.{RESOURCE_SUFFIX}"

    # Unknown host: keep it verbatim rather than inventing a rewrite.  A private
    # DNS name or a non-public cloud is more likely than a typo we can fix.
    return f"https://{host}"


def resource_name(endpoint: str) -> str:
    """The account name -- the first host label, and ARM's ``customSubDomainName``."""
    return normalize(endpoint).removeprefix("https://").split(".", 1)[0]


def anthropic_base(endpoint: str) -> str:
    """Anthropic Messages route. Clients append ``/v1/messages`` (SPEC 5)."""
    return f"{normalize(endpoint)}/anthropic"


def openai_base(endpoint: str) -> str:
    """OpenAI route. Clients append ``/responses`` or ``/chat/completions``.

    Versionless on purpose: no Foundry route takes an ``api-version`` query
    parameter (foundry-api-notes, "Inference routes").
    """
    return f"{normalize(endpoint)}/openai/v1"
