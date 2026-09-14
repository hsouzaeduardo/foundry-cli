# SPDX-License-Identifier: Apache-2.0
"""OpenCode, pointed at Microsoft Foundry (agent reference section 4).

OpenCode is configured by a JSON file naming AI-SDK provider packages, both of
which ship with it. Two details are worth reading twice.

**The base URLs are not the same shape.** ``@ai-sdk/anthropic`` posts to
``{baseURL}/messages`` while ``@ai-sdk/openai`` posts to ``{baseURL}/responses``,
so the Anthropic provider's base URL carries the ``/v1`` segment
(``E/anthropic/v1``) and Pi's -- for the identical route -- does not. Get this
wrong and every request 404s.

**The credential is static.** OpenCode reads ``apiKey`` from the config file and
has no refresh hook, so :func:`foundry.agents.start_refresher` rewrites the file
every 30 minutes for as long as the launcher lives (measured Foundry token
lifetime is 72-90 minutes).

The token is written into the file as a literal rather than through the
``{env:VAR}`` interpolation the vendor documents. That is deliberate: an
environment variable cannot be changed in a running child, so ``{env:VAR}``
would make the refresh above impossible. The file lives in ``~/.foundry``, is
written 0600, and is never read from the user's own configuration.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

from foundry import auth, endpoints
from foundry import profile as _profile
from foundry.agents import AgentError, dialect, preferred_model, start_refresher, write_json

if TYPE_CHECKING:
    from foundry.profile import Profile

NAME = "opencode"
BINARY = "opencode"
DISPLAY = "OpenCode"

INSTALL_HINT = "npm install -g opencode-ai   (or: curl -fsSL https://opencode.ai/install | bash)"

#: OpenCode reads ``opencode.json`` from its config directory. ``OPENCODE_CONFIG``
#: is set to the same file as well, so the file in use is unambiguous whichever
#: of the two variables the installed version honours.
CONFIG_NAME = "opencode.json"

SCHEMA_URL = "https://opencode.ai/config.json"

#: Provider ids. Two of them, because one provider block can carry only one npm
#: package and Foundry serves the Anthropic and OpenAI dialects on different
#: routes. The ``foundry-`` prefix keeps them clear of OpenCode's built-ins.
ANTHROPIC_PROVIDER = "foundry-anthropic"
OPENAI_PROVIDER = "foundry-openai"

ANTHROPIC_NPM = "@ai-sdk/anthropic"
OPENAI_NPM = "@ai-sdk/openai"


class OpenCode:
    """The ``foundry opencode`` agent."""

    name = NAME
    binary = BINARY
    display = DISPLAY
    install_hint = INSTALL_HINT

    #: The config file is rewritten on a timer while the child runs.
    refreshes_in_flight = True

    # -- installation ----------------------------------------------------

    def is_installed(self) -> bool:
        return shutil.which(self.binary) is not None

    def executable(self) -> str:
        """Absolute path to the CLI, or the bare name if it is not on PATH."""
        return shutil.which(self.binary) or self.binary

    def missing_message(self) -> str:
        return (
            f"{self.display} is not installed (`{self.binary}` is not on PATH).\n"
            f"Install it with: {self.install_hint}"
        )

    # -- paths -----------------------------------------------------------

    def home(self) -> Path:
        """``~/.foundry/agents/opencode`` -- what ``OPENCODE_CONFIG_DIR`` points at."""
        return _profile.agent_home(self.name)

    def config_path(self) -> Path:
        return self.home() / CONFIG_NAME

    # -- model -----------------------------------------------------------

    def default_model(self, profile: Profile) -> str | None:
        return preferred_model(profile)

    def provider_for(self, profile: Profile, model: str) -> str:
        """Which of the two provider blocks serves *model*."""
        return ANTHROPIC_PROVIDER if dialect(profile, model) == "anthropic" else OPENAI_PROVIDER

    # -- configuration ---------------------------------------------------

    def document(self, profile: Profile, model: str | None, *, token: str) -> dict[str, Any]:
        """The exact JSON object written to ``opencode.json``.

        Pure, so a test can assert the bytes without a filesystem or a token
        mint. Every deployment the profile knows about is listed, so the user
        can switch models inside OpenCode without coming back here; only the
        top-level ``model`` names the one to start with.
        """
        chosen = self._require_model(profile, model)
        endpoint = endpoints.normalize(profile.endpoint)

        anthropic_models = [
            name
            for name in (
                profile.anthropic("opus"),
                profile.anthropic("sonnet"),
                profile.anthropic("haiku"),
            )
            if name
        ]
        openai_models = list(profile.openai())

        provider_id = self.provider_for(profile, chosen)
        target = anthropic_models if provider_id == ANTHROPIC_PROVIDER else openai_models
        if chosen not in target:
            # A deployment named with --model that the last scan did not see.
            # It still has to appear in `models`, or OpenCode will not offer it.
            target.insert(0, chosen)

        providers: dict[str, Any] = {}
        if anthropic_models:
            providers[ANTHROPIC_PROVIDER] = {
                "npm": ANTHROPIC_NPM,
                "name": "Microsoft Foundry (Anthropic)",
                "options": {
                    # /v1 belongs here: @ai-sdk/anthropic appends only /messages.
                    "baseURL": f"{endpoints.anthropic_base(endpoint)}/v1",
                    "apiKey": token,
                    # @ai-sdk/anthropic sends the credential as `x-api-key`,
                    # which Foundry does not document accepting; the data plane
                    # was verified against `Authorization: Bearer`. Sending both
                    # costs one header and removes the guess.
                    "headers": {"Authorization": f"Bearer {token}"},
                },
                "models": {name: {} for name in anthropic_models},
            }
        if openai_models:
            providers[OPENAI_PROVIDER] = {
                "npm": OPENAI_NPM,
                "name": "Microsoft Foundry (OpenAI)",
                "options": {
                    "baseURL": endpoints.openai_base(endpoint),
                    "apiKey": token,
                },
                "models": {name: {} for name in openai_models},
            }

        return {
            "$schema": SCHEMA_URL,
            "model": f"{provider_id}/{chosen}",
            "provider": providers,
        }

    def write_config(self, profile: Profile, model: str | None) -> Path:
        """Mint a credential and (re)write ``opencode.json``. Returns its path.

        Also the refresher's unit of work: writing the whole document keeps one
        code path for "the config on disk", so a refresh can never disagree with
        a launch about anything but the token.
        """
        token = auth.token(subscription=profile.subscription or None)
        return write_json(self.config_path(), self.document(profile, model, token=token))

    def configure(self, profile: Profile, model: str | None) -> None:
        path = self.write_config(profile, model)
        _profile.record_agent(profile, self.name, home=self.home(), owns=[str(path)])

    def refresh_credential(self, profile: Profile, model: str | None = None) -> Path:
        """Rewrite the config with a freshly minted token."""
        return self.write_config(profile, model)

    # -- launch ----------------------------------------------------------

    def provider_env(self, profile: Profile, model: str | None) -> dict[str, str]:
        """Exactly the variables ``foundry`` sets."""
        del profile, model
        return {
            "OPENCODE_CONFIG_DIR": str(self.home()),
            "OPENCODE_CONFIG": str(self.config_path()),
        }

    def launch_env(self, profile: Profile, model: str | None) -> dict[str, str]:
        """The complete environment for the child, and the refresher started.

        Writing the config here as well as in :meth:`configure` is on purpose: a
        profile configured last week holds a token that died the same hour, and
        the only moment a credential is certainly wanted is the moment of
        launch.
        """
        self.write_config(profile, model)
        start_refresher(
            f"{self.name}:{self.config_path()}",
            lambda: self.write_config(profile, model),
        )
        env = auth.scrubbed_env()
        env.update(self.provider_env(profile, model))
        return env

    def launch_argv(self, profile: Profile, args: list[str]) -> list[str]:
        """``opencode`` plus the user's arguments, untouched.

        The model is already the config's top-level ``model``; injecting
        ``--model`` as well would only add a second place for it to be wrong.
        """
        del profile
        return [self.executable(), *args]

    # -- internals -------------------------------------------------------

    def _require_model(self, profile: Profile, model: str | None) -> str:
        chosen = (model or "").strip() or self.default_model(profile)
        if not chosen:
            raise AgentError(
                "No chat deployment is available on "
                f"{endpoints.normalize(profile.endpoint)}.\n"
                "Publish one in the Foundry portal, then run `foundry configure`."
            )
        return chosen


#: The instance the registry loads.
AGENT = OpenCode()
