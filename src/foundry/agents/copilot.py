# SPDX-License-Identifier: Apache-2.0
"""GitHub Copilot CLI, pointed at Microsoft Foundry (agent reference section 3).

Copilot is the odd one out in three ways, and each one shapes this module.

**Its provider configuration is environment-only.** There is no ``settings.json``
key for the base URL, the provider type or the credential, so ``COPILOT_HOME``
isolates everything *else* Copilot keeps (sessions, logs, MCP config) while the
Foundry wiring travels in the child's environment.

**Its base URL is the bare resource root.** With provider type ``azure`` and
``COPILOT_PROVIDER_AZURE_API_VERSION`` unset, Copilot itself appends
``/openai/v1/chat/completions``, so the configured value is ``E`` -- the one
place in this project where the ``/openai/v1`` segment is *not* part of the base
URL. Setting either ``COPILOT_PROVIDER_AZURE_API_VERSION`` or
``COPILOT_PROVIDER_WIRE_API`` moves that append, which is why this module
removes both from the child environment rather than merely not setting them.
For the same reason no ``--model`` flag is ever injected into the argv:
Copilot's flag validates against its own catalogue and would reject a Foundry
deployment name outright.

**Its credential cannot be refreshed in flight.** A child process's environment
is fixed the moment it starts, and no timer can reach into it; the refresher the
other static-credential agents use would be theatre here. The answer is to pick
a credential that does not expire. Provider type ``azure`` authenticates with
the *resource key*, which has no lifetime, so a Copilot session lasts as long as
the user wants it to. Only when no key can be obtained does this module fall
back to an Entra bearer -- and then it says so, because "this session dies in an
hour" is something to know before starting work rather than after.
"""

from __future__ import annotations

import os

from foundry import auth, console, endpoints
from foundry.agents import dialect, preferred_model
from foundry.agents.base import AgentBase, AgentError, resource_label
from foundry.profile import Profile, record_agent

_INSTALL = "npm install -g @github/copilot"

#: Everything this module puts into the child environment. Recorded on the
#: profile so ``foundry status`` can list it and ``foundry revert`` knows the
#: blast radius was one directory and a process environment -- nothing of the
#: user's.
OWNED_ENV: tuple[str, ...] = (
    "COPILOT_HOME",
    "COPILOT_PROVIDER_TYPE",
    "COPILOT_PROVIDER_BASE_URL",
    "COPILOT_PROVIDER_API_KEY",
    "COPILOT_PROVIDER_BEARER_TOKEN",
    "COPILOT_PROVIDER_MODEL_ID",
    "COPILOT_PROVIDER_WIRE_MODEL",
)

#: Variables that would move the request path or the model identity out from
#: under us if the user happens to have them set. Removed, not overwritten:
#: "unset" is a meaningful state for the first two.
CONFLICTING_ENV: tuple[str, ...] = (
    "COPILOT_PROVIDER_AZURE_API_VERSION",  # switches to the classic deployment route
    "COPILOT_PROVIDER_WIRE_API",  # switches the append to /responses
    "COPILOT_MODEL",  # sets MODEL_ID and WIRE_MODEL at once, defeating the split
)

#: Model ids Copilot ships token limits and agent configuration for, as listed
#: by ``copilot --help`` on the installed CLI. ``COPILOT_PROVIDER_MODEL_ID`` is
#: matched against this set; ``COPILOT_PROVIDER_WIRE_MODEL`` never is.
CATALOGUE_MODEL_IDS: frozenset[str] = frozenset(
    {
        "claude-sonnet-4.5",
        "claude-haiku-4.5",
        "claude-opus-4.5",
        "claude-sonnet-4",
        "gpt-5",
        "gpt-5.1",
        "gpt-5.1-codex-mini",
        "gpt-5.1-codex",
        "gpt-5-mini",
        "gpt-4.1",
        "gemini-3-pro-preview",
    }
)

#: Catalogue id per Anthropic tier, and the order the tiers are tried in.
_ANTHROPIC_IDS: tuple[tuple[str, str], ...] = (
    ("opus", "claude-opus-4.5"),
    ("sonnet", "claude-sonnet-4.5"),
    ("haiku", "claude-haiku-4.5"),
)

#: Limits applied only when a deployment maps to no catalogue id at all, where
#: Copilot has nothing to size a context window from. Deliberately below every
#: chat model Foundry publishes: being under the real limit costs some context,
#: being over it costs a hard 400 on the first long prompt.
FALLBACK_MAX_PROMPT_TOKENS = "128000"
FALLBACK_MAX_OUTPUT_TOKENS = "16384"

_TOKEN_LIMIT_ENV = ("COPILOT_PROVIDER_MAX_PROMPT_TOKENS", "COPILOT_PROVIDER_MAX_OUTPUT_TOKENS")


class Copilot(AgentBase):
    """The ``foundry copilot`` agent."""

    name = "copilot"
    binary = "copilot"
    display = "GitHub Copilot CLI"
    install_hint = _INSTALL

    #: No timer can rewrite a running child's environment. See the module
    #: docstring for what is done instead.
    refreshes_in_flight = False

    # -- model --------------------------------------------------------------

    def default_model(self, profile: Profile) -> str | None:
        """The deployment to launch with when the user names none.

        OpenAI-family deployments lead: this provider speaks the OpenAI
        chat-completions wire protocol, which is the route Foundry serves OpenAI
        models on. An Anthropic-format deployment is offered only when the
        account publishes nothing else, and :meth:`configure` says so out loud.
        """
        return preferred_model(profile, prefer="openai")

    def model_id(self, profile: Profile, model: str) -> str:
        """The well-known id that drives Copilot's token limits and agent config.

        This is the entire point of the ``MODEL_ID`` / ``WIRE_MODEL`` split: the
        deployment name goes on the wire untouched (SPEC section 6.4) while
        Copilot is given an id it recognises. When nothing in its catalogue
        matches, the deployment name is used for both and :meth:`provider_env`
        supplies explicit limits instead.
        """
        exact = _catalogue_match(model)
        if exact is not None:
            return exact
        if dialect(profile, model) == "anthropic":
            return _anthropic_catalogue_id(profile, model)
        return _openai_catalogue_id(model) or model

    # -- configuration ------------------------------------------------------

    def configure(self, profile: Profile, model: str | None = None) -> None:
        """Claim ``~/.foundry/agents/copilot`` and prove the credential resolves.

        Nothing is written beyond the directory: provider configuration is
        environment-only. The value of doing the work here is that it fails on a
        line the user can read, rather than inside a TUI that has already taken
        the screen.
        """
        chosen = self._require_model(profile, model)
        home = self.home()
        kind, _value = self.credential(profile)

        if kind == "bearer":
            console.warn(
                "Copilot cannot refresh a credential once it has started, and no resource "
                "key was available -- this session uses an Entra token that expires in "
                "72-90 minutes."
            )
            console.note(
                "For a credential that does not expire, get 'Cognitive Services "
                "Contributor' on the resource (or set FOUNDRY_API_KEY), then re-run "
                "`foundry copilot`."
            )
        if dialect(profile, chosen) == "anthropic":
            console.warn(
                f"{chosen} is an Anthropic-format deployment, and Copilot speaks the OpenAI "
                "chat-completions protocol; the endpoint may refuse it."
            )
            console.note(
                f"Publish an OpenAI-family model on {resource_label(profile)}, or use "
                "`foundry claude` instead."
            )

        record_agent(profile, self.name, home=home, owns=[str(home), *OWNED_ENV])
        entry = profile.agents.get(self.name)
        if isinstance(entry, dict):
            # What a launch will actually send, so `foundry status` need not
            # re-derive it -- and so the MODEL_ID/WIRE_MODEL split is visible.
            entry["model"] = chosen
            entry["model_id"] = self.model_id(profile, chosen)
            entry["credential"] = kind

    def credential(self, profile: Profile) -> tuple[str, str]:
        """``("key", <resource key>)`` or ``("bearer", <entra token>)``.

        The key is preferred because it does not expire and Copilot cannot renew
        anything. ``FOUNDRY_BEARER`` overrides that: a user who pre-minted a
        token has already said which credential to use.
        """
        subscription = profile.subscription or None
        if (os.environ.get(auth.BEARER_ENV) or "").strip():
            return "bearer", auth.token(subscription=subscription)
        key = auth.api_key(profile.endpoint, subscription)
        if key:
            return "key", key
        return "bearer", auth.token(subscription=subscription)

    def refresh_credential(self, profile: Profile, model: str | None = None) -> str:
        """Re-resolve the credential a launch would use, right now.

        Nothing schedules this -- it cannot reach a running child's environment
        -- but it is the honest answer to "what credential is in play", which
        ``foundry status`` wants.
        """
        del model
        return self.credential(profile)[1]

    # -- launch -------------------------------------------------------------

    def provider_env(self, profile: Profile, model: str | None = None) -> dict[str, str]:
        """Exactly the variables ``foundry`` sets, with nothing inherited.

        Split out from :meth:`launch_env` so a test, a ``--dry-run`` and
        ``foundry status`` can all see the wiring without a copy of the whole
        environment around it.
        """
        chosen = self._require_model(profile, model)
        kind, value = self.credential(profile)
        identity = self.model_id(profile, chosen)

        env: dict[str, str] = {
            "COPILOT_HOME": str(self.home()),
            "COPILOT_PROVIDER_MODEL_ID": identity,
            # Verbatim, always: the deployment name is chosen by whoever
            # published it and is the only string the service answers to.
            "COPILOT_PROVIDER_WIRE_MODEL": chosen,
        }

        if kind == "key":
            # Type `azure` sends `api-key: <value>` and, with no api-version
            # set, posts to <base>/openai/v1/chat/completions -- so the base URL
            # is the bare resource root.
            env["COPILOT_PROVIDER_TYPE"] = "azure"
            env["COPILOT_PROVIDER_BASE_URL"] = endpoints.normalize(profile.endpoint)
            env["COPILOT_PROVIDER_API_KEY"] = value
        else:
            # Type `openai` sends `Authorization: Bearer` and posts to
            # <base>/chat/completions, so here the base URL does carry /openai/v1.
            env["COPILOT_PROVIDER_TYPE"] = "openai"
            env["COPILOT_PROVIDER_BASE_URL"] = endpoints.openai_base(profile.endpoint)
            env["COPILOT_PROVIDER_BEARER_TOKEN"] = value

        if identity not in CATALOGUE_MODEL_IDS:
            # Copilot has no catalogue entry to size this model from. Give it
            # limits it can work with, unless the user has stated better ones.
            for name, fallback in zip(
                _TOKEN_LIMIT_ENV,
                (FALLBACK_MAX_PROMPT_TOKENS, FALLBACK_MAX_OUTPUT_TOKENS),
                strict=True,
            ):
                if not (os.environ.get(name) or "").strip():
                    env[name] = fallback

        return env

    def launch_env(self, profile: Profile, model: str | None = None) -> dict[str, str]:
        """The complete environment for the child process."""
        env = auth.scrubbed_env()
        for name in CONFLICTING_ENV:
            env.pop(name, None)

        provider = self.provider_env(profile, model)
        # The credential we are *not* using must not survive from the user's
        # shell: BEARER_TOKEN outranks API_KEY, so a stale one would win.
        for name in ("COPILOT_PROVIDER_API_KEY", "COPILOT_PROVIDER_BEARER_TOKEN"):
            if name not in provider:
                env.pop(name, None)
        env.update(provider)
        return env

    # -- internals ----------------------------------------------------------

    def _require_model(self, profile: Profile, model: str | None) -> str:
        chosen = (model or "").strip() or self.default_model(profile)
        if not chosen:
            raise AgentError(
                f"{resource_label(profile)} publishes no chat model for Copilot.\n"
                "Publish one in the Foundry portal -- an OpenAI-family model, since Copilot "
                "speaks the OpenAI protocol -- then run `foundry configure`."
            )
        return chosen


def _catalogue_match(model: str) -> str | None:
    """A catalogue id equal to the deployment name, dashes-for-dots included.

    Deployment names copy the model's own name often enough for this to hit: an
    account that published ``gpt-4.1`` or ``claude-opus-4-5`` has named itself
    out of the guessing below entirely.
    """
    lowered = (model or "").strip().lower()
    if lowered in CATALOGUE_MODEL_IDS:
        return lowered
    for known in CATALOGUE_MODEL_IDS:
        if known.replace(".", "-") == lowered:
            return known
    return None


def _anthropic_catalogue_id(profile: Profile, model: str) -> str:
    """The catalogue id for the Anthropic tier this deployment belongs to.

    The profile is asked first because its tiers came from ARM metadata, which
    is authoritative; the name is inspected only for a ``--model`` the last scan
    never saw.
    """
    for tier, identity in _ANTHROPIC_IDS:
        if profile.anthropic(tier) == model:
            return identity
    lowered = model.lower()
    for tier, identity in _ANTHROPIC_IDS:
        if tier in lowered:
            return identity
    return "claude-sonnet-4.5"


def _openai_catalogue_id(model: str) -> str | None:
    """The nearest catalogue id for an OpenAI-family deployment, or ``None``.

    Nearest by family and size rather than by version: the id only has to give
    Copilot a plausible context window and tool-calling profile, since the wire
    model is carried separately and exactly.
    """
    lowered = (model or "").strip().lower()
    if not lowered:
        return None
    small = "mini" in lowered or "nano" in lowered
    if "codex" in lowered:
        return "gpt-5.1-codex-mini" if small else "gpt-5.1-codex"
    if small:
        return "gpt-5-mini"
    if "gpt-5" in lowered:
        return "gpt-5.1"
    if "gpt-4" in lowered:
        return "gpt-4.1"
    return None


#: The module's agent. The registry loads this object.
AGENT = Copilot()
agent = AGENT
