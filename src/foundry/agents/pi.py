# SPDX-License-Identifier: Apache-2.0
"""Pi, pointed at Microsoft Foundry (agent reference section 5).

Pi names the dialect explicitly (``api``) and derives the appended path from it,
which makes the base URLs asymmetric in exactly the way that catches people out:

* ``anthropic-messages`` appends ``/v1/messages`` -- so the base URL is
  ``E/anthropic``, **without** ``/v1``.
* ``openai-responses`` appends ``/responses`` -- so the base URL is
  ``E/openai/v1``, **with** it.

The same Foundry route therefore gets a different base URL here than it does in
:mod:`foundry.agents.opencode`, whose Anthropic provider appends only
``/messages``. Both end at ``E/anthropic/v1/messages``.

``authHeader: true`` on the Anthropic provider is what makes the credential
travel as ``Authorization: Bearer`` instead of ``x-api-key``; that is the header
the Foundry data plane was verified against.

Pi's credential is a static ``apiKey`` in a config file, so
:func:`foundry.agents.start_refresher` rewrites the file every 30 minutes for as
long as the launcher lives -- a Foundry token dies after 72-90 minutes, well
inside a working session.
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

NAME = "pi"
BINARY = "pi"
DISPLAY = "Pi"

INSTALL_HINT = "install the Pi coding agent and make sure `pi` is on your PATH"

#: Providers and settings are two files in the same directory (agent reference
#: section 5), which is what ``PI_CODING_AGENT_DIR`` relocates.
PROVIDERS_NAME = "providers.json"
SETTINGS_NAME = "settings.json"

ANTHROPIC_PROVIDER = "foundry-claude"
OPENAI_PROVIDER = "foundry-openai"

#: Pi's names for the two dialects. The appended path follows from these.
ANTHROPIC_API = "anthropic-messages"
OPENAI_API = "openai-responses"


class Pi:
    """The ``foundry pi`` agent."""

    name = NAME
    binary = BINARY
    display = DISPLAY
    install_hint = INSTALL_HINT

    #: ``providers.json`` is rewritten on a timer while the child runs.
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
            f"To fix: {self.install_hint}"
        )

    # -- paths -----------------------------------------------------------

    def home(self) -> Path:
        """``~/.foundry/agents/pi`` -- what ``PI_CODING_AGENT_DIR`` points at."""
        return _profile.agent_home(self.name)

    def providers_path(self) -> Path:
        return self.home() / PROVIDERS_NAME

    def settings_path(self) -> Path:
        return self.home() / SETTINGS_NAME

    # -- model -----------------------------------------------------------

    def default_model(self, profile: Profile) -> str | None:
        return preferred_model(profile)

    def provider_for(self, profile: Profile, model: str) -> str:
        """Which of the two provider blocks serves *model*."""
        return ANTHROPIC_PROVIDER if dialect(profile, model) == "anthropic" else OPENAI_PROVIDER

    # -- configuration ---------------------------------------------------

    def providers_document(
        self, profile: Profile, model: str | None, *, token: str
    ) -> dict[str, Any]:
        """The exact JSON object written to ``providers.json``.

        Pure, so a test can assert the bytes without a filesystem or a token
        mint. Both dialects are declared when the account publishes both, so the
        user can switch models inside Pi without returning here.
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
            target.insert(0, chosen)

        providers: dict[str, Any] = {}
        if anthropic_models:
            providers[ANTHROPIC_PROVIDER] = {
                # No /v1: the anthropic-messages dialect appends /v1/messages.
                "baseUrl": endpoints.anthropic_base(endpoint),
                "api": ANTHROPIC_API,
                "apiKey": token,
                # Send `Authorization: Bearer`, the header the data plane was
                # verified against, rather than Anthropic's `x-api-key`.
                "authHeader": True,
                "models": [{"id": name} for name in anthropic_models],
            }
        if openai_models:
            providers[OPENAI_PROVIDER] = {
                # /v1 here: the openai-responses dialect appends only /responses.
                "baseUrl": endpoints.openai_base(endpoint),
                "api": OPENAI_API,
                "apiKey": token,
                "models": [{"id": name} for name in openai_models],
            }

        return {"providers": providers}

    def settings_document(self, profile: Profile, model: str | None) -> dict[str, Any]:
        """The exact JSON object written to ``settings.json``.

        Only the two keys that select the model. This file is in ``~/.foundry``,
        never the user's own Pi directory, so there is nothing else in it to
        preserve.
        """
        chosen = self._require_model(profile, model)
        return {
            "defaultProvider": self.provider_for(profile, chosen),
            "defaultModel": chosen,
        }

    def write_config(self, profile: Profile, model: str | None) -> list[Path]:
        """Mint a credential and (re)write both files. Returns their paths.

        ``providers.json`` is written last because it is the one that carries the
        credential: if the process dies between the two writes, the settings
        point at a provider that is merely stale rather than absent.
        """
        token = auth.token(subscription=profile.subscription or None)
        settings = write_json(self.settings_path(), self.settings_document(profile, model))
        providers = write_json(
            self.providers_path(), self.providers_document(profile, model, token=token)
        )
        return [providers, settings]

    def configure(self, profile: Profile, model: str | None) -> None:
        written = self.write_config(profile, model)
        _profile.record_agent(
            profile, self.name, home=self.home(), owns=[str(path) for path in written]
        )

    def refresh_credential(self, profile: Profile, model: str | None = None) -> list[Path]:
        """Rewrite the config with a freshly minted token."""
        return self.write_config(profile, model)

    # -- launch ----------------------------------------------------------

    def provider_env(self, profile: Profile, model: str | None) -> dict[str, str]:
        """Exactly the variables ``foundry`` sets."""
        del profile, model
        return {"PI_CODING_AGENT_DIR": str(self.home())}

    def launch_env(self, profile: Profile, model: str | None) -> dict[str, str]:
        """The complete environment for the child, and the refresher started.

        The config is rewritten here as well as in :meth:`configure` because a
        profile configured yesterday holds a token that died the same hour.
        """
        self.write_config(profile, model)
        start_refresher(
            f"{self.name}:{self.providers_path()}",
            lambda: self.write_config(profile, model),
        )
        env = auth.scrubbed_env()
        env.update(self.provider_env(profile, model))
        return env

    def launch_argv(self, profile: Profile, args: list[str]) -> list[str]:
        """``pi`` plus the user's arguments, untouched.

        The model is already ``settings.json``'s ``defaultModel``; Pi's own flags
        are passed through as the user typed them.
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
AGENT = Pi()
