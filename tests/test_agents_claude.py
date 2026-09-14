# SPDX-License-Identifier: Apache-2.0
"""Unit tests for :mod:`foundry.agents.claude` (agent reference section 1).

Claude Code is configured entirely through the child environment, so almost
every assertion here is about the exact variables handed to it -- including the
ones that must *not* be there. No subprocess is started, no network call is
made, and ``~/.foundry`` lives in ``tmp_path``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from foundry import auth, console
from foundry import profile as profile_mod
from foundry.agents import claude as claude_agent
from foundry.agents.base import AgentError
from foundry.profile import Profile

ENDPOINT = "https://my-resource.services.ai.azure.com"
SUBSCRIPTION = "11111111-1111-1111-1111-111111111111"

OPUS = "claude-opus-4-7"
SONNET = "claude-sonnet-4-6"
HAIKU = "claude-haiku-4-5"

#: The three variables whose absence turns a launch into a request for a model
#: that is not deployed (agent reference section 1: pinning is mandatory).
PIN_VARS = (
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
)

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
    "CLAUDE_CONFIG_DIR",
    "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_FOUNDRY_RESOURCE",
    "ANTHROPIC_FOUNDRY_BASE_URL",
    "ANTHROPIC_FOUNDRY_AUTH_TOKEN",
    "ANTHROPIC_FOUNDRY_API_KEY",
    *PIN_VARS,
)

CLAUDE_BINARY = "/opt/bin/claude"


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
    # scrubbed_env() consults the active az account to decide whether an ambient
    # service principal is the current one. "Cannot verify" is the honest
    # default for a test that has no az at all.
    monkeypatch.setattr(auth, "account", lambda: None)
    # Resolve the binary without requiring Claude Code to be installed.
    monkeypatch.setattr("foundry.agents.base.which", lambda binary: CLAUDE_BINARY)
    yield
    auth.clear_token_cache()


@pytest.fixture
def warnings(monkeypatch) -> list[str]:
    """Capture everything the agent warns about."""
    captured: list[str] = []
    monkeypatch.setattr(console, "warn", captured.append)
    return captured


def make_profile(**deployments: str) -> Profile:
    """A profile publishing exactly the named Anthropic families."""
    return Profile(
        endpoint=ENDPOINT,
        subscription=SUBSCRIPTION,
        resource_group="rg-a",
        account="my-resource",
        deployments={
            "anthropic": dict(deployments),
            "openai": ["gpt-5.4", "gpt-5.4-mini"],
        },
    )


@pytest.fixture
def full_profile() -> Profile:
    return make_profile(opus=OPUS, sonnet=SONNET, haiku=HAIKU)


AGENT = claude_agent.AGENT


# --------------------------------------------------------------------------- #
# The environment handed to Claude Code                                         #
# --------------------------------------------------------------------------- #


def test_launch_env_sets_exactly_the_six_foundry_variables(full_profile) -> None:
    """Native Foundry mode is six variables; anything else is inherited."""
    env = AGENT.launch_env(full_profile)

    added = {k: v for k, v in env.items() if os.environ.get(k) != v}
    assert added == {
        "CLAUDE_CONFIG_DIR": str(profile_mod.app_dir() / "agents" / "claude"),
        "CLAUDE_CODE_USE_FOUNDRY": "1",
        "ANTHROPIC_FOUNDRY_RESOURCE": "my-resource",
        "ANTHROPIC_DEFAULT_OPUS_MODEL": OPUS,
        "ANTHROPIC_DEFAULT_SONNET_MODEL": SONNET,
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": HAIKU,
    }
    # Nothing was dropped from a clean parent environment.
    assert set(os.environ) - set(env) == set()
    assert env.get("PATH") == os.environ.get("PATH")


def test_all_three_aliases_are_pinned_to_real_deployment_names(full_profile) -> None:
    """Unpinned, ``opus``/``sonnet`` resolve to defaults that may not exist here."""
    env = AGENT.launch_env(full_profile)

    assert env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == OPUS
    assert env["ANTHROPIC_DEFAULT_SONNET_MODEL"] == SONNET
    assert env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == HAIKU
    assert all(env[var] for var in PIN_VARS)


def test_native_foundry_mode_is_switched_on(full_profile) -> None:
    env = AGENT.launch_env(full_profile)

    assert env["CLAUDE_CODE_USE_FOUNDRY"] == "1"
    assert env["ANTHROPIC_FOUNDRY_RESOURCE"] == "my-resource"


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://my-resource.services.ai.azure.com",
        "https://my-resource.services.ai.azure.com/",
        "my-resource",
        "my-resource.openai.azure.com",
        "https://my-resource.services.ai.azure.com/api/projects/p1",
    ],
)
def test_the_resource_variable_is_the_bare_name_not_a_url(endpoint: str) -> None:
    """``ANTHROPIC_FOUNDRY_RESOURCE`` takes a resource name, never a host."""
    p = make_profile(opus=OPUS, sonnet=SONNET, haiku=HAIKU)
    p.endpoint = endpoint

    env = AGENT.launch_env(p)

    assert env["ANTHROPIC_FOUNDRY_RESOURCE"] == "my-resource"


def test_claude_config_dir_points_inside_the_app_dir(full_profile) -> None:
    """The user's own ``~/.claude`` is never read and never written."""
    env = AGENT.launch_env(full_profile)
    config_dir = Path(env["CLAUDE_CONFIG_DIR"])

    assert config_dir == profile_mod.app_dir() / "agents" / "claude"
    assert config_dir.is_absolute()
    assert config_dir.is_dir()
    assert profile_mod.app_dir() in config_dir.parents
    assert Path.home() / ".claude" != config_dir


def test_no_custom_base_url_is_produced(full_profile) -> None:
    """Native mode builds its own URL; a base URL here would fight it."""
    env = AGENT.launch_env(full_profile)

    assert "ANTHROPIC_BASE_URL" not in env
    assert "ANTHROPIC_FOUNDRY_BASE_URL" not in env
    assert not [v for v in env.values() if "services.ai.azure.com" in v]
    assert not [v for v in env.values() if "/anthropic" in v]


def test_an_inherited_base_url_or_rival_provider_is_removed(full_profile, monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://someone-elses-proxy.example")
    monkeypatch.setenv("ANTHROPIC_FOUNDRY_BASE_URL", "https://old.services.ai.azure.com/anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-inherited")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "inherited-bearer")
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    monkeypatch.setenv("CLAUDE_CODE_USE_VERTEX", "1")

    env = AGENT.launch_env(full_profile)

    for name in claude_agent._CONFLICTING:
        assert name not in env, name


def test_no_api_key_helper_and_no_settings_file_are_produced(full_profile) -> None:
    """Foundry mode handles auth itself: no helper, no credential of any kind."""
    AGENT.configure(full_profile)
    env = AGENT.launch_env(full_profile)
    home = Path(env["CLAUDE_CONFIG_DIR"])

    # Nothing at all is written -- no settings.json holding an apiKeyHelper.
    assert list(home.iterdir()) == []
    assert "--settings" not in AGENT.launch_argv(full_profile, [])
    assert not [k for k in env if "helper" in k.lower()]
    assert not [v for v in env.values() if "apiKeyHelper" in v]
    # And no credential is handed over: the SDK chain refreshes itself.
    assert "ANTHROPIC_FOUNDRY_AUTH_TOKEN" not in env
    assert "ANTHROPIC_FOUNDRY_API_KEY" not in env


# --------------------------------------------------------------------------- #
# The service-principal trap (AADSTS7000222)                                    #
# --------------------------------------------------------------------------- #


def test_service_principal_variables_are_scrubbed(full_profile, monkeypatch) -> None:
    """``EnvironmentCredential`` runs first and fails the whole chain on these."""
    monkeypatch.setenv("AZURE_CLIENT_ID", "00000000-0000-0000-0000-00000000dead")
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "expired-secret")
    monkeypatch.setenv("AZURE_TENANT_ID", "99999999-9999-9999-9999-999999999999")

    env = AGENT.launch_env(full_profile)

    for name in auth.SP_ENV_VARS:
        assert name not in env, name
    assert "expired-secret" not in env.values()


def test_a_partial_service_principal_is_scrubbed_too(full_profile, monkeypatch) -> None:
    """A lone client id also selects a user-assigned managed identity."""
    monkeypatch.setenv("AZURE_CLIENT_ID", "00000000-0000-0000-0000-00000000dead")

    env = AGENT.launch_env(full_profile)

    assert "AZURE_CLIENT_ID" not in env


def test_a_verified_current_service_principal_survives(full_profile, monkeypatch) -> None:
    """The scrub is delegated to ``auth.scrubbed_env``, not reimplemented here."""
    tenant = "77777777-7777-7777-7777-777777777777"
    monkeypatch.setenv("AZURE_CLIENT_ID", "00000000-0000-0000-0000-00000000beef")
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "still-valid")
    monkeypatch.setenv("AZURE_TENANT_ID", tenant)
    monkeypatch.setattr(auth, "account", lambda: {"tenantId": tenant})
    monkeypatch.setattr(auth, "_sp_token_works", lambda *args: True)

    env = AGENT.launch_env(full_profile)

    assert env["AZURE_CLIENT_ID"] == "00000000-0000-0000-0000-00000000beef"
    assert env["AZURE_TENANT_ID"] == tenant


# --------------------------------------------------------------------------- #
# Credentials and the escape hatches (SPEC section 3)                           #
# --------------------------------------------------------------------------- #


def test_an_inherited_foundry_credential_is_dropped(full_profile, monkeypatch) -> None:
    """A stale bearer outranks the chain and would kill the session at expiry."""
    monkeypatch.setenv("ANTHROPIC_FOUNDRY_AUTH_TOKEN", "stale-token")
    monkeypatch.setenv("ANTHROPIC_FOUNDRY_API_KEY", "stale-key")

    env = AGENT.launch_env(full_profile)

    assert "ANTHROPIC_FOUNDRY_AUTH_TOKEN" not in env
    assert "ANTHROPIC_FOUNDRY_API_KEY" not in env


def test_foundry_api_key_is_passed_through_as_the_vendor_key(full_profile, monkeypatch) -> None:
    monkeypatch.setenv("FOUNDRY_API_KEY", "  resource-key  ")

    env = AGENT.launch_env(full_profile)

    assert env["ANTHROPIC_FOUNDRY_API_KEY"] == "resource-key"
    assert "ANTHROPIC_FOUNDRY_AUTH_TOKEN" not in env


def test_foundry_bearer_is_passed_through_and_warned_about(
    full_profile, monkeypatch, warnings
) -> None:
    monkeypatch.setenv("FOUNDRY_BEARER", "pre-minted-token")

    env = AGENT.launch_env(full_profile)

    assert env["ANTHROPIC_FOUNDRY_AUTH_TOKEN"] == "pre-minted-token"
    assert "ANTHROPIC_FOUNDRY_API_KEY" not in env
    assert len(warnings) == 1
    assert "FOUNDRY_BEARER" in warnings[0]
    assert "az login" in warnings[0]


def test_the_api_key_outranks_the_bearer(full_profile, monkeypatch, warnings) -> None:
    monkeypatch.setenv("FOUNDRY_API_KEY", "resource-key")
    monkeypatch.setenv("FOUNDRY_BEARER", "pre-minted-token")

    env = AGENT.launch_env(full_profile)

    assert env["ANTHROPIC_FOUNDRY_API_KEY"] == "resource-key"
    assert "ANTHROPIC_FOUNDRY_AUTH_TOKEN" not in env
    assert warnings == []


# --------------------------------------------------------------------------- #
# Model selection                                                               #
# --------------------------------------------------------------------------- #


def test_an_explicit_model_is_passed_verbatim(full_profile) -> None:
    """SPEC section 6.4: never rewritten, suffixed or canonicalised."""
    env = AGENT.launch_env(full_profile, "Weird_Deployment.Name-v2")

    assert env["ANTHROPIC_MODEL"] == "Weird_Deployment.Name-v2"


def test_no_model_means_no_anthropic_model_variable(full_profile, monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_MODEL", "inherited-model")

    env = AGENT.launch_env(full_profile)

    assert "ANTHROPIC_MODEL" not in env


@pytest.mark.parametrize(
    ("published", "expected"),
    [
        ({"opus": OPUS, "sonnet": SONNET, "haiku": HAIKU}, SONNET),
        ({"opus": OPUS, "haiku": HAIKU}, OPUS),
        ({"haiku": HAIKU}, HAIKU),
        ({}, None),
    ],
)
def test_default_model_prefers_sonnet(published: dict, expected: str | None) -> None:
    """Sonnet is what Claude Code picks unasked; opus would silently cost more."""
    assert AGENT.default_model(make_profile(**published)) == expected


# --------------------------------------------------------------------------- #
# The documented fallback: a family with no deployment                          #
# --------------------------------------------------------------------------- #

#: (published families, expected pins). All three aliases are pinned in every
#: case -- leaving one unpinned means Claude Code asks Foundry for a model that
#: is not there.
SUBSTITUTION_CASES: tuple[tuple[dict, dict, tuple[str, ...]], ...] = (
    (
        {"sonnet": SONNET},
        {"opus": SONNET, "sonnet": SONNET, "haiku": SONNET},
        ("opus", "haiku"),
    ),
    (
        {"opus": OPUS},
        {"opus": OPUS, "sonnet": OPUS, "haiku": OPUS},
        ("sonnet", "haiku"),
    ),
    (
        {"haiku": HAIKU},
        {"opus": HAIKU, "sonnet": HAIKU, "haiku": HAIKU},
        ("opus", "sonnet"),
    ),
    (
        {"opus": OPUS, "sonnet": SONNET},
        {"opus": OPUS, "sonnet": SONNET, "haiku": SONNET},
        ("haiku",),
    ),
    (
        {"opus": OPUS, "haiku": HAIKU},
        # sonnet prefers opus over haiku when it must borrow.
        {"opus": OPUS, "sonnet": OPUS, "haiku": HAIKU},
        ("sonnet",),
    ),
    (
        {"sonnet": SONNET, "haiku": HAIKU},
        {"opus": SONNET, "sonnet": SONNET, "haiku": HAIKU},
        ("opus",),
    ),
)


@pytest.mark.parametrize(("published", "expected", "borrowed"), SUBSTITUTION_CASES)
def test_a_family_with_no_deployment_borrows_one(
    published: dict, expected: dict, borrowed: tuple[str, ...]
) -> None:
    pins, substituted = claude_agent.model_pins(make_profile(**published))

    assert pins == {claude_agent.MODEL_ENV[fam]: name for fam, name in expected.items()}
    assert [fam for fam, _ in substituted] == list(borrowed)
    assert [name for _, name in substituted] == [expected[fam] for fam in borrowed]


def test_a_substitution_is_announced_never_silent(warnings) -> None:
    AGENT.configure(make_profile(sonnet=SONNET))

    assert len(warnings) == 2
    joined = "\n".join(warnings)
    assert "ANTHROPIC_DEFAULT_OPUS_MODEL" in joined
    assert "ANTHROPIC_DEFAULT_HAIKU_MODEL" in joined
    assert joined.count(SONNET) == 2
    assert "my-resource" in joined


def test_a_resource_with_every_family_warns_about_nothing(full_profile, warnings) -> None:
    AGENT.configure(full_profile)

    assert warnings == []


def test_no_anthropic_deployment_at_all_names_the_fix() -> None:
    empty = make_profile()

    with pytest.raises(AgentError) as excinfo:
        AGENT.launch_env(empty)

    message = str(excinfo.value)
    assert "my-resource" in message
    assert "no Anthropic model deployed" in message
    assert "Microsoft Foundry portal" in message
    assert "foundry configure" in message


def test_configure_fails_early_rather_than_inside_claude_code() -> None:
    with pytest.raises(AgentError):
        AGENT.configure(make_profile())


# --------------------------------------------------------------------------- #
# Bookkeeping and launch                                                        #
# --------------------------------------------------------------------------- #


def test_configure_records_the_home_directory_and_the_pins(full_profile) -> None:
    AGENT.configure(full_profile, model="claude-opus-4-7")

    entry = full_profile.agents["claude"]
    home = str(profile_mod.app_dir() / "agents" / "claude")
    assert entry["home"] == home
    assert entry["owns"] == [home]
    assert entry["model"] == "claude-opus-4-7"
    assert entry["pins"] == {
        "ANTHROPIC_DEFAULT_OPUS_MODEL": OPUS,
        "ANTHROPIC_DEFAULT_SONNET_MODEL": SONNET,
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": HAIKU,
    }
    assert entry["configured_at"].endswith("Z")
    assert full_profile.is_configured("claude")


def test_configure_is_idempotent(full_profile) -> None:
    AGENT.configure(full_profile)
    first = dict(full_profile.agents["claude"])
    AGENT.configure(full_profile)
    second = dict(full_profile.agents["claude"])

    assert first["owns"] == second["owns"]
    assert first["pins"] == second["pins"]
    assert list(full_profile.agents) == ["claude"]


def test_revert_deletes_the_home_directory_and_forgets_the_agent(full_profile) -> None:
    AGENT.configure(full_profile)
    home = profile_mod.app_dir() / "agents" / "claude"
    (home / "leftover.json").write_text("{}", encoding="utf-8")

    AGENT.revert(full_profile)

    assert not home.exists()
    assert "claude" not in full_profile.agents


def test_launch_argv_is_the_binary_then_every_argument_verbatim(full_profile) -> None:
    argv = AGENT.launch_argv(full_profile, ["-r", "--model", "x", "--", "-r"])

    assert argv == [CLAUDE_BINARY, "-r", "--model", "x", "--", "-r"]


def test_a_missing_binary_names_the_install_command(full_profile, monkeypatch) -> None:
    monkeypatch.setattr("foundry.agents.base.which", lambda binary: None)

    assert AGENT.is_installed() is False
    with pytest.raises(AgentError) as excinfo:
        AGENT.launch_argv(full_profile, [])

    assert "npm install -g @anthropic-ai/claude-code" in str(excinfo.value)


def test_agent_identity() -> None:
    assert AGENT.name == "claude"
    assert AGENT.binary == "claude"
    assert AGENT.display == "Claude Code"
