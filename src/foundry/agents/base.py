# SPDX-License-Identifier: Apache-2.0
"""The contract every agent module satisfies, and the little they share.

`foundry` adds an agent by adding one module (SPEC section 10), so the surface an
agent module has to implement is deliberately small: five members that answer
"is it here", "what model", "write the config", "what environment", "what argv".

Two decisions are encoded here rather than repeated in five modules.

*Configuration is a directory, not an edit.* Every supported agent has an
isolation variable (``CLAUDE_CONFIG_DIR``, ``CODEX_HOME``, ... -- agent reference
section 6), so each agent gets ``~/.foundry/agents/<name>`` and the user's own
config is never read, never written and never at risk. :meth:`AgentBase.revert`
is then a directory delete rather than a diff.

*The credential is a command, not a value* wherever the agent supports one
(SPEC section 6.2). :func:`auth_token_argv` builds the argv an agent re-runs to
refresh its own bearer -- ``foundry auth-token`` -- and it has to survive being
run by a child process from an arbitrary working directory, hence the care taken
in :func:`foundry_command` to produce an absolute, self-sufficient command line.
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Protocol, runtime_checkable

from foundry import endpoints

# The shared error type lives in the package, not here: `cli.py` catches one
# class for every agent, and a second, structurally identical one defined in
# this module would be caught by nobody. Re-exported so that
# ``from foundry.agents.base import AgentError`` keeps working.
from foundry.agents import AgentError
from foundry.profile import Profile, app_dir, forget_agent

__all__ = [
    "Agent",
    "AgentBase",
    "AgentError",
    "auth_token_argv",
    "foundry_command",
    "missing_binary",
    "resource_label",
    "which",
]


@runtime_checkable
class Agent(Protocol):
    """What ``cli.py`` may assume about an agent.

    Structural, not inherited: :class:`AgentBase` is a convenience, and a module
    that satisfies these five members without it is equally valid.
    """

    #: Registry key, and the directory name under ``~/.foundry/agents``.
    name: str
    #: Executable to look for on PATH and to exec.
    binary: str
    #: Human-facing name, used in messages ("Claude Code").
    display: str

    def is_installed(self) -> bool:
        """True when :attr:`binary` is on PATH."""
        ...

    def default_model(self, profile: Profile) -> str | None:
        """Deployment this agent runs when the user names none, or ``None``."""
        ...

    def configure(self, profile: Profile, model: str | None) -> None:
        """Write this agent's foundry-owned configuration. Idempotent.

        Mutates *profile* with the ownership record; the caller saves it.
        """
        ...

    def launch_env(self, profile: Profile, model: str | None) -> dict[str, str]:
        """The complete environment for the child process."""
        ...

    def launch_argv(self, profile: Profile, args: list[str]) -> list[str]:
        """The argv to exec. *args* passes through untouched (SPEC section 7)."""
        ...


# ---------------------------------------------------------------------------
# Binaries
# ---------------------------------------------------------------------------


def which(binary: str) -> str | None:
    """Absolute path of *binary* on PATH, or ``None``.

    Absolute because the argv is handed to a process whose working directory is
    the user's, not ours; and resolved through :func:`shutil.which` because on
    Windows every one of these CLIs is an npm ``.cmd`` shim, which is found only
    via ``PATHEXT``.
    """
    found = shutil.which(binary)
    return os.path.abspath(found) if found else None


def resource_label(profile: Profile) -> str:
    """A name for the resource that is safe to interpolate into a message.

    Error construction must not itself raise: ``endpoints.resource_name`` rejects
    a malformed endpoint, and a traceback thrown while explaining a missing
    deployment would hide the thing the user actually has to fix.
    """
    if profile.account:
        return profile.account
    try:
        return endpoints.resource_name(profile.endpoint)
    except ValueError:
        return profile.endpoint or "this endpoint"


def missing_binary(display: str, binary: str, install: str) -> AgentError:
    """The "agent binary missing" error (SPEC section 9): what, and how to install."""
    return AgentError(
        f"{display} is not installed, or `{binary}` is not on PATH.\nInstall it with: {install}"
    )


# ---------------------------------------------------------------------------
# Re-invoking ourselves
# ---------------------------------------------------------------------------

#: Escape hatch: an explicit path to the ``foundry`` executable, for the case
#: where this process cannot recognise itself (a zipapp, a wrapper script).
EXECUTABLE_ENV = "FOUNDRY_EXECUTABLE"

#: Last-resort command line: the current interpreter plus the console-script
#: entry point declared in ``pyproject.toml``. ``-m foundry`` is deliberately not
#: used -- a package ``__main__`` is not part of the frozen interface, and its
#: absence would fail *silently*, and an empty stdout is an unreadable token.
BOOTSTRAP = "import sys; from foundry.cli import main; sys.exit(main())"


def foundry_command() -> list[str]:
    """Argv prefix that re-invokes this CLI, absolute and cwd-independent.

    Agents run their credential command from whatever directory the user
    happened to be in, with whatever PATH they happened to have, minutes or
    hours after launch. A relative path, a bare name, or ``sys.argv[0]`` taken on
    faith each work on the developer's machine and fail on someone else's.
    """
    executable = _executable()
    if executable is not None:
        return [executable]
    return [os.path.abspath(sys.executable), "-c", BOOTSTRAP]


def _executable() -> str | None:
    for candidate in _executable_candidates():
        if candidate and os.path.isfile(candidate):
            return os.path.abspath(candidate)
    return None


def _executable_candidates() -> Iterator[str | None]:
    """Every plausible location of the installed ``foundry`` launcher, best first."""
    override = (os.environ.get(EXECUTABLE_ENV) or "").strip().strip("\"'")
    if override:
        yield os.path.expanduser(override)

    # What the user actually typed, when it names us and resolves to a file.
    argv0 = (sys.argv[0] if sys.argv else "") or ""
    if Path(argv0).stem.lower().startswith("foundry"):
        if os.sep in argv0 or (os.altsep and os.altsep in argv0):
            yield os.path.expanduser(argv0)
        else:
            yield shutil.which(argv0)

    # The console script sits beside the interpreter running us
    # (".../venv/Scripts/foundry.exe", ".../venv/bin/foundry"). More reliable
    # than PATH, which the agent's child process may not share.
    interpreter_dir = Path(os.path.abspath(sys.executable)).parent
    for directory in (interpreter_dir, interpreter_dir / "Scripts", interpreter_dir.parent / "bin"):
        yield shutil.which("foundry", path=str(directory))

    yield shutil.which("foundry")


def auth_token_argv(profile: Profile, *, endpoint: str | None = None) -> list[str]:
    """Argv for ``foundry auth-token`` against *profile*'s resource.

    This is what an agent's credential command runs -- every
    ``refresh_interval_ms`` for Codex, on a timer for the static-credential
    agents -- so it names the endpoint and subscription explicitly rather than
    trusting ``~/.foundry/config.json`` to still say the same thing an hour from
    now.
    """
    argv = foundry_command()
    argv += ["auth-token", "--endpoint", endpoints.normalize(endpoint or profile.endpoint)]
    subscription = (profile.subscription or "").strip()
    if subscription:
        argv += ["--subscription", subscription]
    return argv


# ---------------------------------------------------------------------------
# Shared implementation
# ---------------------------------------------------------------------------


class AgentBase:
    """Default implementations of the mechanical half of :class:`Agent`.

    Optional. It exists because "find the binary, exec it with the user's
    arguments appended, delete my directory on revert" is identical for every
    agent, and five copies of it would drift.
    """

    name: str = ""
    binary: str = ""
    display: str = ""
    #: One line telling the user how to install :attr:`binary` (SPEC section 9).
    install_hint: str = ""

    def is_installed(self) -> bool:
        return which(self.binary) is not None

    def executable(self) -> str:
        """Absolute path of the agent binary, or an error naming the install command."""
        found = which(self.binary)
        if found is None:
            raise missing_binary(self.display, self.binary, self.install_hint)
        return found

    def home(self) -> Path:
        """This agent's private config root, created on demand."""
        from foundry.profile import agent_home  # local: import time stays free of mkdir

        return agent_home(self.name)

    def launch_argv(self, profile: Profile, args: list[str]) -> list[str]:
        """The agent's own binary, then every user argument verbatim (SPEC section 7)."""
        return [self.executable(), *list(args)]

    def revert(self, profile: Profile) -> None:
        """Delete this agent's private config root and forget it.

        Nothing outside ``~/.foundry`` was ever written, so this is the whole of
        the undo (SPEC section 6.3). Mutates *profile*; the caller saves it.
        """
        directory = app_dir() / "agents" / self.name
        if directory.exists():
            try:
                shutil.rmtree(directory)
            except OSError as exc:
                raise AgentError(
                    f"cannot remove {directory}: {exc.strerror or exc}.\n"
                    "Close anything still running from that directory, or delete it by hand."
                ) from exc
        forget_agent(profile, self.name)
