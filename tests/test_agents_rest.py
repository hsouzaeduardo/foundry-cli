# SPDX-License-Identifier: Apache-2.0
"""Copilot, OpenCode and Pi -- the three agents whose wiring is a base URL.

The single highest-value thing these tests protect is the path-append asymmetry
of ``docs/agent-config-reference.md``: the same Foundry route is expressed with
a *different* base URL per client, because each client appends a different
suffix. One misplaced ``/v1`` and every request 404s, silently, at runtime.

    Copilot (azure)             E                 + /openai/v1/chat/completions
    OpenCode @ai-sdk/anthropic  E/anthropic/v1    + /messages
    OpenCode @ai-sdk/openai     E/openai/v1       + /responses
    Pi anthropic-messages       E/anthropic       + /v1/messages
    Pi openai-responses         E/openai/v1       + /responses

Everything here runs against mocked ``az`` and mocked HTTP: :func:`sandbox`
replaces :func:`foundry.auth.token` / :func:`foundry.auth.api_key`, and poisons
:func:`foundry.auth.run_az` and :func:`urllib.request.urlopen` so that a test
which reaches for a real subprocess or a real socket fails loudly instead of
passing on a developer's machine and nowhere else.
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from foundry import auth
from foundry import profile as profile_mod
from foundry.agents import AgentError
from foundry.agents import copilot as copilot_mod
from foundry.agents import opencode as opencode_mod
from foundry.agents import pi as pi_mod
from foundry.profile import Profile

# --------------------------------------------------------------------------- #
# Fixtures                                                                      #
# --------------------------------------------------------------------------- #

#: ``E`` in the agent reference.
ENDPOINT = "https://my-resource.services.ai.azure.com"

#: JWT-shaped, so a test that asserts on redaction elsewhere sees the real shape.
TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJmb3VuZHJ5LXRlc3QifQ.c2lnbmF0dXJlLXZhbHVl"
TOKEN_AFTER_REFRESH = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJyZWZyZXNoZWQifQ.YW5vdGhlci1zaWduYXR1cmU"

API_KEY = "0123456789abcdef0123456789abcdef"

#: Deployment names that say nothing about the model behind them. SPEC section 2:
#: the name is chosen freely by whoever published it, so any classification that
#: works here proves it came from ARM metadata and not from string matching.
OPUS = "alpha-1"
SONNET = "bravo-2"
HAIKU = "charlie-3"
OPENAI_NEW = "delta-4"
OPENAI_OLD = "echo-5"

#: Ambient state that must never reach a test. Leaking the developer's
#: environment into the suite is a bug class this project has already been bitten
#: by: ``FOUNDRY_BEARER`` alone silently changes which credential branch runs.
LEAKY_ENV: tuple[str, ...] = (
    "FOUNDRY_BEARER",
    "FOUNDRY_API_KEY",
    "FOUNDRY_EXECUTABLE",
    "AZURE_CLIENT_ID",
    "AZURE_CLIENT_SECRET",
    "AZURE_TENANT_ID",
    "NO_COLOR",
    "COPILOT_HOME",
    "COPILOT_MODEL",
    "COPILOT_PROVIDER_TYPE",
    "COPILOT_PROVIDER_BASE_URL",
    "COPILOT_PROVIDER_API_KEY",
    "COPILOT_PROVIDER_BEARER_TOKEN",
    "COPILOT_PROVIDER_MODEL_ID",
    "COPILOT_PROVIDER_WIRE_MODEL",
    "COPILOT_PROVIDER_WIRE_API",
    "COPILOT_PROVIDER_AZURE_API_VERSION",
    "COPILOT_PROVIDER_MAX_PROMPT_TOKENS",
    "COPILOT_PROVIDER_MAX_OUTPUT_TOKENS",
    "OPENCODE_CONFIG",
    "OPENCODE_CONFIG_DIR",
    "PI_CODING_AGENT_DIR",
)


class Sandbox:
    """The mocked credential boundary, plus the isolated ``~/.foundry``."""

    def __init__(self, home: Path) -> None:
        self.home = home
        self.token_value = TOKEN
        self.token_subscriptions: list[str | None] = []
        self.api_key_value: str | None = None
        self.api_key_calls: list[tuple[str, str | None]] = []

    def token(self, *, resource: str = auth.DATA_RESOURCE, subscription: str | None = None) -> str:
        del resource
        self.token_subscriptions.append(subscription)
        return self.token_value

    def api_key(self, endpoint: str, subscription: str | None = None) -> str | None:
        self.api_key_calls.append((endpoint, subscription))
        return self.api_key_value


@pytest.fixture(autouse=True)
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Sandbox:
    """Isolate ``~/.foundry`` into tmp_path and forbid `az`, HTTP and ambient env."""
    home = tmp_path / "dot-foundry"
    monkeypatch.setenv(profile_mod.HOME_ENV, str(home))
    for name in LEAKY_ENV:
        monkeypatch.delenv(name, raising=False)

    box = Sandbox(home)
    monkeypatch.setattr(auth, "token", box.token)
    monkeypatch.setattr(auth, "api_key", box.api_key)

    def no_az(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(f"a test tried to run az: {args!r} {kwargs!r}")

    def no_network(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(f"a test tried to open a socket: {args!r}")

    monkeypatch.setattr(auth, "run_az", no_az)
    monkeypatch.setattr(urllib.request, "urlopen", no_network)
    return box


@pytest.fixture
def profile() -> Profile:
    """A resource publishing three Anthropic tiers and two OpenAI deployments."""
    return Profile(
        endpoint=ENDPOINT,
        subscription="11111111-2222-3333-4444-555555555555",
        resource_group="rg-example",
        account="my-resource",
        deployments={
            "anthropic": {"opus": OPUS, "sonnet": SONNET, "haiku": HAIKU},
            "openai": [OPENAI_NEW, OPENAI_OLD],
        },
        agents={},
    )


@pytest.fixture
def no_refresher(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Any]]:
    """Capture ``start_refresher`` instead of spawning a background thread."""
    started: list[tuple[str, Any]] = []

    def fake(label: str, rewrite: Any, **kwargs: Any) -> None:
        del kwargs
        started.append((label, rewrite))

    monkeypatch.setattr(opencode_mod, "start_refresher", fake)
    monkeypatch.setattr(pi_mod, "start_refresher", fake)
    return started


def files_under(root: Path) -> set[str]:
    """Every file below *root*, as ``/``-joined relative paths."""
    if not root.exists():
        return set()
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


# --------------------------------------------------------------------------- #
# The base-URL / path-append table (agent reference, top table)                  #
# --------------------------------------------------------------------------- #


def copilot_base(p: Profile) -> str:
    return copilot_mod.AGENT.provider_env(p, OPENAI_NEW)["COPILOT_PROVIDER_BASE_URL"]


def opencode_anthropic_base(p: Profile) -> str:
    document = opencode_mod.AGENT.document(p, SONNET, token=TOKEN)
    return document["provider"][opencode_mod.ANTHROPIC_PROVIDER]["options"]["baseURL"]


def opencode_openai_base(p: Profile) -> str:
    document = opencode_mod.AGENT.document(p, SONNET, token=TOKEN)
    return document["provider"][opencode_mod.OPENAI_PROVIDER]["options"]["baseURL"]


def pi_anthropic_base(p: Profile) -> str:
    document = pi_mod.AGENT.providers_document(p, SONNET, token=TOKEN)
    return document["providers"][pi_mod.ANTHROPIC_PROVIDER]["baseUrl"]


def pi_openai_base(p: Profile) -> str:
    document = pi_mod.AGENT.providers_document(p, SONNET, token=TOKEN)
    return document["providers"][pi_mod.OPENAI_PROVIDER]["baseUrl"]


BASE_URL_TABLE: tuple[tuple[str, Any, str, str, str], ...] = (
    # label, base extractor, base URL configured, what the client appends, result
    (
        "copilot-azure",
        copilot_base,
        ENDPOINT,
        "/openai/v1/chat/completions",
        f"{ENDPOINT}/openai/v1/chat/completions",
    ),
    (
        "opencode-anthropic",
        opencode_anthropic_base,
        f"{ENDPOINT}/anthropic/v1",
        "/messages",
        f"{ENDPOINT}/anthropic/v1/messages",
    ),
    (
        "opencode-openai",
        opencode_openai_base,
        f"{ENDPOINT}/openai/v1",
        "/responses",
        f"{ENDPOINT}/openai/v1/responses",
    ),
    (
        "pi-anthropic-messages",
        pi_anthropic_base,
        f"{ENDPOINT}/anthropic",
        "/v1/messages",
        f"{ENDPOINT}/anthropic/v1/messages",
    ),
    (
        "pi-openai-responses",
        pi_openai_base,
        f"{ENDPOINT}/openai/v1",
        "/responses",
        f"{ENDPOINT}/openai/v1/responses",
    ),
)


@pytest.mark.parametrize(
    ("label", "extract", "expected_base", "client_appends", "expected_request"),
    BASE_URL_TABLE,
    ids=[row[0] for row in BASE_URL_TABLE],
)
def test_base_url_is_exactly_what_the_client_expects(
    label: str,
    extract: Any,
    expected_base: str,
    client_appends: str,
    expected_request: str,
    profile: Profile,
    sandbox: Sandbox,
) -> None:
    """Each configured base URL, plus the client's own append, is the live route."""
    del label
    sandbox.api_key_value = API_KEY  # copilot's azure branch
    base = extract(profile)
    assert base == expected_base
    assert base + client_appends == expected_request


def test_the_v1_segment_sits_on_opposite_sides_for_opencode_and_pi(
    profile: Profile, sandbox: Sandbox
) -> None:
    """The asymmetry itself: same route, different base URL, identical request."""
    del sandbox
    opencode_base = opencode_anthropic_base(profile)
    pi_base = pi_anthropic_base(profile)

    assert opencode_base.endswith("/anthropic/v1")
    assert pi_base.endswith("/anthropic")
    assert not pi_base.endswith("/v1")
    assert opencode_base != pi_base
    # @ai-sdk/anthropic appends /messages; Pi's anthropic-messages appends /v1/messages.
    assert opencode_base + "/messages" == pi_base + "/v1/messages"


def test_copilot_base_url_carries_no_route_segment(profile: Profile, sandbox: Sandbox) -> None:
    """Copilot is the one place ``/openai/v1`` is *not* part of the base URL."""
    sandbox.api_key_value = API_KEY
    base = copilot_base(profile)
    assert base == ENDPOINT
    assert "/openai" not in base
    assert not base.endswith("/v1")


# --------------------------------------------------------------------------- #
# Copilot                                                                       #
# --------------------------------------------------------------------------- #


def test_copilot_provider_env_with_a_resource_key_is_exact(
    profile: Profile, sandbox: Sandbox
) -> None:
    """Type ``azure`` -> ``api-key`` auth, bare root base URL, and nothing else."""
    sandbox.api_key_value = API_KEY
    env = copilot_mod.AGENT.provider_env(profile, OPENAI_NEW)

    assert env == {
        "COPILOT_HOME": str(profile_mod.app_dir() / "agents" / "copilot"),
        "COPILOT_PROVIDER_TYPE": "azure",
        "COPILOT_PROVIDER_BASE_URL": ENDPOINT,
        "COPILOT_PROVIDER_API_KEY": API_KEY,
        # delta-4 matches nothing in Copilot's catalogue, so it is its own id and
        # the token limits have to be stated.
        "COPILOT_PROVIDER_MODEL_ID": OPENAI_NEW,
        "COPILOT_PROVIDER_WIRE_MODEL": OPENAI_NEW,
        "COPILOT_PROVIDER_MAX_PROMPT_TOKENS": copilot_mod.FALLBACK_MAX_PROMPT_TOKENS,
        "COPILOT_PROVIDER_MAX_OUTPUT_TOKENS": copilot_mod.FALLBACK_MAX_OUTPUT_TOKENS,
    }
    assert "COPILOT_PROVIDER_BEARER_TOKEN" not in env
    assert sandbox.api_key_calls == [(ENDPOINT, profile.subscription)]


def test_copilot_falls_back_to_a_bearer_and_moves_the_route_segment(
    profile: Profile, sandbox: Sandbox
) -> None:
    """No key available -> type ``openai``, bearer auth, base URL gains /openai/v1."""
    sandbox.api_key_value = None
    env = copilot_mod.AGENT.provider_env(profile, OPENAI_NEW)

    assert env["COPILOT_PROVIDER_TYPE"] == "openai"
    assert env["COPILOT_PROVIDER_BASE_URL"] == f"{ENDPOINT}/openai/v1"
    assert env["COPILOT_PROVIDER_BEARER_TOKEN"] == TOKEN
    assert "COPILOT_PROVIDER_API_KEY" not in env
    assert sandbox.token_subscriptions == [profile.subscription]


def test_copilot_bearer_env_overrides_the_key(
    profile: Profile, sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pre-minted ``FOUNDRY_BEARER`` says which credential the user wants."""
    sandbox.api_key_value = API_KEY
    monkeypatch.setenv(auth.BEARER_ENV, "pre-minted")
    kind, value = copilot_mod.AGENT.credential(profile)
    assert (kind, value) == ("bearer", TOKEN)
    assert sandbox.api_key_calls == []


@pytest.mark.parametrize(
    ("deployment", "expected_model_id"),
    [
        # Deployment names that say nothing: the id comes from the profile's own
        # ARM-derived tiers, and the deployment name still goes on the wire.
        (OPUS, "claude-opus-4.5"),
        (SONNET, "claude-sonnet-4.5"),
        (HAIKU, "claude-haiku-4.5"),
        # An OpenAI-family deployment named after its model matches exactly.
        ("gpt-4.1", "gpt-4.1"),
        ("gpt-5.1-codex", "gpt-5.1-codex"),
        # Dashes-for-dots is the same catalogue entry.
        ("claude-opus-4-5", "claude-opus-4.5"),
        # Near matches by family and size.
        ("prod-gpt-5-nano", "gpt-5-mini"),
        ("gpt-4o-prod", "gpt-4.1"),
        # Nothing recognisable: the deployment name is used for both.
        ("mystery-deployment", "mystery-deployment"),
    ],
)
def test_copilot_model_id_wire_model_split(
    deployment: str, expected_model_id: str, profile: Profile, sandbox: Sandbox
) -> None:
    """MODEL_ID drives token limits; WIRE_MODEL is the deployment, verbatim."""
    sandbox.api_key_value = API_KEY
    env = copilot_mod.AGENT.provider_env(profile, deployment)

    assert env["COPILOT_PROVIDER_MODEL_ID"] == expected_model_id
    assert env["COPILOT_PROVIDER_WIRE_MODEL"] == deployment
    assert copilot_mod.AGENT.model_id(profile, deployment) == expected_model_id


def test_copilot_states_token_limits_only_when_the_id_is_unknown(
    profile: Profile, sandbox: Sandbox
) -> None:
    """A catalogue id sizes itself; an unknown one needs explicit limits."""
    sandbox.api_key_value = API_KEY

    known = copilot_mod.AGENT.provider_env(profile, "gpt-4.1")
    assert known["COPILOT_PROVIDER_MODEL_ID"] in copilot_mod.CATALOGUE_MODEL_IDS
    assert "COPILOT_PROVIDER_MAX_PROMPT_TOKENS" not in known
    assert "COPILOT_PROVIDER_MAX_OUTPUT_TOKENS" not in known

    unknown = copilot_mod.AGENT.provider_env(profile, "mystery-deployment")
    assert unknown["COPILOT_PROVIDER_MODEL_ID"] not in copilot_mod.CATALOGUE_MODEL_IDS
    assert unknown["COPILOT_PROVIDER_MAX_PROMPT_TOKENS"] == "128000"
    assert unknown["COPILOT_PROVIDER_MAX_OUTPUT_TOKENS"] == "16384"


def test_copilot_home_is_inside_the_foundry_app_dir(profile: Profile, sandbox: Sandbox) -> None:
    """``COPILOT_HOME`` relocates ~/.copilot into foundry's own tree."""
    sandbox.api_key_value = API_KEY
    home = Path(copilot_mod.AGENT.provider_env(profile, OPENAI_NEW)["COPILOT_HOME"])

    assert home == sandbox.home / "agents" / "copilot"
    assert home.is_absolute()
    assert home.is_relative_to(profile_mod.app_dir())
    assert home.is_dir()


def test_copilot_launch_env_removes_the_variables_that_move_the_route(
    profile: Profile, sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An inherited api-version or wire-api would repoint the request path."""
    sandbox.api_key_value = API_KEY
    monkeypatch.setenv("COPILOT_PROVIDER_AZURE_API_VERSION", "2024-10-21")
    monkeypatch.setenv("COPILOT_PROVIDER_WIRE_API", "responses")
    monkeypatch.setenv("COPILOT_MODEL", "gpt-5")
    monkeypatch.setenv("COPILOT_PROVIDER_BEARER_TOKEN", "stale-token-from-the-users-shell")

    env = copilot_mod.AGENT.launch_env(profile, OPENAI_NEW)

    for name in copilot_mod.CONFLICTING_ENV:
        assert name not in env
    # BEARER_TOKEN outranks API_KEY, so a stale one would win if it survived.
    assert "COPILOT_PROVIDER_BEARER_TOKEN" not in env
    assert env["COPILOT_PROVIDER_API_KEY"] == API_KEY
    assert env["COPILOT_PROVIDER_BASE_URL"] == ENDPOINT


def test_copilot_configure_records_the_split_and_writes_no_file(
    profile: Profile, sandbox: Sandbox
) -> None:
    """Provider configuration is environment-only: only the directory is claimed."""
    sandbox.api_key_value = API_KEY
    copilot_mod.AGENT.configure(profile, None)

    entry = profile.agents["copilot"]
    home = str(profile_mod.app_dir() / "agents" / "copilot")
    assert entry["home"] == home
    assert entry["model"] == OPENAI_NEW  # prefer="openai": the OpenAI tier leads
    assert entry["model_id"] == OPENAI_NEW
    assert entry["credential"] == "key"
    assert entry["owns"] == [home, *copilot_mod.OWNED_ENV]
    assert files_under(sandbox.home) == set()


def test_copilot_default_model_prefers_the_openai_family(profile: Profile) -> None:
    """Copilot speaks the OpenAI wire protocol, so an OpenAI deployment leads."""
    assert copilot_mod.AGENT.default_model(profile) == OPENAI_NEW

    anthropic_only = Profile(
        endpoint=ENDPOINT,
        subscription="",
        resource_group="",
        account="my-resource",
        deployments={"anthropic": {"sonnet": SONNET}, "openai": []},
    )
    assert copilot_mod.AGENT.default_model(anthropic_only) == SONNET


def test_copilot_without_any_deployment_names_the_fix(sandbox: Sandbox) -> None:
    del sandbox
    empty = Profile(
        endpoint=ENDPOINT,
        subscription="",
        resource_group="",
        account="my-resource",
        deployments={},
    )
    with pytest.raises(AgentError) as caught:
        copilot_mod.AGENT.provider_env(empty, None)
    message = str(caught.value)
    assert "my-resource publishes no chat model for Copilot." in message
    assert "foundry configure" in message


# --------------------------------------------------------------------------- #
# OpenCode                                                                      #
# --------------------------------------------------------------------------- #


def test_opencode_document_is_exact(profile: Profile, sandbox: Sandbox) -> None:
    """Every key of ``opencode.json``, including both base URLs."""
    del sandbox
    document = opencode_mod.AGENT.document(profile, None, token=TOKEN)

    assert document == {
        "$schema": "https://opencode.ai/config.json",
        # Top-level model is "<providerID>/<modelID>"; sonnet is the default tier.
        "model": f"foundry-anthropic/{SONNET}",
        "provider": {
            "foundry-anthropic": {
                "npm": "@ai-sdk/anthropic",
                "name": "Microsoft Foundry (Anthropic)",
                "options": {
                    "baseURL": f"{ENDPOINT}/anthropic/v1",
                    "apiKey": TOKEN,
                    "headers": {"Authorization": f"Bearer {TOKEN}"},
                },
                "models": {OPUS: {}, SONNET: {}, HAIKU: {}},
            },
            "foundry-openai": {
                "npm": "@ai-sdk/openai",
                "name": "Microsoft Foundry (OpenAI)",
                "options": {
                    "baseURL": f"{ENDPOINT}/openai/v1",
                    "apiKey": TOKEN,
                },
                "models": {OPENAI_NEW: {}, OPENAI_OLD: {}},
            },
        },
    }


def test_opencode_models_is_an_object_keyed_by_deployment_name(
    profile: Profile, sandbox: Sandbox
) -> None:
    """``models`` is a mapping, not an array -- the key is what goes on the wire."""
    del sandbox
    providers = opencode_mod.AGENT.document(profile, None, token=TOKEN)["provider"]

    anthropic_models = providers["foundry-anthropic"]["models"]
    openai_models = providers["foundry-openai"]["models"]

    assert isinstance(anthropic_models, dict)
    assert isinstance(openai_models, dict)
    # Order is opus, sonnet, haiku, then the OpenAI list newest-first.
    assert list(anthropic_models) == [OPUS, SONNET, HAIKU]
    assert list(openai_models) == [OPENAI_NEW, OPENAI_OLD]
    assert all(entry == {} for entry in anthropic_models.values())
    assert all(entry == {} for entry in openai_models.values())


@pytest.mark.parametrize(
    ("requested", "expected_selector"),
    [
        (None, f"foundry-anthropic/{SONNET}"),
        (OPUS, f"foundry-anthropic/{OPUS}"),
        (HAIKU, f"foundry-anthropic/{HAIKU}"),
        (OPENAI_NEW, f"foundry-openai/{OPENAI_NEW}"),
        (OPENAI_OLD, f"foundry-openai/{OPENAI_OLD}"),
    ],
)
def test_opencode_selector_string_names_provider_and_model(
    requested: str | None, expected_selector: str, profile: Profile, sandbox: Sandbox
) -> None:
    del sandbox
    document = opencode_mod.AGENT.document(profile, requested, token=TOKEN)
    assert document["model"] == expected_selector

    provider_id, _, model_id = expected_selector.partition("/")
    assert model_id in document["provider"][provider_id]["models"]


def test_opencode_adds_a_deployment_the_last_scan_never_saw(
    profile: Profile, sandbox: Sandbox
) -> None:
    """``--model`` must appear in ``models`` or OpenCode will not offer it."""
    del sandbox
    document = opencode_mod.AGENT.document(profile, "brand-new-gpt", token=TOKEN)

    assert document["model"] == "foundry-openai/brand-new-gpt"
    assert list(document["provider"]["foundry-openai"]["models"]) == [
        "brand-new-gpt",
        OPENAI_NEW,
        OPENAI_OLD,
    ]


def test_opencode_omits_a_provider_the_resource_cannot_serve(sandbox: Sandbox) -> None:
    """No Anthropic deployment means no Anthropic provider block at all."""
    del sandbox
    openai_only = Profile(
        endpoint=ENDPOINT,
        subscription="",
        resource_group="",
        account="my-resource",
        deployments={"openai": [OPENAI_NEW]},
    )
    document = opencode_mod.AGENT.document(openai_only, None, token=TOKEN)

    assert list(document["provider"]) == ["foundry-openai"]
    assert document["model"] == f"foundry-openai/{OPENAI_NEW}"


def test_opencode_write_config_writes_that_exact_json(profile: Profile, sandbox: Sandbox) -> None:
    """What lands on disk is the document, key for key, and nothing else is written."""
    path = opencode_mod.AGENT.write_config(profile, None)

    assert path == sandbox.home / "agents" / "opencode" / "opencode.json"
    assert files_under(sandbox.home) == {"agents/opencode/opencode.json"}

    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk == opencode_mod.AGENT.document(profile, None, token=TOKEN)
    assert on_disk["provider"]["foundry-anthropic"]["options"]["baseURL"] == (
        f"{ENDPOINT}/anthropic/v1"
    )
    assert on_disk["provider"]["foundry-openai"]["options"]["baseURL"] == f"{ENDPOINT}/openai/v1"
    assert sandbox.token_subscriptions == [profile.subscription]


def test_opencode_configure_records_the_file_it_owns(profile: Profile, sandbox: Sandbox) -> None:
    opencode_mod.AGENT.configure(profile, None)

    entry = profile.agents["opencode"]
    home = sandbox.home / "agents" / "opencode"
    assert entry["home"] == str(home)
    assert entry["owns"] == [str(home / "opencode.json")]
    assert "configured_at" in entry


def test_opencode_isolation_variables_point_inside_the_app_dir(
    profile: Profile, sandbox: Sandbox, no_refresher: list[tuple[str, Any]]
) -> None:
    env = opencode_mod.AGENT.launch_env(profile, None)
    home = sandbox.home / "agents" / "opencode"

    assert env["OPENCODE_CONFIG_DIR"] == str(home)
    assert env["OPENCODE_CONFIG"] == str(home / "opencode.json")
    assert Path(env["OPENCODE_CONFIG_DIR"]).is_relative_to(profile_mod.app_dir())
    assert Path(env["OPENCODE_CONFIG"]).is_relative_to(profile_mod.app_dir())
    # The launch mints a fresh credential rather than trusting last week's file.
    assert (home / "opencode.json").is_file()
    assert [label for label, _ in no_refresher] == [f"opencode:{home / 'opencode.json'}"]


def test_opencode_refresher_rewrites_only_the_credential(
    profile: Profile, sandbox: Sandbox, no_refresher: list[tuple[str, Any]]
) -> None:
    """A 30-minute rewrite must change the token and nothing else."""
    opencode_mod.AGENT.launch_env(profile, None)
    path = opencode_mod.AGENT.config_path()
    before = json.loads(path.read_text(encoding="utf-8"))

    sandbox.token_value = TOKEN_AFTER_REFRESH
    _label, rewrite = no_refresher[0]
    rewrite()

    after = json.loads(path.read_text(encoding="utf-8"))
    assert after["provider"]["foundry-anthropic"]["options"]["apiKey"] == TOKEN_AFTER_REFRESH
    assert after["provider"]["foundry-openai"]["options"]["apiKey"] == TOKEN_AFTER_REFRESH
    assert after["provider"]["foundry-anthropic"]["options"]["headers"] == {
        "Authorization": f"Bearer {TOKEN_AFTER_REFRESH}"
    }
    assert _without_credentials(after) == _without_credentials(before)


def test_opencode_argv_passes_user_arguments_through_untouched(
    profile: Profile, sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    del sandbox
    monkeypatch.setattr(opencode_mod.AGENT, "executable", lambda: "/usr/local/bin/opencode")
    assert opencode_mod.AGENT.launch_argv(profile, ["run", "--continue", "-m", "x"]) == [
        "/usr/local/bin/opencode",
        "run",
        "--continue",
        "-m",
        "x",
    ]


def test_opencode_without_any_deployment_names_the_fix(sandbox: Sandbox) -> None:
    del sandbox
    empty = Profile(
        endpoint=ENDPOINT, subscription="", resource_group="", account="", deployments={}
    )
    with pytest.raises(AgentError) as caught:
        opencode_mod.AGENT.document(empty, None, token=TOKEN)
    assert "No chat deployment is available on https://my-resource.services.ai.azure.com" in str(
        caught.value
    )
    assert "foundry configure" in str(caught.value)


# --------------------------------------------------------------------------- #
# Pi                                                                            #
# --------------------------------------------------------------------------- #


def test_pi_providers_document_is_exact(profile: Profile, sandbox: Sandbox) -> None:
    """Every key of ``providers.json``, including the asymmetric base URLs."""
    del sandbox
    document = pi_mod.AGENT.providers_document(profile, None, token=TOKEN)

    assert document == {
        "providers": {
            "foundry-claude": {
                # No /v1: anthropic-messages appends /v1/messages itself.
                "baseUrl": f"{ENDPOINT}/anthropic",
                "api": "anthropic-messages",
                "apiKey": TOKEN,
                "authHeader": True,
                "models": [{"id": OPUS}, {"id": SONNET}, {"id": HAIKU}],
            },
            "foundry-openai": {
                # /v1 here: openai-responses appends only /responses.
                "baseUrl": f"{ENDPOINT}/openai/v1",
                "api": "openai-responses",
                "apiKey": TOKEN,
                "models": [{"id": OPENAI_NEW}, {"id": OPENAI_OLD}],
            },
        }
    }


def test_pi_anthropic_provider_sends_a_bearer_header(profile: Profile, sandbox: Sandbox) -> None:
    """``authHeader: true`` is what makes the credential travel as a bearer."""
    del sandbox
    provider = pi_mod.AGENT.providers_document(profile, None, token=TOKEN)["providers"]
    assert provider["foundry-claude"]["authHeader"] is True
    # The OpenAI dialect has no such switch; asserting its absence keeps a stray
    # copy-paste from silently changing the header on that route.
    assert "authHeader" not in provider["foundry-openai"]


def test_pi_models_is_a_list_of_id_objects(profile: Profile, sandbox: Sandbox) -> None:
    """Pi's shape is a list of ``{"id": ...}`` -- unlike OpenCode's object."""
    del sandbox
    providers = pi_mod.AGENT.providers_document(profile, None, token=TOKEN)["providers"]
    assert providers["foundry-claude"]["models"] == [{"id": OPUS}, {"id": SONNET}, {"id": HAIKU}]
    assert providers["foundry-openai"]["models"] == [{"id": OPENAI_NEW}, {"id": OPENAI_OLD}]


@pytest.mark.parametrize(
    ("requested", "expected_provider", "expected_model"),
    [
        (None, "foundry-claude", SONNET),
        (OPUS, "foundry-claude", OPUS),
        (HAIKU, "foundry-claude", HAIKU),
        (OPENAI_NEW, "foundry-openai", OPENAI_NEW),
        (OPENAI_OLD, "foundry-openai", OPENAI_OLD),
        # Unknown to the profile and named like nothing: the OpenAI dialect is the
        # default guess, and the deployment name is still used verbatim.
        ("brand-new-gpt", "foundry-openai", "brand-new-gpt"),
        # Unknown but named like a Claude model: the Anthropic dialect.
        ("claude-experiment", "foundry-claude", "claude-experiment"),
    ],
)
def test_pi_settings_document_selects_the_default_provider(
    requested: str | None,
    expected_provider: str,
    expected_model: str,
    profile: Profile,
    sandbox: Sandbox,
) -> None:
    del sandbox
    assert pi_mod.AGENT.settings_document(profile, requested) == {
        "defaultProvider": expected_provider,
        "defaultModel": expected_model,
    }


def test_pi_write_config_writes_both_files(profile: Profile, sandbox: Sandbox) -> None:
    """providers.json and settings.json, parsed and compared key by key."""
    written = pi_mod.AGENT.write_config(profile, None)
    home = sandbox.home / "agents" / "pi"

    # providers.json leads: it carries the credential, so a crash between the two
    # writes leaves settings pointing at a stale provider rather than none.
    assert written == [home / "providers.json", home / "settings.json"]
    assert files_under(sandbox.home) == {"agents/pi/providers.json", "agents/pi/settings.json"}

    providers = json.loads((home / "providers.json").read_text(encoding="utf-8"))
    settings = json.loads((home / "settings.json").read_text(encoding="utf-8"))

    assert providers == pi_mod.AGENT.providers_document(profile, None, token=TOKEN)
    assert settings == {"defaultProvider": "foundry-claude", "defaultModel": SONNET}
    assert providers["providers"][settings["defaultProvider"]]["models"][1] == {"id": SONNET}
    assert providers["providers"]["foundry-claude"]["baseUrl"] == f"{ENDPOINT}/anthropic"
    assert providers["providers"]["foundry-openai"]["baseUrl"] == f"{ENDPOINT}/openai/v1"


def test_pi_configure_records_both_files(profile: Profile, sandbox: Sandbox) -> None:
    pi_mod.AGENT.configure(profile, None)
    home = sandbox.home / "agents" / "pi"

    entry = profile.agents["pi"]
    assert entry["home"] == str(home)
    assert entry["owns"] == [str(home / "providers.json"), str(home / "settings.json")]


def test_pi_isolation_variable_points_inside_the_app_dir(
    profile: Profile, sandbox: Sandbox, no_refresher: list[tuple[str, Any]]
) -> None:
    env = pi_mod.AGENT.launch_env(profile, None)
    home = sandbox.home / "agents" / "pi"

    assert env["PI_CODING_AGENT_DIR"] == str(home)
    assert Path(env["PI_CODING_AGENT_DIR"]).is_relative_to(profile_mod.app_dir())
    assert (home / "providers.json").is_file()
    assert [label for label, _ in no_refresher] == [f"pi:{home / 'providers.json'}"]


def test_pi_refresher_rewrites_only_the_credential(
    profile: Profile, sandbox: Sandbox, no_refresher: list[tuple[str, Any]]
) -> None:
    pi_mod.AGENT.launch_env(profile, None)
    path = pi_mod.AGENT.providers_path()
    before = json.loads(path.read_text(encoding="utf-8"))

    sandbox.token_value = TOKEN_AFTER_REFRESH
    _label, rewrite = no_refresher[0]
    rewrite()

    after = json.loads(path.read_text(encoding="utf-8"))
    assert after["providers"]["foundry-claude"]["apiKey"] == TOKEN_AFTER_REFRESH
    assert after["providers"]["foundry-openai"]["apiKey"] == TOKEN_AFTER_REFRESH
    assert _without_credentials(after) == _without_credentials(before)


def test_pi_argv_passes_user_arguments_through_untouched(
    profile: Profile, sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    del sandbox
    monkeypatch.setattr(pi_mod.AGENT, "executable", lambda: "/usr/local/bin/pi")
    assert pi_mod.AGENT.launch_argv(profile, ["-r", "--", "--help"]) == [
        "/usr/local/bin/pi",
        "-r",
        "--",
        "--help",
    ]


def test_pi_without_any_deployment_names_the_fix(sandbox: Sandbox) -> None:
    del sandbox
    empty = Profile(
        endpoint=ENDPOINT, subscription="", resource_group="", account="", deployments={}
    )
    with pytest.raises(AgentError) as caught:
        pi_mod.AGENT.providers_document(empty, None, token=TOKEN)
    assert "No chat deployment is available" in str(caught.value)
    assert "foundry configure" in str(caught.value)


# --------------------------------------------------------------------------- #
# Cross-agent invariants                                                        #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("module", "variable", "directory"),
    [
        (copilot_mod, "COPILOT_HOME", "copilot"),
        (opencode_mod, "OPENCODE_CONFIG_DIR", "opencode"),
        (pi_mod, "PI_CODING_AGENT_DIR", "pi"),
    ],
    ids=["copilot", "opencode", "pi"],
)
def test_every_isolation_variable_names_its_own_directory_under_the_app_dir(
    module: Any,
    variable: str,
    directory: str,
    profile: Profile,
    sandbox: Sandbox,
) -> None:
    """SPEC section 6.1: each agent gets a private root, so revert is a delete."""
    sandbox.api_key_value = API_KEY
    env = module.AGENT.provider_env(profile, None)
    assert env[variable] == str(sandbox.home / "agents" / directory)


@pytest.mark.parametrize(
    ("module", "expected"),
    [
        (copilot_mod, ("copilot", "copilot", "GitHub Copilot CLI")),
        (opencode_mod, ("opencode", "opencode", "OpenCode")),
        (pi_mod, ("pi", "pi", "Pi")),
    ],
    ids=["copilot", "opencode", "pi"],
)
def test_agent_identity(module: Any, expected: tuple[str, str, str]) -> None:
    agent = module.AGENT
    assert (agent.name, agent.binary, agent.display) == expected
    assert agent.install_hint.strip()


def _without_credentials(payload: Any) -> Any:
    """*payload* with every credential-shaped value blanked, for structural diffs."""
    if isinstance(payload, dict):
        return {
            key: ("<credential>" if key in ("apiKey", "headers") else _without_credentials(value))
            for key, value in payload.items()
        }
    if isinstance(payload, list):
        return [_without_credentials(item) for item in payload]
    return payload
