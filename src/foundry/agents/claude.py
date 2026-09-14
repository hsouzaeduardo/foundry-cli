# SPDX-License-Identifier: Apache-2.0
"""Claude Code, through its native Microsoft Foundry mode.

Claude Code supports Foundry as a first-class provider (agent reference
section 1), so this module configures *no* base URL, writes *no* settings file
and installs *no* credential helper. It sets six environment variables and gets
out of the way. Three of them are the reason `foundry` exists at all.

*The credential must not be a value.* Of the three authentication options the
vendor offers, only the third -- neither ``ANTHROPIC_FOUNDRY_AUTH_TOKEN`` nor
``ANTHROPIC_FOUNDRY_API_KEY`` set, so the Azure SDK's ``DefaultAzureCredential``
chain runs -- refreshes itself. A minted token lives 72-90 minutes and Claude
Code will not renew one, so a baked-in bearer turns a long session into a
timebomb. This module therefore hands over no credential whatsoever.

*That chain has a trap, and it was hit in practice.*
``DefaultAzureCredential`` tries ``EnvironmentCredential`` first and, when
``AZURE_CLIENT_ID`` / ``AZURE_CLIENT_SECRET`` / ``AZURE_TENANT_ID`` are lying
around from some unrelated service principal, uses them and **fails the whole
chain** rather than falling through (``AADSTS7000222``: expired client secret).
:func:`foundry.auth.scrubbed_env` removes them; without it the launch dies with
an error that names Entra, not the environment.

*The three model aliases must be pinned.* Left unset, ``opus``/``sonnet``/
``haiku`` resolve to Claude Code's built-in Foundry defaults, which lag current
releases and need not exist in this account -- and Foundry does no startup model
check, so the first request just fails. Discovering the deployment names and
pinning them is the substance of what this module contributes.
"""

from __future__ import annotations

import os

from foundry import auth, console, endpoints
from foundry.agents.base import AgentBase, AgentError, resource_label
from foundry.profile import Profile, record_agent

#: Anthropic families, strongest first.
FAMILIES: tuple[str, ...] = ("opus", "sonnet", "haiku")

#: The alias each family pins. All three are always set -- see the module
#: docstring; an unpinned alias is a request that fails at the first token.
MODEL_ENV: dict[str, str] = {
    "opus": "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "sonnet": "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "haiku": "ANTHROPIC_DEFAULT_HAIKU_MODEL",
}

#: Which deployment stands in for a family this resource does not publish,
#: in order of preference. A substitution is announced, never silent
#: (:meth:`ClaudeCode.configure`), but it beats the alternative: leaving the
#: alias unpinned means Claude Code asks Foundry for a model that is not there.
#: Both substitutions for a missing ``haiku`` are more expensive than a real
#: haiku, which is worth saying out loud, and cheaper than a failed session.
_SUBSTITUTES: dict[str, tuple[str, ...]] = {
    "opus": ("opus", "sonnet", "haiku"),
    "sonnet": ("sonnet", "opus", "haiku"),
    "haiku": ("haiku", "sonnet", "opus"),
}

#: Inherited variables that select a *different* provider or endpoint. Foundry
#: mode is explicit here, so leaving any of these in the child environment turns
#: a working launch into an argument between two configurations.
_CONFLICTING: tuple[str, ...] = (
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_FOUNDRY_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
)

#: Vendor precedence order: a bearer beats an API key beats the credential
#: chain. Both are cleared unless `foundry`'s own escape hatches ask for them.
_TOKEN_ENV = "ANTHROPIC_FOUNDRY_AUTH_TOKEN"
_KEY_ENV = "ANTHROPIC_FOUNDRY_API_KEY"

_INSTALL = "npm install -g @anthropic-ai/claude-code"


class ClaudeCode(AgentBase):
    """Claude Code (`claude`), pointed at Foundry natively."""

    name = "claude"
    binary = "claude"
    display = "Claude Code"
    install_hint = _INSTALL

    # -- model selection ----------------------------------------------------

    def default_model(self, profile: Profile) -> str | None:
        """The deployment behind Claude Code's own default alias.

        Sonnet leads because that is what Claude Code selects when nobody says
        otherwise; returning opus here would silently promote every session to
        the most expensive model on the resource.
        """
        for fam in ("sonnet", "opus", "haiku"):
            deployment = profile.anthropic(fam)
            if deployment:
                return deployment
        return None

    # -- configuration ------------------------------------------------------

    def configure(self, profile: Profile, model: str | None = None) -> None:
        """Claim ``~/.foundry/agents/claude`` and record what was pinned.

        There is nothing to write. Native Foundry mode is entirely environment
        driven, and a settings file would only add a second place for the
        endpoint to be wrong. What this call does is prove that the pins can be
        built at all -- failing here, with a message about publishing a model,
        rather than inside Claude Code with a bare HTTP error.
        """
        pins, substituted = model_pins(profile)
        home = self.home()

        for family, deployment in substituted:
            console.warn(
                f"{resource_label(profile)} publishes no {family}-class Anthropic model; "
                f"pinning {MODEL_ENV[family]} to {deployment}."
            )

        record_agent(
            profile,
            self.name,
            home=home,
            # Nothing on disk but the directory: the configuration is the
            # environment, which dies with the process (SPEC section 6.3).
            owns=[str(home)],
        )
        # Recorded so `foundry status` can report what a launch will actually
        # ask for, without re-deriving it. The model itself is applied per
        # launch through ANTHROPIC_MODEL, not here.
        entry = profile.agents.get(self.name)
        if isinstance(entry, dict):
            entry["pins"] = dict(pins)
            if model:
                entry["model"] = model

    # -- launch -------------------------------------------------------------

    def launch_env(self, profile: Profile, model: str | None = None) -> dict[str, str]:
        """The child environment: isolation, Foundry mode, and the three pins.

        May print a warning to stderr when the inherited environment carries a
        credential that outranks the self-refreshing one.
        """
        pins, _ = model_pins(profile)

        # scrubbed_env() is load-bearing, not hygiene: see the module docstring.
        env = auth.scrubbed_env()
        for name in _CONFLICTING:
            env.pop(name, None)

        env["CLAUDE_CONFIG_DIR"] = str(self.home())
        env["CLAUDE_CODE_USE_FOUNDRY"] = "1"
        env["ANTHROPIC_FOUNDRY_RESOURCE"] = endpoints.resource_name(profile.endpoint)
        env.update(pins)

        if model:
            # Verbatim (SPEC section 6.4). Claude Code's own --model flag, which
            # a passthrough argument may also carry, outranks this.
            env["ANTHROPIC_MODEL"] = model
        else:
            env.pop("ANTHROPIC_MODEL", None)

        _apply_credential(env)
        return env

    def launch_argv(self, profile: Profile, args: list[str]) -> list[str]:
        return [self.executable(), *list(args)]


#: The module's agent. `cli.py` registers this object.
AGENT = ClaudeCode()
agent = AGENT


# ---------------------------------------------------------------------------
# Pins
# ---------------------------------------------------------------------------


def model_pins(profile: Profile) -> tuple[dict[str, str], list[tuple[str, str]]]:
    """Return ``({env var: deployment}, [(family, substitute deployment), ...])``.

    All three aliases are always pinned. Raises :class:`AgentError` when the
    resource publishes no Anthropic deployment at all, because then there is
    nothing to pin them to and Claude Code cannot work against this endpoint.
    """
    published = {family: profile.anthropic(family) for family in FAMILIES}
    if not any(published.values()):
        raise AgentError(_no_anthropic_message(profile))

    pins: dict[str, str] = {}
    substituted: list[tuple[str, str]] = []
    for family in FAMILIES:
        deployment = published.get(family)
        if not deployment:
            deployment = next(
                published[alternative]
                for alternative in _SUBSTITUTES[family]
                if published.get(alternative)
            )
            substituted.append((family, deployment))
        pins[MODEL_ENV[family]] = deployment
    return pins, substituted


def _no_anthropic_message(profile: Profile) -> str:
    resource = resource_label(profile)
    return (
        f"{resource} has no Anthropic model deployed, and Claude Code needs one.\n"
        f"Publish a Claude deployment in the Microsoft Foundry portal "
        f"(Deployments -> Deploy model -> claude-sonnet / claude-opus) on {profile.endpoint}, "
        f"then run: foundry configure"
    )


# ---------------------------------------------------------------------------
# Credential
# ---------------------------------------------------------------------------


def _apply_credential(env: dict[str, str]) -> None:
    """Leave the credential to ``DefaultAzureCredential`` unless told otherwise.

    ``FOUNDRY_API_KEY`` and ``FOUNDRY_BEARER`` are the headless escape hatches of
    SPEC section 3: on a machine where ``az login`` is impractical the credential
    chain has nothing to find, so the user's own value is passed through in the
    vendor's precedence order. Otherwise both variables are removed, including
    when they were inherited -- a stale bearer in the environment outranks the
    chain and would kill the session at the first expiry.
    """
    api_key = (os.environ.get(auth.API_KEY_ENV) or "").strip()
    bearer = (os.environ.get(auth.BEARER_ENV) or "").strip()

    env.pop(_TOKEN_ENV, None)
    env.pop(_KEY_ENV, None)

    if api_key:
        env[_KEY_ENV] = api_key
        return
    if bearer:
        env[_TOKEN_ENV] = bearer
        console.warn(
            f"Using the token in {auth.BEARER_ENV} for Claude Code. It is not refreshed, so "
            "this session ends when the token expires (72-90 minutes). Sign in with "
            "`az login` instead to have the credential renewed automatically."
        )
