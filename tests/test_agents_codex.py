# SPDX-License-Identifier: Apache-2.0
"""Unit tests for :mod:`foundry.agents.codex` (agent reference section 2).

Codex is configured by one file, so every assertion here parses the generated
``config.toml`` and checks it key by key. Four of those keys have failure modes
that are invisible until Codex refuses to start: a reserved provider id, a
``wire_api`` of ``"chat"``, an empty provider ``name``, and a base URL that is
not the versionless ``/openai/v1`` route.

No subprocess is started and no network call is made; ``~/.foundry`` lives in
``tmp_path``.
"""

from __future__ import annotations

import os
import sys
import tomllib
from pathlib import Path

import pytest

from foundry import auth, console
from foundry import profile as profile_mod
from foundry.agents import base
from foundry.agents import codex as codex_agent
from foundry.agents.base import AgentError
from foundry.profile import Profile

ENDPOINT = "https://my-resource.services.ai.azure.com"
SUBSCRIPTION = "11111111-1111-1111-1111-111111111111"

NEWEST = "gpt-5.4"
OLDER = "gpt-4.1"

#: Codex rejects these provider ids outright.
RESERVED_PROVIDER_IDS = ("openai", "ollama", "lmstudio")

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
    "CODEX_HOME",
)

CODEX_BINARY = "/opt/bin/codex"


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
    monkeypatch.setattr("foundry.agents.base.which", lambda binary: CODEX_BINARY)
    yield
    auth.clear_token_cache()


@pytest.fixture
def foundry_exe(tmp_path, monkeypatch) -> Path:
    """Pin the path Codex will re-invoke, so the credential command is exact."""
    exe = tmp_path / "bin" / ("foundry.exe" if sys.platform == "win32" else "foundry")
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setenv("FOUNDRY_EXECUTABLE", str(exe))
    return exe


@pytest.fixture
def warnings(monkeypatch) -> list[str]:
    captured: list[str] = []
    monkeypatch.setattr(console, "warn", captured.append)
    return captured


def make_profile(
    *, openai=(NEWEST, OLDER), endpoint=ENDPOINT, subscription=SUBSCRIPTION
) -> Profile:
    return Profile(
        endpoint=endpoint,
        subscription=subscription,
        resource_group="rg-a",
        account="my-resource",
        deployments={
            "anthropic": {"opus": "claude-opus-4-7", "sonnet": "claude-sonnet-4-6"},
            "openai": list(openai),
        },
    )


AGENT = codex_agent.AGENT


def written(profile: Profile) -> dict:
    """Parse the config.toml this agent just wrote."""
    return tomllib.loads(AGENT.config_path().read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# The generated config.toml, key by key                                         #
# --------------------------------------------------------------------------- #


def test_config_toml_top_level_keys(foundry_exe) -> None:
    p = make_profile()

    AGENT.configure(p)
    document = written(p)

    assert document["model"] == NEWEST
    assert document["model_provider"] == "foundry"
    assert set(document) == {"model", "model_provider", "model_providers"}
    assert set(document["model_providers"]) == {"foundry"}


@pytest.mark.parametrize("reserved", RESERVED_PROVIDER_IDS)
def test_the_provider_id_is_never_a_reserved_one(foundry_exe, reserved: str) -> None:
    """``openai``/``ollama``/``lmstudio`` are reserved and rejected by Codex."""
    p = make_profile()
    AGENT.configure(p)
    document = written(p)

    assert codex_agent.PROVIDER_ID != reserved
    assert document["model_provider"] != reserved
    assert reserved not in document["model_providers"]


def test_provider_table(foundry_exe) -> None:
    p = make_profile()

    AGENT.configure(p)
    provider = written(p)["model_providers"]["foundry"]

    assert set(provider) == {"name", "base_url", "wire_api", "auth"}
    # A provider with an empty name fails Codex's validation.
    assert provider["name"] == "Microsoft Foundry"
    assert provider["name"].strip() != ""
    # Codex joins base + "/responses", so the /v1 belongs here...
    assert provider["base_url"] == "https://my-resource.services.ai.azure.com/openai/v1"
    assert provider["base_url"].endswith("/openai/v1")
    # ... and the Foundry OpenAI route takes no api-version.
    assert "api-version" not in provider["base_url"]
    assert "?" not in provider["base_url"]
    assert not provider["base_url"].endswith("/")
    # "chat" is a hard deserialization error in current Codex.
    assert provider["wire_api"] == "responses"
    assert provider["wire_api"] != "chat"


def test_the_resulting_request_url_is_the_responses_route(foundry_exe) -> None:
    """Codex joins ``base.trim_end_matches('/') + "/" + path``."""
    p = make_profile()
    AGENT.configure(p)
    base_url = written(p)["model_providers"]["foundry"]["base_url"]

    assert base_url.rstrip("/") + "/responses" == (
        "https://my-resource.services.ai.azure.com/openai/v1/responses"
    )


def test_auth_table_is_a_command_with_an_absolute_path(foundry_exe) -> None:
    """The credential is a command, not a value: Codex re-runs it on a timer."""
    p = make_profile()

    AGENT.configure(p)
    auth_table = written(p)["model_providers"]["foundry"]["auth"]

    assert set(auth_table) == {"command", "args", "timeout_ms", "refresh_interval_ms"}
    assert os.path.isabs(auth_table["command"])
    assert Path(auth_table["command"]) == foundry_exe
    assert auth_table["args"] == [
        "auth-token",
        "--endpoint",
        ENDPOINT,
        "--subscription",
        SUBSCRIPTION,
    ]
    assert auth_table["timeout_ms"] == 10_000
    assert auth_table["refresh_interval_ms"] == 900_000
    # Comfortably inside the measured 72-90 minute token lifetime.
    assert auth_table["refresh_interval_ms"] < 72 * 60 * 1000


def test_the_credential_command_falls_back_to_this_interpreter(monkeypatch) -> None:
    """No installed launcher: re-invoke ourselves, still by absolute path."""
    monkeypatch.delenv("FOUNDRY_EXECUTABLE", raising=False)
    monkeypatch.setattr(base.shutil, "which", lambda *args, **kwargs: None)
    monkeypatch.setattr(sys, "argv", ["pytest"])
    p = make_profile()

    AGENT.configure(p)
    auth_table = written(p)["model_providers"]["foundry"]["auth"]

    assert auth_table["command"] == os.path.abspath(sys.executable)
    assert os.path.isabs(auth_table["command"])
    assert auth_table["args"][:2] == ["-c", base.BOOTSTRAP]
    assert auth_table["args"][2:] == [
        "auth-token",
        "--endpoint",
        ENDPOINT,
        "--subscription",
        SUBSCRIPTION,
    ]


def test_the_credential_command_normalises_the_endpoint(foundry_exe) -> None:
    p = make_profile(endpoint="my-resource.openai.azure.com/api/projects/p1")

    AGENT.configure(p)
    args = written(p)["model_providers"]["foundry"]["auth"]["args"]

    assert args[1:3] == ["--endpoint", ENDPOINT]


def test_no_subscription_means_no_subscription_flag(foundry_exe) -> None:
    p = make_profile(subscription="")

    AGENT.configure(p)
    args = written(p)["model_providers"]["foundry"]["auth"]["args"]

    assert args == ["auth-token", "--endpoint", ENDPOINT]
    assert "--subscription" not in args


def test_the_static_credential_mechanisms_are_not_used(foundry_exe) -> None:
    """``env_key`` is static and ``experimental_bearer_token`` is discouraged."""
    p = make_profile()
    AGENT.configure(p)
    provider = written(p)["model_providers"]["foundry"]

    assert "env_key" not in provider
    assert "experimental_bearer_token" not in provider
    assert "query_params" not in provider


def test_no_token_is_ever_written_to_the_file(foundry_exe, monkeypatch) -> None:
    monkeypatch.setenv("FOUNDRY_BEARER", "super-secret-bearer")
    p = make_profile()

    AGENT.configure(p)

    assert "super-secret-bearer" not in AGENT.config_path().read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# The file on disk                                                              #
# --------------------------------------------------------------------------- #


def test_the_config_lives_in_the_agent_home_and_nowhere_else(foundry_exe) -> None:
    p = make_profile()

    AGENT.configure(p)

    home = profile_mod.app_dir() / "agents" / "codex"
    assert AGENT.config_path() == home / "config.toml"
    assert AGENT.config_path().is_file()
    # The user's own ~/.codex is never touched.
    assert Path.home() / ".codex" not in AGENT.config_path().parents
    # No temp file survives the atomic replace.
    assert [f.name for f in home.iterdir()] == ["config.toml"]


def test_the_file_is_utf8_with_a_trailing_newline_and_a_header(foundry_exe) -> None:
    p = make_profile()

    AGENT.configure(p)
    text = AGENT.config_path().read_text(encoding="utf-8")

    assert text.endswith("\n")
    assert text.startswith("# Written by foundry.")
    assert "model_providers.foundry" in text
    assert "CODEX_HOME" in text


def test_configure_records_exactly_what_it_owns(foundry_exe) -> None:
    p = make_profile()

    AGENT.configure(p)

    entry = p.agents["codex"]
    assert entry["home"] == str(profile_mod.app_dir() / "agents" / "codex")
    assert entry["owns"] == [
        str(AGENT.config_path()),
        "model",
        "model_provider",
        "model_providers.foundry",
    ]
    assert entry["model"] == NEWEST
    assert entry["configured_at"].endswith("Z")


def test_configure_is_idempotent(foundry_exe) -> None:
    p = make_profile()

    AGENT.configure(p)
    first = AGENT.config_path().read_text(encoding="utf-8")
    AGENT.configure(p)
    second = AGENT.config_path().read_text(encoding="utf-8")

    assert first == second


USER_CONFIG = """\
# my own notes
approval_policy = "on-request"
model = "stale-model"
model_provider = "something-else"

[model_providers.something-else]
name = "Other"
base_url = "https://other.example"

[projects."/home/me/work"]
trust_level = "trusted"
"""


def test_user_keys_and_other_providers_are_preserved(foundry_exe) -> None:
    """The file is ours, but the user may well have added to it."""
    p = make_profile()
    home = profile_mod.app_dir() / "agents" / "codex"
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text(USER_CONFIG, encoding="utf-8")

    AGENT.configure(p, model="gpt-5.4-mini")
    document = written(p)

    assert document["approval_policy"] == "on-request"
    assert document["projects"]["/home/me/work"]["trust_level"] == "trusted"
    assert document["model_providers"]["something-else"] == {
        "name": "Other",
        "base_url": "https://other.example",
    }
    assert "# my own notes" in AGENT.config_path().read_text(encoding="utf-8")
    # ... and the owned keys -- only those -- are updated.
    assert document["model"] == "gpt-5.4-mini"
    assert document["model_provider"] == "foundry"
    assert document["model_providers"]["foundry"]["wire_api"] == "responses"
    assert document["model_providers"]["foundry"]["base_url"].endswith("/openai/v1")


def test_an_unparseable_config_is_backed_up_and_rewritten(foundry_exe, warnings) -> None:
    """The failure isolation was adopted to survive, applied to our own file."""
    p = make_profile()
    home = profile_mod.app_dir() / "agents" / "codex"
    home.mkdir(parents=True, exist_ok=True)
    garbage = "this is not = = valid toml [[[\n"
    (home / "config.toml").write_text(garbage, encoding="utf-8")

    AGENT.configure(p)

    document = written(p)
    assert document["model_provider"] == "foundry"

    backups = list((profile_mod.app_dir() / "backups").iterdir())
    assert len(backups) == 1
    assert backups[0].name.startswith("config.toml.")
    assert backups[0].name.endswith(".bak")
    assert backups[0].read_text(encoding="utf-8") == garbage

    assert len(warnings) == 1
    assert "not valid TOML" in warnings[0]
    assert str(backups[0]) in warnings[0]


# --------------------------------------------------------------------------- #
# Model selection                                                               #
# --------------------------------------------------------------------------- #


def test_default_model_is_the_newest_openai_deployment(foundry_exe) -> None:
    """Only the OpenAI families: this provider speaks the Responses wire API."""
    p = make_profile()

    assert AGENT.default_model(p) == NEWEST
    AGENT.configure(p)
    assert written(p)["model"] == NEWEST


def test_an_anthropic_only_resource_has_no_codex_model(foundry_exe) -> None:
    p = make_profile(openai=())

    assert AGENT.default_model(p) is None
    with pytest.raises(AgentError) as excinfo:
        AGENT.configure(p)

    message = str(excinfo.value)
    assert "my-resource" in message
    assert "no OpenAI-family chat model deployed" in message
    assert "Microsoft Foundry portal" in message
    assert "foundry codex --model <deployment>" in message
    assert not AGENT.config_path().exists()


@pytest.mark.parametrize("deployment", ["gpt-5.4-mini", "Weird_Deployment.Name-v2", "prod-fast"])
def test_an_explicit_model_is_written_verbatim(foundry_exe, deployment: str) -> None:
    """SPEC section 6.4: never rewritten, suffixed or canonicalised."""
    p = make_profile()

    AGENT.configure(p, model=deployment)

    assert written(p)["model"] == deployment
    assert p.agents["codex"]["model"] == deployment


def test_an_explicit_model_need_not_be_a_known_deployment(foundry_exe) -> None:
    p = make_profile(openai=())

    AGENT.configure(p, model="published-yesterday")

    assert written(p)["model"] == "published-yesterday"


# --------------------------------------------------------------------------- #
# Launch                                                                        #
# --------------------------------------------------------------------------- #


def test_codex_home_points_inside_the_app_dir(foundry_exe) -> None:
    p = make_profile()
    AGENT.configure(p)

    env = AGENT.launch_env(p)
    home = Path(env["CODEX_HOME"])

    assert home == profile_mod.app_dir() / "agents" / "codex"
    assert home.is_absolute()
    assert home.is_dir()
    assert (home / "config.toml").is_file()
    assert profile_mod.app_dir() in home.parents
    assert Path.home() / ".codex" != home


def test_launch_env_changes_nothing_but_codex_home(foundry_exe) -> None:
    """Codex fetches its own credential; there is nothing else to hand it."""
    p = make_profile()

    env = AGENT.launch_env(p)

    added = {k: v for k, v in env.items() if os.environ.get(k) != v}
    assert added == {"CODEX_HOME": str(profile_mod.app_dir() / "agents" / "codex")}
    assert set(os.environ) - set(env) == set()


def test_launch_argv_is_the_binary_then_every_argument_verbatim(foundry_exe) -> None:
    p = make_profile()

    argv = AGENT.launch_argv(p, ["exec", "--full-auto", "--", "-c", "x=1"])

    assert argv == [CODEX_BINARY, "exec", "--full-auto", "--", "-c", "x=1"]


def test_a_missing_binary_names_the_install_command(monkeypatch, foundry_exe) -> None:
    monkeypatch.setattr("foundry.agents.base.which", lambda binary: None)
    p = make_profile()

    assert AGENT.is_installed() is False
    with pytest.raises(AgentError) as excinfo:
        AGENT.launch_argv(p, [])

    assert "npm install -g @openai/codex" in str(excinfo.value)


def test_revert_deletes_the_home_directory_and_forgets_the_agent(foundry_exe) -> None:
    p = make_profile()
    AGENT.configure(p)
    home = profile_mod.app_dir() / "agents" / "codex"

    AGENT.revert(p)

    assert not home.exists()
    assert "codex" not in p.agents


def test_agent_identity() -> None:
    assert AGENT.name == "codex"
    assert AGENT.binary == "codex"
    assert AGENT.display == "Codex"
    assert codex_agent.CONFIG_NAME == "config.toml"
