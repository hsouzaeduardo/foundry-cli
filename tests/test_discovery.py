# SPDX-License-Identifier: Apache-2.0
"""Unit tests for :mod:`foundry.discovery` (SPEC sections 4 and 9).

Nothing here touches the network, the Azure CLI or the developer's home
directory: ``discovery._get`` is replaced with an in-memory ARM, ``auth.token``
and ``auth.subscriptions`` are stubbed, and ``FOUNDRY_HOME`` is redirected into
``tmp_path``.
"""

from __future__ import annotations

import json

import pytest

from foundry import auth, discovery
from foundry.discovery import Account, Deployment

# --------------------------------------------------------------------------- #
# Isolation                                                                     #
# --------------------------------------------------------------------------- #

#: Ambient variables that would otherwise leak the developer's machine into a
#: test run. Every one of these steers production code.
_LEAKY_ENV = (
    "FOUNDRY_BEARER",
    "FOUNDRY_API_KEY",
    "FOUNDRY_HOME",
    "FOUNDRY_EXECUTABLE",
    "AZURE_CLIENT_ID",
    "AZURE_CLIENT_SECRET",
    "AZURE_TENANT_ID",
    "AZURE_AUTHORITY_HOST",
    "NO_COLOR",
)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Redirect ``~/.foundry`` into tmp_path and delete ambient state."""
    for name in _LEAKY_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("FOUNDRY_HOME", str(tmp_path / "dot-foundry"))
    auth.clear_token_cache()

    def no_subprocess(*args, **kwargs):  # pragma: no cover - only runs on a bug
        raise AssertionError("a test tried to run the Azure CLI")

    monkeypatch.setattr(auth, "run_az", no_subprocess)
    yield
    auth.clear_token_cache()


# --------------------------------------------------------------------------- #
# sort_key -- SPEC section 4, "Ordering"                                        #
# --------------------------------------------------------------------------- #

#: (newer, older). Ascending ``sort_key`` order must place *newer* first.
#: The dotted/dashed mix is the whole point: a dash-only comparator reads
#: ``gpt-5.4-nano`` as version 5 and ranks ``gpt-4.1`` -- read as (4, 1) -- above
#: it, which silently makes the oldest model on the resource the default.
NEWER_FIRST: tuple[tuple[str, str], ...] = (
    # The two cases the spec names explicitly.
    ("gpt-5.4-nano", "gpt-4.1"),
    ("claude-opus-4-7", "claude-opus-4-6"),
    # Dotted against dotted.
    ("gpt-5.4", "gpt-5.3"),
    ("gpt-4.1", "gpt-4"),
    ("gpt-4.1-mini", "gpt-4.0-mini"),
    # Dashed against dashed.
    ("claude-sonnet-4-6", "claude-sonnet-4-5"),
    ("claude-opus-4-7", "claude-opus-3-9"),
    # Dotted against dashed, same family: 5.4 beats 4.7 whichever separator.
    ("gpt-5.4-nano", "gpt-4-7-nano"),
    ("claude-opus-5-0", "claude-opus-4.9"),
    # A larger major beats a longer minor run.
    ("gpt-5", "gpt-4.9.9"),
    # A versioned name beats an unversioned one, so `model-router` never takes
    # the default slot away from a real model.
    ("gpt-4", "model-router"),
    ("claude-opus-4-7", "model-router"),
)


@pytest.mark.parametrize(("newer", "older"), NEWER_FIRST)
def test_sort_key_orders_newest_first(newer: str, older: str) -> None:
    assert discovery.sort_key(newer) < discovery.sort_key(older)
    assert sorted([older, newer], key=discovery.sort_key) == [newer, older]


def test_sort_key_parses_dotted_and_dashed_into_the_same_tuple() -> None:
    """``5.4`` and ``5-4`` are the same version, expressed two ways."""
    assert discovery.sort_key("gpt-5.4-nano")[1] == discovery.sort_key("gpt-5-4-nano")[1]
    assert discovery.sort_key("claude-opus-4-7")[1] == discovery.sort_key("claude-opus-4.7")[1]


def test_sort_key_exact_shape() -> None:
    """The key is (unversioned flag, negated version slots, name)."""
    assert discovery.sort_key("gpt-5.4-nano") == (0, (-5, -4, 0, 0), "gpt-5.4-nano")
    assert discovery.sort_key("claude-opus-4-7") == (0, (-4, -7, 0, 0), "claude-opus-4-7")
    assert discovery.sort_key("model-router") == (1, (0, 0, 0, 0), "model-router")


def test_sort_key_is_case_and_whitespace_insensitive() -> None:
    assert discovery.sort_key("  GPT-5.4-Nano  ") == discovery.sort_key("gpt-5.4-nano")


def test_sort_key_sorts_a_realistic_catalogue() -> None:
    catalogue = [
        "gpt-4.1",
        "model-router",
        "gpt-5.4-nano",
        "claude-opus-4-6",
        "gpt-5.4",
        "claude-opus-4-7",
    ]
    assert sorted(catalogue, key=discovery.sort_key) == [
        "gpt-5.4",
        "gpt-5.4-nano",
        "claude-opus-4-7",
        "claude-opus-4-6",
        "gpt-4.1",
        "model-router",
    ]


def test_sort_key_of_an_empty_name_does_not_raise() -> None:
    assert discovery.sort_key("") == (1, (0, 0, 0, 0), "")


# --------------------------------------------------------------------------- #
# is_chat_model -- SPEC section 4, the non-chat exclusion list                  #
# --------------------------------------------------------------------------- #

#: Every marker the spec lists, in a name a real account actually publishes.
NON_CHAT_NAMES: tuple[tuple[str, str], ...] = (
    ("embed", "text-embedding-ada-002"),
    ("embedding", "text-embedding-3-large"),
    ("rerank", "cohere-rerank-v3.5"),
    ("whisper", "whisper-1"),
    ("transcribe", "gpt-4o-transcribe"),
    ("audio", "gpt-4o-audio-preview"),
    ("realtime", "gpt-4o-realtime-preview"),
    ("tts", "tts-1-hd"),
    ("image", "gpt-image-1"),
    ("dall-e", "dall-e-3"),
    ("sora", "sora-2"),
    ("flux", "FLUX.1-Kontext-pro"),
    ("diffusion", "stable-diffusion-3.5-large"),
    ("moderation", "text-moderation-latest"),
    ("ocr", "mistral-ocr-2503"),
)


@pytest.mark.parametrize(("marker", "model_name"), NON_CHAT_NAMES)
def test_is_chat_model_excludes_every_documented_marker(marker: str, model_name: str) -> None:
    assert marker in discovery.NON_CHAT_MARKERS
    assert discovery.is_chat_model(model_name) is False


def test_the_marker_list_is_exactly_the_one_the_spec_names() -> None:
    assert set(discovery.NON_CHAT_MARKERS) == {marker for marker, _ in NON_CHAT_NAMES}


#: Real chat deployments. Every one of these must survive the filter -- an
#: over-eager exclusion hides the only model on the resource.
CHAT_NAMES: tuple[str, ...] = (
    "gpt-5.4",
    "gpt-5.4-mini",
    "gpt-5.4-nano",
    "gpt-4.1",
    "gpt-4o",
    "gpt-4o-mini",
    "o3-mini",
    "o4-mini",
    "model-router",
    "claude-opus-4-7",
    "claude-sonnet-4-6",
    "claude-haiku-4-5",
    "Llama-3.3-70B-Instruct",
    "Cohere-command-r-plus-08-2024",
    "mistral-large-2411",
    "DeepSeek-R1",
    "grok-3",
    "Phi-4-reasoning",
    "codex-mini",
)


@pytest.mark.parametrize("model_name", CHAT_NAMES)
def test_is_chat_model_keeps_real_chat_models(model_name: str) -> None:
    assert discovery.is_chat_model(model_name) is True


def test_a_marker_only_counts_at_a_segment_boundary() -> None:
    """ "reimagined" contains "image" but is not an image model."""
    assert discovery.is_chat_model("reimagined-chat") is True
    assert discovery.is_chat_model("gpt-image-1") is False


@pytest.mark.parametrize("model_name", ["", "   ", None])
def test_is_chat_model_rejects_an_empty_name(model_name) -> None:
    assert discovery.is_chat_model(model_name) is False


def test_is_chat_model_is_case_insensitive() -> None:
    assert discovery.is_chat_model("Text-Embedding-3-Large") is False
    assert discovery.is_chat_model("Whisper-1") is False


# --------------------------------------------------------------------------- #
# family -- driven by the ARM `fmt` field, name only as a fallback              #
# --------------------------------------------------------------------------- #


def dep(name: str, model: str = "", fmt: str = "", version: str = "1", sku: str = "") -> Deployment:
    return Deployment(name=name, model=model, fmt=fmt, version=version, sku=sku)


#: (deployment, expected family). The deployment *name* is chosen freely by
#: whoever published it, so several rows deliberately give it a name that
#: contradicts the format: the format must win.
FAMILY_CASES: tuple[tuple[Deployment, str | None], ...] = (
    # -- format is authoritative --------------------------------------------
    (dep("prod-fast", model="claude-opus-4-7", fmt="Anthropic"), "opus"),
    (dep("prod-fast", model="claude-sonnet-4-6", fmt="Anthropic"), "sonnet"),
    (dep("prod-fast", model="claude-haiku-4-5", fmt="Anthropic"), "haiku"),
    (dep("gpt-fast", model="claude-sonnet-4-6", fmt="Anthropic"), "sonnet"),
    (dep("claude-prod", model="gpt-5.4", fmt="OpenAI"), "openai"),
    (dep("anything", model="gpt-4.1", fmt="OpenAI"), "openai"),
    (dep("anything", model="model-router", fmt="OpenAI"), "openai"),
    # Case and stray whitespace in the format field must not matter.
    (dep("x", model="claude-opus-4-7", fmt="anthropic"), "opus"),
    (dep("x", model="gpt-5.4", fmt="openai"), "openai"),
    (dep("x", model="claude-opus-4-7", fmt="  Anthropic  "), "opus"),
    # -- a known-but-different vendor: still chat, family "other" ------------
    (dep("x", model="Cohere-command-r-plus-08-2024", fmt="Cohere"), "other"),
    (dep("x", model="mistral-large-2411", fmt="Mistral AI"), "other"),
    (dep("x", model="Llama-3.3-70B-Instruct", fmt="Meta"), "other"),
    (dep("x", model="DeepSeek-R1", fmt="DeepSeek"), "other"),
    # ... unless the name betrays a family the caller cares about.
    (dep("x", model="claude-sonnet-4-6", fmt="SomeReseller"), "sonnet"),
    (dep("x", model="gpt-5.4", fmt="SomeReseller"), "openai"),
    # -- no format at all: name matching is the only signal left -------------
    (dep("x", model="gpt-4.1"), "openai"),
    (dep("x", model="o3-mini"), "openai"),
    (dep("x", model="model-router"), "openai"),
    (dep("x", model="codex-mini"), "openai"),
    (dep("x", model="claude-opus-4-7"), "opus"),
    (dep("x", model="claude-haiku-4-5"), "haiku"),
    (dep("x", model="claude-4-experimental"), "other"),
    (dep("x", model="grok-3"), "other"),
    (dep("x", model="Llama-3.3-70B-Instruct"), "other"),
    # No model name either: fall back to the deployment name.
    (dep("gpt-5.4-nano"), "openai"),
    (dep("claude-opus-4-7"), "opus"),
    # -- not a chat model at all --------------------------------------------
    (dep("embeddings", model="text-embedding-3-large", fmt="OpenAI"), None),
    (dep("stt", model="whisper-1", fmt="OpenAI"), None),
    (dep("pictures", model="dall-e-3", fmt="OpenAI"), None),
    (dep("flux-prod", model="FLUX.1-Kontext-pro", fmt="BlackForestLabs"), None),
    (dep(""), None),
)


@pytest.mark.parametrize(("deployment", "expected"), FAMILY_CASES)
def test_family(deployment: Deployment, expected: str | None) -> None:
    assert discovery.family(deployment) == expected


def test_family_prefers_the_arm_format_over_the_deployment_name() -> None:
    """One misleading name classifies two ways, decided only by ``fmt``."""
    misleading = "gpt-turbo-prod"
    assert discovery.family(dep(misleading, model="claude-opus-4-7", fmt="Anthropic")) == "opus"
    assert discovery.family(dep(misleading, model="gpt-5.4", fmt="OpenAI")) == "openai"


def test_family_falls_back_to_the_deployment_name_for_an_anthropic_format() -> None:
    """``fmt`` says Anthropic but the model name carries no tier."""
    assert discovery.family(dep("my-sonnet-box", model="claude-next", fmt="Anthropic")) == "sonnet"
    assert discovery.family(dep("mystery-box", model="claude-next", fmt="Anthropic")) == "other"


# --------------------------------------------------------------------------- #
# A fake Azure Resource Manager                                                 #
# --------------------------------------------------------------------------- #

ARM = "https://management.azure.com"
SUB_A = "11111111-1111-1111-1111-111111111111"
SUB_B = "22222222-2222-2222-2222-222222222222"
SUB_C = "33333333-3333-3333-3333-333333333333"


def accounts_url(sub: str) -> str:
    return (
        f"{ARM}/subscriptions/{sub}/providers/Microsoft.CognitiveServices/accounts"
        f"?api-version={discovery.API_VERSION}"
    )


def deployments_url(sub: str, group: str, account: str) -> str:
    return (
        f"{ARM}/subscriptions/{sub}/resourceGroups/{group}"
        f"/providers/Microsoft.CognitiveServices/accounts/{account}"
        f"/deployments?api-version={discovery.API_VERSION}"
    )


def arm_account(name: str, *, sub: str, group: str, kind: str = "AIServices", **properties) -> dict:
    properties.setdefault("customSubDomainName", name)
    properties.setdefault("endpoint", f"https://{name}.cognitiveservices.azure.com/")
    return {
        "id": (
            f"/subscriptions/{sub}/resourceGroups/{group}"
            f"/providers/Microsoft.CognitiveServices/accounts/{name}"
        ),
        "name": name,
        "kind": kind,
        "location": "swedencentral",
        "properties": properties,
    }


def arm_deployment(
    name: str, model: str, fmt: str, *, state: str = "Succeeded", sku: str = "GlobalStandard"
) -> dict:
    return {
        "name": name,
        "sku": {"name": sku},
        "properties": {
            "provisioningState": state,
            "model": {"name": model, "format": fmt, "version": "2025-01-01"},
        },
    }


class FakeArm:
    """An in-memory ARM: url -> (status, payload). Records every request."""

    def __init__(self, routes: dict[str, tuple[int, dict]]):
        self.routes = routes
        self.calls: list[tuple[str, dict[str, str]]] = []

    def get(self, url: str, headers: dict[str, str], *, timeout=None):
        self.calls.append((url, dict(headers)))
        try:
            status, payload = self.routes[url]
        except KeyError:  # pragma: no cover - a mismatch is a bug in the test
            raise AssertionError(f"unexpected ARM request: {url}") from None
        return status, json.dumps(payload).encode("utf-8")

    def urls(self) -> list[str]:
        return [url for url, _ in self.calls]


@pytest.fixture
def arm(monkeypatch):
    """Install a FakeArm plus a token stub. Returns the installer."""

    def install(routes: dict[str, tuple[int, dict]], *, subs=(SUB_A,), token_errors=None):
        fake = FakeArm(routes)
        monkeypatch.setattr(discovery, "_get", fake.get)
        monkeypatch.setattr(
            auth,
            "subscriptions",
            lambda: [
                {"id": s, "name": f"sub-{i}", "state": "Enabled", "isDefault": i == 0}
                for i, s in enumerate(subs)
            ],
        )
        errors = token_errors or {}

        def fake_token(*, resource=auth.DATA_RESOURCE, subscription=None):
            assert resource == auth.ARM_RESOURCE, "a management call needs an ARM token"
            if subscription in errors:
                raise auth.AuthError(errors[subscription])
            return f"arm-token-for-{subscription}"

        monkeypatch.setattr(auth, "token", fake_token)
        return fake

    return install


# --------------------------------------------------------------------------- #
# nextLink pagination -- SPEC section 4                                         #
# --------------------------------------------------------------------------- #


def test_accounts_listing_follows_next_link(arm) -> None:
    """A resource on ARM's second page is invisible to a single-GET scan."""
    page1 = accounts_url(SUB_A)
    page2 = f"{ARM}/subscriptions/{SUB_A}/accounts?$skipToken=PAGE2"
    fake = arm(
        {
            page1: (
                200,
                {"value": [arm_account("alpha", sub=SUB_A, group="rg-a")], "nextLink": page2},
            ),
            page2: (200, {"value": [arm_account("beta", sub=SUB_A, group="rg-b")]}),
        }
    )

    found = discovery.list_accounts()

    assert [a.name for a in found] == ["alpha", "beta"]
    assert fake.urls() == [page1, page2]
    assert found[1] == Account(
        subscription=SUB_A,
        resource_group="rg-b",
        name="beta",
        endpoint="https://beta.services.ai.azure.com",
        location="swedencentral",
    )


def test_deployments_listing_follows_next_link(arm) -> None:
    page1 = deployments_url(SUB_A, "rg-a", "alpha")
    page2 = f"{ARM}/deployments-page-2"
    fake = arm(
        {
            page1: (
                200,
                {"value": [arm_deployment("gpt-4.1-prod", "gpt-4.1", "OpenAI")], "nextLink": page2},
            ),
            page2: (200, {"value": [arm_deployment("newest", "gpt-5.4", "OpenAI")]}),
        }
    )
    account = Account(SUB_A, "rg-a", "alpha", "https://alpha.services.ai.azure.com", "sc")

    found = discovery.deployments(account)

    # Page two carries the newer model, so pagination and ordering are both
    # exercised: a single-GET implementation returns the 4.1 as the default.
    assert [d.name for d in found] == ["newest", "gpt-4.1-prod"]
    assert found[0] == Deployment(
        name="newest", model="gpt-5.4", fmt="OpenAI", version="2025-01-01", sku="GlobalStandard"
    )
    assert fake.urls() == [page1, page2]
    assert fake.calls[0][1]["Authorization"] == f"Bearer arm-token-for-{SUB_A}"


def test_pagination_stops_on_a_self_referential_next_link(arm) -> None:
    """A nextLink loop must terminate rather than hang the scan."""
    page1 = accounts_url(SUB_A)
    fake = arm(
        {
            page1: (
                200,
                {"value": [arm_account("alpha", sub=SUB_A, group="rg-a")], "nextLink": page1},
            )
        }
    )

    assert [a.name for a in discovery.list_accounts()] == ["alpha"]
    assert fake.urls() == [page1]


def test_deployments_url_names_subscription_group_account_and_api_version(arm) -> None:
    quoted = deployments_url(SUB_A, "rg%20with%20space", "alpha")
    fake = arm({quoted: (200, {"value": []})})
    account = Account(SUB_A, "rg with space", "alpha", "https://alpha.services.ai.azure.com", "sc")

    assert discovery.deployments(account) == []
    assert fake.urls() == [quoted]
    assert f"api-version={discovery.API_VERSION}" in quoted


def test_deployments_drops_everything_not_succeeded(arm) -> None:
    page = deployments_url(SUB_A, "rg-a", "alpha")
    arm(
        {
            page: (
                200,
                {
                    "value": [
                        arm_deployment("ok", "gpt-4.1", "OpenAI"),
                        arm_deployment("half-built", "gpt-5.4", "OpenAI", state="Creating"),
                        arm_deployment("broken", "gpt-5.4", "OpenAI", state="Failed"),
                        {"name": "", "properties": {}},
                    ]
                },
            )
        }
    )
    account = Account(SUB_A, "rg-a", "alpha", "https://alpha.services.ai.azure.com", "sc")

    assert [d.name for d in discovery.deployments(account)] == ["ok"]


def test_deployments_keeps_non_chat_models_for_the_caller_to_filter(arm) -> None:
    """Filtering is ``is_chat_model``'s job, not the lister's."""
    page = deployments_url(SUB_A, "rg-a", "alpha")
    arm(
        {
            page: (
                200,
                {
                    "value": [
                        arm_deployment("chat", "gpt-4.1", "OpenAI"),
                        arm_deployment("stt", "whisper-1", "OpenAI"),
                    ]
                },
            )
        }
    )
    account = Account(SUB_A, "rg-a", "alpha", "https://alpha.services.ai.azure.com", "sc")

    assert sorted(d.name for d in discovery.deployments(account)) == ["chat", "stt"]


def test_deployments_without_a_resource_group_names_the_fix(arm) -> None:
    arm({})
    account = Account(SUB_A, "", "alpha", "https://alpha.services.ai.azure.com", "sc")

    with pytest.raises(auth.AuthError) as excinfo:
        discovery.deployments(account)

    assert "foundry configure" in str(excinfo.value)


def test_arm_403_names_the_role_to_ask_for(arm) -> None:
    page = deployments_url(SUB_A, "rg-a", "alpha")
    arm({page: (403, {"error": {"code": "AuthorizationFailed", "message": "no Reader here"}})})
    account = Account(SUB_A, "rg-a", "alpha", "https://alpha.services.ai.azure.com", "sc")

    with pytest.raises(auth.AuthError) as excinfo:
        discovery.deployments(account)

    message = str(excinfo.value)
    assert "403" in message
    assert "Reader" in message
    assert "no Reader here" in message


# --------------------------------------------------------------------------- #
# Account matching                                                              #
# --------------------------------------------------------------------------- #


def test_find_account_matches_on_the_custom_subdomain(arm) -> None:
    arm(
        {
            accounts_url(SUB_A): (
                200,
                {
                    "value": [
                        arm_account("other", sub=SUB_A, group="rg-x"),
                        arm_account("my-resource", sub=SUB_A, group="rg-a"),
                    ]
                },
            )
        }
    )

    account = discovery.find_account("https://my-resource.services.ai.azure.com")

    assert account == Account(
        subscription=SUB_A,
        resource_group="rg-a",
        name="my-resource",
        endpoint="https://my-resource.services.ai.azure.com",
        location="swedencentral",
    )


@pytest.mark.parametrize(
    "given",
    [
        "my-resource",
        "my-resource.openai.azure.com",
        "https://my-resource.services.ai.azure.com/api/projects/p1",
    ],
)
def test_find_account_accepts_every_endpoint_shape(arm, given: str) -> None:
    arm(
        {
            accounts_url(SUB_A): (
                200,
                {
                    "value": [
                        arm_account(
                            "my-resource",
                            sub=SUB_A,
                            group="rg-a",
                            endpoints={
                                "AI Foundry API": (
                                    "https://my-resource.services.ai.azure.com/api/projects/p1"
                                )
                            },
                        )
                    ]
                },
            )
        }
    )

    account = discovery.find_account(given)

    assert account.name == "my-resource"
    # The project path segment serves no inference and must not survive.
    assert account.endpoint == "https://my-resource.services.ai.azure.com"


def test_scan_skips_accounts_that_cannot_serve_models(arm) -> None:
    arm(
        {
            accounts_url(SUB_A): (
                200,
                {
                    "value": [
                        arm_account("speech", sub=SUB_A, group="rg-a", kind="SpeechServices"),
                        arm_account("vision", sub=SUB_A, group="rg-a", kind="ComputerVision"),
                        arm_account("real", sub=SUB_A, group="rg-a", kind="OpenAI"),
                    ]
                },
            )
        }
    )

    assert [a.name for a in discovery.list_accounts()] == ["real"]


def test_scan_dedupes_the_same_account_seen_twice(arm) -> None:
    page1 = accounts_url(SUB_A)
    page2 = f"{ARM}/page2"
    arm(
        {
            page1: (
                200,
                {"value": [arm_account("alpha", sub=SUB_A, group="rg-a")], "nextLink": page2},
            ),
            page2: (200, {"value": [arm_account("alpha", sub=SUB_A, group="RG-A")]}),
        }
    )

    assert [a.name for a in discovery.list_accounts()] == ["alpha"]


# --------------------------------------------------------------------------- #
# "we could not look" is not "it is not there" -- SPEC section 9                #
# --------------------------------------------------------------------------- #


def test_unscannable_subscriptions_are_named_in_the_error(arm) -> None:
    """The case the spec singles out.

    Reporting a bare "not found" when two of three subscriptions refused a token
    sends the user hunting the wrong problem, so the message must name each one
    and say it was *not* searched.
    """
    arm(
        {accounts_url(SUB_A): (200, {"value": [arm_account("other", sub=SUB_A, group="rg-a")]})},
        subs=(SUB_A, SUB_B, SUB_C),
        token_errors={
            SUB_B: "az could not mint a management token for this subscription",
            SUB_C: "Please run `az login` to set up an account",
        },
    )

    with pytest.raises(auth.AuthError) as excinfo:
        discovery.find_account("https://my-resource.services.ai.azure.com")

    message = str(excinfo.value)

    # Every subscription is listed, by id and by display name.
    for sub, name in ((SUB_A, "sub-0"), (SUB_B, "sub-1"), (SUB_C, "sub-2")):
        assert sub in message
        assert name in message

    # The two that failed are marked as not searched, each with its reason.
    assert f"{SUB_B}] -- NOT searched" in message
    assert f"{SUB_C}] -- NOT searched" in message
    assert "az could not mint a management token for this subscription" in message
    assert "Please run `az login` to set up an account" in message

    # The one that answered is not.
    assert f"{SUB_A}] -- NOT searched" not in message

    # And the message says the resource may well exist, plus the flag that fixes it.
    assert "2 of them could NOT be searched" in message
    assert "the resource may well exist" in message
    assert "--subscription" in message
    assert "my-resource" in message


def test_a_fully_searched_scan_says_so_instead_of_blaming_access(arm) -> None:
    arm(
        {
            accounts_url(SUB_A): (200, {"value": [arm_account("other", sub=SUB_A, group="rg-a")]}),
            accounts_url(SUB_B): (200, {"value": []}),
        },
        subs=(SUB_A, SUB_B),
    )

    with pytest.raises(auth.AuthError) as excinfo:
        discovery.find_account("missing-resource")

    message = str(excinfo.value)
    assert "NOT searched" not in message
    assert "All of them answered" in message
    assert "Check the endpoint" in message
    assert "--subscription" in message


def test_scan_report_records_what_could_not_be_searched(arm) -> None:
    arm(
        {accounts_url(SUB_A): (200, {"value": [arm_account("alpha", sub=SUB_A, group="rg-a")]})},
        subs=(SUB_A, SUB_B),
        token_errors={SUB_B: "token refused"},
    )

    accounts, report = discovery.scan_accounts()

    assert [a.name for a in accounts] == ["alpha"]
    assert report.searched == (SUB_A, SUB_B)
    assert dict(report.unreachable) == {SUB_B: "token refused"}
    assert report.names == {SUB_A: "sub-0", SUB_B: "sub-1"}
    assert report.describe() == (
        f"  - sub-0 [{SUB_A}]\n  - sub-1 [{SUB_B}] -- NOT searched: token refused"
    )


def test_one_dead_subscription_does_not_hide_the_resource_in_the_next(arm) -> None:
    arm(
        {
            accounts_url(SUB_B): (
                200,
                {"value": [arm_account("my-resource", sub=SUB_B, group="rg-b")]},
            )
        },
        subs=(SUB_A, SUB_B),
        token_errors={SUB_A: "token refused"},
    )

    account = discovery.find_account("my-resource")

    assert account.subscription == SUB_B
    assert account.resource_group == "rg-b"


def test_an_explicit_subscription_short_circuits_the_scan(arm) -> None:
    fake = arm(
        {
            accounts_url(SUB_B): (
                200,
                {"value": [arm_account("my-resource", sub=SUB_B, group="rg-b")]},
            )
        },
        subs=(SUB_A, SUB_B),
    )

    account = discovery.find_account("my-resource", subscription=SUB_B)

    assert account.subscription == SUB_B
    assert fake.urls() == [accounts_url(SUB_B)]


def test_no_enabled_subscriptions_names_az_login(arm, monkeypatch) -> None:
    arm({})
    monkeypatch.setattr(auth, "subscriptions", list)

    with pytest.raises(auth.AuthError) as excinfo:
        discovery.list_accounts()

    assert "az login" in str(excinfo.value)


def test_disabled_subscriptions_are_not_scanned(arm, monkeypatch) -> None:
    fake = arm({accounts_url(SUB_A): (200, {"value": []})})
    monkeypatch.setattr(
        auth,
        "subscriptions",
        lambda: [
            {"id": SUB_A, "name": "sub-0", "state": "Enabled"},
            {"id": SUB_B, "name": "sub-1", "state": "Disabled"},
        ],
    )

    discovery.list_accounts()

    assert fake.urls() == [accounts_url(SUB_A)]
