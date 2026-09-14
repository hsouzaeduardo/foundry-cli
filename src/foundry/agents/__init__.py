# SPDX-License-Identifier: Apache-2.0
"""Agent registry, plus the machinery the static-credential agents share.

Two things live here, and they live together for one reason: adding an agent
must cost one module and one line.

**The registry.** :data:`_MODULES` maps a canonical agent name to the module
implementing it. Lookup goes through :data:`REGISTRY`, a mapping that imports on
first access. ``foundry --version`` has no business importing five agent
modules, and an agent that is broken -- or, while the tree is being written, not
there yet -- must break only the command that asks for it.

**Static credentials.** Claude Code renews its own credential through
``DefaultAzureCredential`` and Codex re-runs a credential *command*; Copilot,
OpenCode and Pi are each handed a value once and never ask again (agent
reference section 7). A Foundry token lives 72-90 minutes, which is shorter than
a working session, so :class:`CredentialRefresher` rewrites that value every 30
minutes for as long as the launcher -- and therefore the child -- is alive.

The refresher only helps an agent that reads its credential from a *file*, which
is why OpenCode and Pi use it and Copilot does not: Copilot's provider
configuration is environment-only, and no process can alter a child's
environment after it has started. See :mod:`foundry.agents.copilot`.
"""

from __future__ import annotations

import importlib
import inspect
import json
import os
import tempfile
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # imported for typing only, so this module loads standalone
    from foundry.agents.base import Agent
    from foundry.profile import Profile

__all__ = [
    "REFRESH_INTERVAL_SECONDS",
    "REGISTRY",
    "RETRY_INTERVAL_SECONDS",
    "AgentError",
    "CredentialRefresher",
    "dialect",
    "find",
    "get",
    "names",
    "normalize",
    "preferred_model",
    "start_refresher",
    "stop_refreshers",
    "write_json",
]


class AgentError(RuntimeError):
    """A user-fixable agent problem -- SPEC section 9, exit code 1.

    Separate from :class:`foundry.auth.AuthError` because the remedies are
    different in kind: "publish a deployment", "install this CLI". The message
    always names the fix, so a caller may print it verbatim.
    """


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

#: name -> module. The one line an agent costs.
_MODULES: dict[str, str] = {
    "claude": "foundry.agents.claude",
    "codex": "foundry.agents.codex",
    "copilot": "foundry.agents.copilot",
    "opencode": "foundry.agents.opencode",
    "pi": "foundry.agents.pi",
}

#: Spellings a user plausibly types. Canonical names are always accepted and are
#: not repeated here. Suffixes like ``-cli`` are stripped before lookup, so
#: ``copilot-cli`` needs no entry.
_ALIASES: dict[str, str] = {
    "cc": "claude",
    "claude-code": "claude",
    "claudecode": "claude",
    "anthropic": "claude",
    "openai-codex": "codex",
    "gh-copilot": "copilot",
    "github-copilot": "copilot",
    "githubcopilot": "copilot",
    "oc": "opencode",
    "open-code": "opencode",
    "pi-coding-agent": "pi",
}

#: Attributes an object must carry to be the module's agent instance. Checked
#: structurally because ``Agent`` is a Protocol -- and because five modules are
#: written independently, so a resolver that trusted one attribute name would be
#: a standing merge conflict.
_AGENT_ATTRS = ("name", "binary", "display", "is_installed", "configure", "launch_argv")


def names() -> tuple[str, ...]:
    """Canonical agent names, in the order the CLI should list them."""
    return tuple(_MODULES)


def normalize(name: str) -> str | None:
    """Canonical agent name for *name*, or ``None`` if it names no agent.

    Accepts the canonical name, the obvious aliases, and any of them wrapped in
    the noise a shell user adds: case, surrounding whitespace, underscores for
    dashes, a ``-cli`` or ``-code`` suffix.
    """
    key = (name or "").strip().lower().replace("_", "-").replace(" ", "-")
    while key.startswith("-"):
        key = key[1:]
    if not key:
        return None
    for candidate in (key, key.removesuffix("-cli"), key.removesuffix("-code")):
        if candidate in _MODULES:
            return candidate
        if candidate in _ALIASES:
            return _ALIASES[candidate]
    return None


def get(name: str) -> Agent:
    """The agent instance registered under *name* (aliases accepted).

    Raises ``KeyError`` naming every valid agent when *name* is not one, and
    ``ImportError`` when the module exists but cannot be loaded -- the two are
    genuinely different problems and deserve different messages.
    """
    canonical = normalize(name)
    if canonical is None:
        raise KeyError(f"{name!r} is not a foundry agent. Choose one of: {', '.join(names())}.")
    return REGISTRY[canonical]


def find(name: str) -> Agent | None:
    """Like :func:`get`, but ``None`` instead of ``KeyError`` for an unknown name."""
    canonical = normalize(name)
    if canonical is None:
        return None
    return REGISTRY[canonical]


class _Registry(Mapping[str, "Agent"]):
    """``name -> agent instance``, importing each module on first access.

    A plain dict built at import time would make every command pay for every
    agent, and would turn one unimportable module into a dead CLI.
    """

    def __init__(self, modules: Mapping[str, str]) -> None:
        self._modules = dict(modules)
        self._instances: dict[str, Agent] = {}
        self._lock = threading.Lock()

    def __getitem__(self, key: str) -> Agent:
        try:
            module_path = self._modules[key]
        except KeyError:
            raise KeyError(
                f"{key!r} is not a foundry agent. Choose one of: {', '.join(self._modules)}."
            ) from None
        with self._lock:
            cached = self._instances.get(key)
            if cached is None:
                cached = _load(key, module_path)
                self._instances[key] = cached
            return cached

    def __iter__(self) -> Iterator[str]:
        return iter(self._modules)

    def __len__(self) -> int:
        return len(self._modules)


def _load(name: str, module_path: str) -> Agent:
    try:
        module = importlib.import_module(module_path)
    except ImportError as exc:  # a missing or broken agent module
        raise ImportError(
            f"The {name} agent module ({module_path}) could not be imported: {exc}. "
            "Reinstall foundry, or report this with the full error."
        ) from exc

    instance = _agent_in(module, name)
    if instance is None:
        raise ImportError(
            f"{module_path} defines no agent instance for {name!r}. "
            "An agent module must expose its instance as `AGENT`."
        )
    return instance


def _agent_in(module: Any, name: str) -> Agent | None:
    """Find the module's agent instance.

    ``AGENT`` is the convention. The fallbacks exist because the agent modules
    are written independently of this one: a module that exports its instance as
    ``claude`` or as a zero-argument ``Claude`` class still registers, instead of
    failing at the user's first launch over a naming detail.
    """
    for attr in ("AGENT", name, name.upper(), name.capitalize()):
        candidate = getattr(module, attr, None)
        resolved = _as_agent(candidate, name)
        if resolved is not None:
            return resolved
    for _, candidate in vars(module).items():
        resolved = _as_agent(candidate, name)
        if resolved is not None:
            return resolved
    return None


def _as_agent(candidate: Any, name: str) -> Agent | None:
    if candidate is None or inspect.ismodule(candidate):
        return None
    if isinstance(candidate, type):
        if not all(hasattr(candidate, attr) for attr in _AGENT_ATTRS):
            return None
        try:
            candidate = candidate()
        except TypeError:
            return None
    if not all(hasattr(candidate, attr) for attr in _AGENT_ATTRS):
        return None
    return candidate if getattr(candidate, "name", None) == name else None


REGISTRY: Mapping[str, Agent] = _Registry(_MODULES)


# ---------------------------------------------------------------------------
# Model selection, shared by the agents that offer several deployments
# ---------------------------------------------------------------------------


def preferred_model(p: Profile, *, prefer: str = "anthropic") -> str | None:
    """The deployment to use when the user named none.

    Sonnet leads the Anthropic tier deliberately: it is what every one of these
    CLIs picks by default when left alone, so ``foundry <agent>`` behaves the
    same way with and without a Foundry endpoint. ``prefer="openai"`` flips the
    tiers for agents that speak only the OpenAI wire protocol.
    """
    anthropic = [n for n in (p.anthropic("sonnet"), p.anthropic("opus"), p.anthropic("haiku")) if n]
    openai = list(p.openai())
    order = openai + anthropic if prefer == "openai" else anthropic + openai
    return order[0] if order else None


def dialect(p: Profile, model: str) -> str:
    """``"anthropic"`` or ``"openai"``: which wire protocol serves *model*.

    Answered from the profile, whose classification came from ARM metadata --
    a deployment name is chosen freely by whoever published it and means
    nothing (SPEC section 2). The name is inspected only for a model the user
    passed with ``--model`` that is not in the profile at all, where there is no
    other signal and refusing to launch would be worse than a guess.
    """
    wanted = (model or "").strip()
    if not wanted:
        return "anthropic"
    if wanted in {p.anthropic(tier) for tier in ("opus", "sonnet", "haiku")} - {None}:
        return "anthropic"
    if wanted in p.openai():
        return "openai"
    lowered = wanted.lower()
    if any(marker in lowered for marker in ("claude", "anthropic", "opus", "sonnet", "haiku")):
        return "anthropic"
    return "openai"


# ---------------------------------------------------------------------------
# Config files
# ---------------------------------------------------------------------------


def write_json(path: Path, payload: dict[str, Any]) -> Path:
    """Write *payload* to *path* atomically, owner-readable only.

    Atomic because :class:`CredentialRefresher` rewrites these files underneath a
    running agent: a config observed half-written is a crash the user cannot
    explain. Owner-only because the file holds a bearer token -- the one place
    ``foundry`` puts a credential on disk, and only because OpenCode and Pi
    accept no other form of it.
    """
    body = json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=False) + "\n"
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)

    tmp_path: str | None = None
    try:
        fd, tmp_path = tempfile.mkstemp(dir=str(directory), prefix=f".{path.name}-", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        with suppress(OSError):  # a no-op on Windows, which has no mode bits
            os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
        tmp_path = None
    finally:
        if tmp_path is not None:
            with suppress(OSError):
                os.unlink(tmp_path)
    return path


# ---------------------------------------------------------------------------
# Credential refresh
# ---------------------------------------------------------------------------

#: Rewrite cadence. Measured Foundry token lifetimes are 72-90 minutes
#: (api notes, third pass), so 30 minutes leaves two clear attempts before the
#: oldest of them dies.
REFRESH_INTERVAL_SECONDS = 1800.0

#: Cadence after a failed rewrite. A failure is usually transient (``az`` busy,
#: a network blip) and waiting another half hour would spend the whole margin.
RETRY_INTERVAL_SECONDS = 60.0


class CredentialRefresher:
    """Re-run *rewrite* on a timer until stopped or the process exits.

    The thread is a daemon: ``foundry`` may hand the terminal over with
    ``os.exec*`` or die on a signal, and a refresher must never be the reason a
    launcher outlives its child.

    Failures are recorded on :attr:`last_error` and not printed. The child owns
    the terminal by then -- most of these agents draw a full-screen TUI -- and a
    line of ``foundry`` diagnostics scribbled across it costs more than it
    explains. Pass *on_error* to route the failure somewhere the user will
    actually see it.
    """

    __slots__ = (
        "_lock",
        "_on_error",
        "_rewrite",
        "_stop",
        "_thread",
        "interval",
        "label",
        "last_error",
        "retry_interval",
    )

    def __init__(
        self,
        rewrite: Callable[[], object],
        *,
        label: str,
        interval: float = REFRESH_INTERVAL_SECONDS,
        retry_interval: float = RETRY_INTERVAL_SECONDS,
        on_error: Callable[[BaseException], None] | None = None,
    ) -> None:
        self._rewrite = rewrite
        self._on_error = on_error
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.label = label
        self.interval = float(interval)
        self.retry_interval = float(retry_interval)
        self.last_error: BaseException | None = None

    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def start(self) -> CredentialRefresher:
        """Start the timer. Calling it twice is a no-op, not a second thread."""
        with self._lock:
            if self.running:
                return self
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, name=f"foundry-refresh-{self.label}", daemon=True
            )
            self._thread.start()
        return self

    def stop(self, *, timeout: float = 5.0) -> None:
        """Stop the timer and wait briefly for the thread to notice.

        Interrupting the wait rather than cancelling a timer means a rewrite in
        flight always finishes, so the file on disk is never left mid-write.
        """
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)
        self._thread = None

    def __enter__(self) -> CredentialRefresher:
        return self.start()

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    def _run(self) -> None:
        delay = self.interval
        while not self._stop.wait(delay):
            try:
                self._rewrite()
            except BaseException as exc:  # noqa: BLE001 - a refresher never kills the launcher
                self.last_error = exc
                delay = self.retry_interval
                if self._on_error is not None:
                    with suppress(BaseException):
                        self._on_error(exc)
            else:
                self.last_error = None
                delay = self.interval


#: Live refreshers, keyed by label. One per agent home: ``launch_env`` may be
#: called more than once for the same agent (a retry, a status probe) and each
#: call must not leave another thread rewriting the same file.
_REFRESHERS: dict[str, CredentialRefresher] = {}
_REFRESHERS_LOCK = threading.Lock()


def start_refresher(
    label: str,
    rewrite: Callable[[], object],
    *,
    interval: float = REFRESH_INTERVAL_SECONDS,
    on_error: Callable[[BaseException], None] | None = None,
) -> CredentialRefresher:
    """Start (or return) the single refresher for *label*."""
    with _REFRESHERS_LOCK:
        existing = _REFRESHERS.get(label)
        if existing is not None and existing.running:
            return existing
        refresher = CredentialRefresher(rewrite, label=label, interval=interval, on_error=on_error)
        _REFRESHERS[label] = refresher
    return refresher.start()


def stop_refreshers() -> None:
    """Stop every running refresher. Call it once the child process has exited."""
    with _REFRESHERS_LOCK:
        live = list(_REFRESHERS.values())
        _REFRESHERS.clear()
    for refresher in live:
        refresher.stop()


# Registering an agent is one line in _MODULES above; the modules themselves are
# imported lazily by _Registry, so nothing is imported here on purpose.
