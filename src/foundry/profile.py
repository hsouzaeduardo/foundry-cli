# SPDX-License-Identifier: Apache-2.0
"""On-disk state: a single ``~/.foundry/config.json`` (SPEC section 8).

Three rules drive everything in this module.

*One file.* Every persisted fact lives in one JSON object so that "what does
``foundry`` think it is talking to" is answerable with ``cat``, and so that
``revert`` is a delete rather than a diff.

*No migration.* An unrecognised ``version`` is discarded and rebuilt. A launcher
is cheap to reconfigure -- a scan of the subscriptions costs seconds -- so
carrying migration code for the lifetime of the project buys nothing, and it is
the kind of code that rots untested.

*No secrets.* Tokens live 72-90 minutes and are re-minted from ``az`` on demand;
writing one to disk would create a credential outliving the session that minted
it. :func:`save` scrubs credential-shaped keys defensively, because the
free-form ``agents`` sub-dicts are filled in by five different agent modules and
an invariant that is only documented is not an invariant.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

#: Bumped only when the on-disk shape changes incompatibly. Anything else found
#: on disk is thrown away, never migrated -- see the module docstring.
VERSION = 1

#: Overrides ``~/.foundry``. Exists so tests never touch a developer's real home
#: directory, and so a CI runner can point the whole tree at a scratch dir.
HOME_ENV = "FOUNDRY_HOME"

CONFIG_NAME = "config.json"

#: Keys that must never reach disk, matched case-insensitively against every
#: mapping key in the payload. Values are never inspected: a deployment name or
#: a filesystem path is data, and guessing that data is a secret would silently
#: corrupt the profile.
_SECRET_EXACT = frozenset(
    {
        "apikey",
        "api_key",
        "authorization",
        "bearer",
        "credential",
        "credentials",
        "key",
    }
)
_SECRET_SUBSTRINGS = ("token", "secret", "password", "passwd")

#: An agent identifier becomes a directory name under ``~/.foundry/agents``, so
#: it is restricted rather than escaped -- a name that needs escaping is a bug
#: in the caller, not a path to sanitise.
_TOOL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class ProfileError(RuntimeError):
    """A state-file problem the user can fix. The message names the fix."""


@dataclass
class Profile:
    """The persisted view of one Foundry resource and what has been configured.

    ``deployments`` and ``agents`` are deliberately loose dicts: the agent
    modules own their own bookkeeping, and this module refuses to become a
    schema authority for five independently-evolving CLIs.

    ``deployments`` is ``{"anthropic": {"opus": name, "sonnet": name,
    "haiku": name}, "openai": [name, ...]}``.

    ``agents`` is ``{tool: {"configured_at": iso8601, "home": abs_path,
    "owns": [...]}}`` -- see :func:`record_agent`.
    """

    endpoint: str
    subscription: str
    resource_group: str
    account: str
    deployments: dict[str, Any] = field(default_factory=dict)
    agents: dict[str, Any] = field(default_factory=dict)

    # -- convenience readers ------------------------------------------------
    # The deployments shape is nested enough that every caller would otherwise
    # re-implement the same ``.get()`` chains, each with its own bug.

    def anthropic(self, family: str) -> str | None:
        """Deployment name published for ``opus`` / ``sonnet`` / ``haiku``."""
        block = self.deployments.get("anthropic")
        if not isinstance(block, dict):
            return None
        value = block.get(family)
        return value if isinstance(value, str) and value else None

    def openai(self) -> list[str]:
        """Deployment names of the OpenAI-family chat models, newest first."""
        block = self.deployments.get("openai")
        if not isinstance(block, list):
            return []
        return [v for v in block if isinstance(v, str) and v]

    def all_deployments(self) -> list[str]:
        """Every usable deployment name, Anthropic first, without duplicates."""
        names: list[str] = []
        for fam in ("opus", "sonnet", "haiku"):
            name = self.anthropic(fam)
            if name and name not in names:
                names.append(name)
        for name in self.openai():
            if name not in names:
                names.append(name)
        return names

    def is_configured(self, tool: str) -> bool:
        """True once ``tool`` has been configured against this endpoint."""
        return isinstance(self.agents.get(tool), dict)

    def to_dict(self) -> dict[str, Any]:
        """The exact JSON object written to disk, ``version`` first."""
        return {
            "version": VERSION,
            "endpoint": self.endpoint,
            "subscription": self.subscription,
            "resource_group": self.resource_group,
            "account": self.account,
            "deployments": self.deployments,
            "agents": self.agents,
        }


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def app_dir() -> Path:
    """``~/.foundry`` (or ``$FOUNDRY_HOME``), absolute, not created.

    Pure on purpose: ``foundry --version`` must not create directories.
    """
    override = os.environ.get(HOME_ENV)
    base = Path(override).expanduser() if override else Path.home() / ".foundry"
    return Path(os.path.abspath(base))


def config_path() -> Path:
    """Absolute path of the one state file."""
    return app_dir() / CONFIG_NAME


def backups_dir() -> Path:
    """``~/.foundry/backups`` (SPEC section 8), created on demand.

    Only the shared-file fallback in SPEC section 6 writes here; with the
    isolation variables from the agent reference it stays empty.
    """
    return _ensure_dir(app_dir() / "backups")


def agent_home(tool: str) -> Path:
    """``~/.foundry/agents/<tool>``, created on demand, absolute.

    This is what each agent's isolation variable (``CODEX_HOME``,
    ``CLAUDE_CONFIG_DIR``, ...) is pointed at, and those are read by a child
    process whose working directory we do not control -- hence absolute, always.
    """
    if not isinstance(tool, str) or not _TOOL_RE.match(tool):
        raise ValueError(
            f"invalid agent name {tool!r}: use letters, digits, dot, dash or "
            "underscore -- it becomes a directory name under ~/.foundry/agents"
        )
    return _ensure_dir(app_dir() / "agents" / tool)


# ---------------------------------------------------------------------------
# Load / save / clear
# ---------------------------------------------------------------------------


def load() -> Profile | None:
    """Read the state file, or ``None`` if there is nothing usable.

    ``None`` covers all of: no file, unreadable file, malformed JSON, an
    unrecognised ``version``, and a record with no endpoint. Every one of those
    has the same remedy -- run ``foundry configure`` -- so they are not
    distinguished here. The stale bytes are left in place; the next :func:`save`
    replaces them.
    """
    path = config_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None

    # `version` must be the integer 1, not merely something equal to it: in Python
    # `True == 1` and `1.0 == 1`, so a bare `!=` would accept `{"version": true}`
    # and `{"version": 1.0}` as a valid v1 profile. A file whose version field is
    # not an int was not written by us, and is discarded like any other unknown
    # version rather than half-trusted.
    version = data.get("version") if isinstance(data, dict) else None
    if type(version) is not int or version != VERSION:
        return None

    endpoint = data.get("endpoint")
    if not isinstance(endpoint, str) or not endpoint:
        return None

    deployments = data.get("deployments")
    agents = data.get("agents")
    return Profile(
        endpoint=endpoint,
        subscription=_as_str(data.get("subscription")),
        resource_group=_as_str(data.get("resource_group")),
        account=_as_str(data.get("account")),
        deployments=deployments if isinstance(deployments, dict) else {},
        agents=agents if isinstance(agents, dict) else {},
    )


def save(p: Profile) -> None:
    """Write the state file atomically.

    Temp file in the same directory, then :func:`os.replace`. An interrupted run
    -- Ctrl-C during a configure, a crash between two agents -- can therefore
    leave the old config or the new one but never half of either. A truncated
    ``config.json`` is indistinguishable from a corrupt one and would silently
    cost the user their endpoint.
    """
    directory = _ensure_dir(app_dir())
    target = directory / CONFIG_NAME
    body = json.dumps(_scrubbed(p.to_dict()), indent=2, ensure_ascii=False) + "\n"

    tmp_path: str | None = None
    try:
        fd, tmp_path = tempfile.mkstemp(dir=str(directory), prefix=".config-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(body)
            fh.flush()
            os.fsync(fh.fileno())
        with suppress(OSError):  # best effort; a no-op on Windows
            os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, target)
        tmp_path = None
    except OSError as exc:
        raise ProfileError(
            f"cannot write {target}: {exc.strerror or exc}. "
            f"Check that {directory} is writable, or set {HOME_ENV} to a directory that is."
        ) from exc
    finally:
        if tmp_path is not None:
            with suppress(OSError):
                os.unlink(tmp_path)


def clear(*, keep_backups: bool = True) -> None:
    """Delete foundry-owned state: the config file and every agent home.

    ``keep_backups`` defaults to True because ``~/.foundry/backups`` holds the
    only copy of any user file that was edited in place (SPEC section 6), and
    deleting it during a revert would destroy the material the revert restores
    from. Pass ``keep_backups=False`` for a full teardown once nothing needs
    restoring.

    Missing paths are not an error: ``clear()`` states an outcome, not a
    transition.
    """
    base = app_dir()
    with suppress(OSError):
        (base / CONFIG_NAME).unlink()
    _rmtree(base / "agents")
    if not keep_backups:
        _rmtree(base / "backups")
        # Only now can the root plausibly be empty. Leave it if it is not:
        # anything still in there was not put there by us.
        with suppress(OSError):
            base.rmdir()


# ---------------------------------------------------------------------------
# Bookkeeping helpers
# ---------------------------------------------------------------------------


def record_agent(
    p: Profile,
    tool: str,
    *,
    home: Path | str | None = None,
    owns: list[str] | None = None,
) -> None:
    """Stamp ``tool`` as configured. Mutates ``p``; the caller still saves.

    Records ``configured_at`` (UTC, ISO 8601), ``home`` (the isolation
    directory, absolute) and ``owns`` (the files or keys written) so that
    ``foundry revert`` removes exactly what was written and nothing else.
    """
    entry: dict[str, Any] = {"configured_at": now_iso()}
    if home is not None:
        entry["home"] = str(Path(os.path.abspath(Path(home).expanduser())))
    if owns is not None:
        entry["owns"] = list(owns)
    p.agents[tool] = entry


def forget_agent(p: Profile, tool: str) -> None:
    """Drop ``tool``'s record after a revert. Mutates ``p``; the caller saves."""
    p.agents.pop(tool, None)


def backup(path: Path | str) -> Path | None:
    """Copy a user-owned file into ``~/.foundry/backups`` before editing it.

    Returns the backup's path, or ``None`` when the source does not exist. The
    name carries a UTC timestamp so a second run never clobbers the first --
    and therefore original -- copy.
    """
    source = Path(path).expanduser()
    if not source.is_file():
        return None
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    destination = backups_dir() / f"{source.name}.{stamp}.bak"
    try:
        shutil.copy2(source, destination)
    except OSError as exc:
        raise ProfileError(
            f"cannot back up {source} to {destination}: {exc.strerror or exc}. "
            "Fix the permissions on ~/.foundry/backups and try again."
        ) from exc
    return destination


def now_iso() -> str:
    """UTC timestamp, second resolution, ``Z``-suffixed."""
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _as_str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _ensure_dir(path: Path) -> Path:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ProfileError(
            f"cannot create {path}: {exc.strerror or exc}. "
            f"Create it by hand, or set {HOME_ENV} to a writable directory."
        ) from exc
    with suppress(OSError):  # best effort; a no-op on Windows
        os.chmod(path, 0o700)
    return path


def _rmtree(path: Path) -> None:
    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise ProfileError(
            f"cannot remove {path}: {exc.strerror or exc}. "
            "Close anything still running from that directory and try again, "
            "or delete it by hand."
        ) from exc


def _is_secret_key(key: str) -> bool:
    lowered = key.lower()
    if lowered in _SECRET_EXACT:
        return True
    return any(marker in lowered for marker in _SECRET_SUBSTRINGS)


def _scrubbed(value: Any) -> Any:
    """Deep copy with credential-shaped mapping keys removed.

    Defence in depth for "tokens are never persisted": the ``agents`` sub-dicts
    are written by other modules, and a bearer token dropped in one would
    otherwise outlive the process that minted it.
    """
    if isinstance(value, dict):
        return {
            k: _scrubbed(v)
            for k, v in value.items()
            if not (isinstance(k, str) and _is_secret_key(k))
        }
    if isinstance(value, (list, tuple)):
        return [_scrubbed(v) for v in value]
    return value
