# SPDX-License-Identifier: Apache-2.0
"""Codex, configured through a private ``CODEX_HOME``.

``CODEX_HOME`` was verified to relocate Codex's whole configuration root: with it
set, Codex reads ``$CODEX_HOME/config.toml`` and ignores ``~/.codex/config.toml``
entirely -- confirmed on a machine whose real ``~/.codex/config.toml`` fails to
parse, where Codex started normally anyway (agent reference section 2). So this
module never touches the user's file, and isolation buys immunity from a user
config that is already broken as a side effect.

Four details in the file it writes are load-bearing, and each has a failure mode
that is not obvious from the error Codex prints:

* ``wire_api`` must be ``"responses"``. ``"chat"`` is a hard deserialization
  error in current Codex, so the whole config -- not just the provider -- fails.
* the provider id must not be ``openai``, ``ollama`` or ``lmstudio``; those are
  reserved and rejected. ``foundry`` is fine.
* ``name`` must be non-empty or validation fails.
* ``base_url`` is joined as ``base.trim_end_matches('/') + "/" + path`` and Codex
  appends ``/responses``, so the base carries ``/openai/v1`` and no api-version:
  the Foundry OpenAI route is versionless.

The credential is a *command*, not a value (SPEC section 6.2): the
``[model_providers.foundry.auth]`` table points Codex back at this very
executable's hidden ``foundry auth-token``, which Codex re-runs every
``refresh_interval_ms``. A Foundry token lives 72-90 minutes, so a static bearer
would end a long session mid-flight; this way Codex renews it itself.

**The model lives in the file, not in the environment.** Codex has no documented
model environment variable, so ``foundry codex --model X`` must reach
:meth:`Codex.configure`; :meth:`Codex.launch_env` cannot express it.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import MutableMapping
from contextlib import suppress
from pathlib import Path

import tomlkit
from tomlkit.exceptions import TOMLKitError
from tomlkit.items import Table

from foundry import console, endpoints
from foundry.agents.base import AgentBase, AgentError, auth_token_argv, resource_label
from foundry.profile import Profile, backup, record_agent

#: Provider id, and therefore the ``[model_providers.<id>]`` table name.
#: ``openai``/``ollama``/``lmstudio`` are reserved by Codex and rejected.
PROVIDER_ID = "foundry"

#: Displayed by Codex when it names the provider. Must be non-empty.
PROVIDER_NAME = "Microsoft Foundry"

#: ``"chat"`` no longer deserializes at all; there is no second option.
WIRE_API = "responses"

#: How long Codex waits for ``foundry auth-token``. The vendor's example says
#: 5000, which is not enough here: the command starts a Python process which
#: starts ``az`` (itself Python) to mint the token, and a cold `az` alone costs
#: 2-4 seconds on Windows. A timeout kills the session; a generous one costs
#: nothing, because the call returns as soon as az does.
AUTH_TIMEOUT_MS = 10_000

#: 15 minutes, comfortably inside the measured 72-90 minute token lifetime.
AUTH_REFRESH_MS = 900_000

CONFIG_NAME = "config.toml"

#: Exactly what this module owns in that file, for ``foundry revert``
#: (SPEC section 6.3). Anything else in it is the user's and is preserved.
OWNED_KEYS: tuple[str, ...] = ("model", "model_provider", f"model_providers.{PROVIDER_ID}")

_HEADER = (
    "Written by foundry. Managed keys: "
    f"{', '.join(OWNED_KEYS)}.\n"
    "Anything else in this file is yours and is preserved across runs.\n"
    "Codex reads this file, and not ~/.codex/config.toml, because\n"
    "foundry sets CODEX_HOME to this directory.\n"
)

_INSTALL = "npm install -g @openai/codex"


class Codex(AgentBase):
    """Codex (`codex`), pointed at the Foundry OpenAI Responses route."""

    name = "codex"
    binary = "codex"
    display = "Codex"
    install_hint = _INSTALL

    # -- model selection ----------------------------------------------------

    def default_model(self, profile: Profile) -> str | None:
        """The newest OpenAI-family deployment, or ``None``.

        Only the OpenAI families are offered: this provider speaks the Responses
        wire API, and pointing it at an Anthropic deployment name would send a
        Responses request to a model published behind the Anthropic route.
        Discovery has already ordered the list newest-first.
        """
        published = profile.openai()
        return published[0] if published else None

    # -- configuration ------------------------------------------------------

    def configure(self, profile: Profile, model: str | None = None) -> None:
        """Write ``$CODEX_HOME/config.toml``. Idempotent; safe to re-run.

        *model* is written verbatim as the ``model`` key (SPEC section 6.4).
        Because Codex takes the model from this file, re-running configure is
        how a ``--model`` on the command line takes effect.
        """
        deployment = (model or "").strip() or self.default_model(profile)
        if not deployment:
            raise AgentError(_no_openai_message(profile))

        path = self.config_path()
        document = _load(path)
        _apply(document, profile, deployment)
        _write(path, tomlkit.dumps(document))

        record_agent(profile, self.name, home=self.home(), owns=[str(path), *OWNED_KEYS])
        entry = profile.agents.get(self.name)
        if isinstance(entry, dict):
            entry["model"] = deployment

    def config_path(self) -> Path:
        """``~/.foundry/agents/codex/config.toml`` -- the only file we write."""
        return self.home() / CONFIG_NAME

    # -- launch -------------------------------------------------------------

    def launch_env(self, profile: Profile, model: str | None = None) -> dict[str, str]:
        """The child environment: ``CODEX_HOME``, and nothing else changed.

        The credential is fetched by Codex itself through the ``[..auth]``
        command, so -- unlike Claude Code -- nothing here depends on the Azure
        SDK credential chain and there is no ambient service principal to scrub:
        ``foundry auth-token`` goes through ``az``, which those variables do not
        steer.

        *model* is accepted for interface symmetry and cannot be honoured here;
        Codex reads the model from ``config.toml``. Call :meth:`configure` with
        it first.
        """
        env = dict(os.environ)
        env["CODEX_HOME"] = str(self.home())
        return env

    def launch_argv(self, profile: Profile, args: list[str]) -> list[str]:
        return [self.executable(), *list(args)]


#: The module's agent. `cli.py` registers this object.
AGENT = Codex()
agent = AGENT


# ---------------------------------------------------------------------------
# The document
# ---------------------------------------------------------------------------


def _load(path: Path) -> tomlkit.TOMLDocument:
    """Parse an existing ``config.toml``, or start a fresh one.

    The file is inside a `foundry`-owned directory, but the user may well have
    added their own keys to it -- approval policy, sandbox, an MCP server -- and
    tomlkit round-trips those, and their comments, untouched. A file that no
    longer parses is backed up to ``~/.foundry/backups`` and replaced, rather
    than being left to break every future launch: an unparseable config.toml is
    exactly the failure that isolation was adopted to survive.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _fresh()
    except OSError as exc:
        raise AgentError(
            f"cannot read {path}: {exc.strerror or exc}.\n"
            "Fix the permissions on that file, or delete it and re-run: foundry configure"
        ) from exc

    try:
        return tomlkit.parse(raw)
    except TOMLKitError as exc:
        saved = backup(path)
        console.warn(
            f"{path} is not valid TOML ({exc}); rewriting it."
            + (f" The old file is at {saved}." if saved else "")
        )
        return _fresh()


def _fresh() -> tomlkit.TOMLDocument:
    document = tomlkit.document()
    for line in _HEADER.strip().splitlines():
        document.add(tomlkit.comment(line))
    document.add(tomlkit.nl())
    return document


def _apply(document: tomlkit.TOMLDocument, profile: Profile, deployment: str) -> None:
    """Set the owned keys on *document*, leaving everything else alone."""
    document["model"] = deployment
    document["model_provider"] = PROVIDER_ID

    providers = document.get("model_providers")
    if not isinstance(providers, MutableMapping):
        # A super table renders as `[model_providers.foundry]` with no empty
        # `[model_providers]` header of its own.
        providers = tomlkit.table(True)
        document["model_providers"] = providers
    providers[PROVIDER_ID] = _provider_table(profile)


def _provider_table(profile: Profile) -> Table:
    argv = auth_token_argv(profile)

    auth_table = tomlkit.table()
    # argv[0] is an absolute path: Codex runs this from the user's working
    # directory, long after launch, with a PATH we do not control.
    auth_table["command"] = argv[0]
    auth_table["args"] = tomlkit.item(list(argv[1:]))
    auth_table["timeout_ms"] = AUTH_TIMEOUT_MS
    auth_table["refresh_interval_ms"] = AUTH_REFRESH_MS

    provider = tomlkit.table()
    provider["name"] = PROVIDER_NAME
    provider["base_url"] = endpoints.openai_base(profile.endpoint)
    provider["wire_api"] = WIRE_API
    provider["auth"] = auth_table
    return provider


def _write(path: Path, text: str) -> None:
    """Replace *path* atomically, so a crash cannot leave half a config.

    Codex refuses to start on a config it cannot parse, and half a TOML file
    parses about as often as none of one.
    """
    if not text.endswith("\n"):
        text += "\n"

    directory = path.parent
    tmp_path: str | None = None
    try:
        fd, tmp_path = tempfile.mkstemp(dir=str(directory), prefix=".config-", suffix=".toml")
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        tmp_path = None
    except OSError as exc:
        raise AgentError(
            f"cannot write {path}: {exc.strerror or exc}.\n"
            f"Check that {directory} is writable, then re-run: foundry configure"
        ) from exc
    finally:
        if tmp_path is not None:
            with suppress(OSError):
                os.unlink(tmp_path)


def _no_openai_message(profile: Profile) -> str:
    resource = resource_label(profile)
    return (
        f"{resource} has no OpenAI-family chat model deployed, and Codex needs one.\n"
        f"Publish one in the Microsoft Foundry portal (Deployments -> Deploy model -> "
        f"gpt-5.4-mini, for instance) on {profile.endpoint}, then run: foundry configure\n"
        "Or name an existing deployment explicitly: foundry codex --model <deployment>"
    )
