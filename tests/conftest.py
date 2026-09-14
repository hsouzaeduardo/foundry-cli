# SPDX-License-Identifier: Apache-2.0
"""Shared test fixtures.

Two invariants are enforced here for the whole suite, both autouse:

*Isolation.* ``~/.foundry`` is redirected into ``tmp_path`` and ``Path.home()``
is redirected too, so a bug in :func:`foundry.profile.app_dir` cannot reach the
developer's real home. Every environment variable ``foundry`` reads is deleted,
because a developer with ``FOUNDRY_BEARER`` exported would otherwise watch the
auth tests pass for the wrong reason. Every in-process cache is reset.

*No real world.* ``subprocess`` and ``urllib.request.urlopen`` are replaced by
guards that raise. A test that forgets to mock the ``az`` boundary fails loudly
instead of shelling out to a real Azure CLI (SPEC section 11).
"""

from __future__ import annotations

import json
import os
import subprocess
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from foundry import auth, console, profile

# ---------------------------------------------------------------------------
# Environment isolation
# ---------------------------------------------------------------------------

#: Everything ``foundry`` (or the credential chain it feeds) reads from the
#: environment. Ambient values leaking in from the developer's shell have
#: already produced false passes in this project, so they are deleted, not
#: overwritten.
SCRUBBED_ENV_VARS: tuple[str, ...] = (
    "FOUNDRY_BEARER",
    "FOUNDRY_API_KEY",
    "FOUNDRY_HOME",
    "FOUNDRY_DEBUG",
    "FOUNDRY_TEST_ENDPOINT",
    "AZURE_CLIENT_ID",
    "AZURE_CLIENT_SECRET",
    "AZURE_TENANT_ID",
    "AZURE_AUTHORITY_HOST",
    "AZURE_CORE_ONLY_SHOW_ERRORS",
    "AZURE_CORE_NO_COLOR",
    "NO_COLOR",
    "FORCE_COLOR",
)


@pytest.fixture(autouse=True)
def isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Redirect the app directory into ``tmp_path`` and clear ambient state.

    Returns the (not yet created) ``~/.foundry`` equivalent for this test.
    """
    for name in SCRUBBED_ENV_VARS:
        monkeypatch.delenv(name, raising=False)

    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    app = home / ".foundry"

    monkeypatch.setenv("FOUNDRY_HOME", str(app))
    # Belt and braces: if app_dir() ever stopped honouring FOUNDRY_HOME, the
    # fallback must still land in tmp_path rather than the real home directory.
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))

    _reset_caches()
    yield app
    _reset_caches()


def _reset_caches() -> None:
    """Drop every in-process cache so tests cannot observe each other."""
    auth.clear_token_cache()
    auth._SP_PROBE.clear()
    console._printed = False
    try:  # cli pulls in typer/questionary; keep conftest usable without them
        from foundry import cli
    except ImportError:  # pragma: no cover - dependencies are declared
        return
    cli._AGENT_CACHE.clear()


# ---------------------------------------------------------------------------
# No real subprocesses, no real network
# ---------------------------------------------------------------------------


#: Captured before any patching, so the escape hatch below restores the genuine
#: article rather than whatever the previous fixture left behind.
_REAL_SUBPROCESS_RUN = subprocess.run
_REAL_URLOPEN = urllib.request.urlopen


class UnmockedBoundary(AssertionError):
    """Raised when a test reaches a real subprocess or a real socket."""


def _guard(what: str) -> Any:
    def deny(*args: Any, **kwargs: Any) -> Any:
        raise UnmockedBoundary(
            f"test reached the real {what} boundary with {args!r}; "
            "mock it (see the fake_az / fake_urlopen fixtures)"
        )

    return deny


@pytest.fixture(autouse=True)
def no_real_boundaries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail any test that shells out or opens a socket without mocking."""
    monkeypatch.setattr(subprocess, "run", _guard("subprocess.run"))
    monkeypatch.setattr(subprocess, "Popen", _guard("subprocess.Popen"))
    monkeypatch.setattr(subprocess, "check_output", _guard("subprocess.check_output"))
    monkeypatch.setattr(subprocess, "call", _guard("subprocess.call"))
    monkeypatch.setattr(urllib.request, "urlopen", _guard("urllib urlopen"))


@pytest.fixture
def allow_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    """Opt back into the real boundaries. No unit test should need this."""
    monkeypatch.setattr(subprocess, "run", _REAL_SUBPROCESS_RUN)
    monkeypatch.setattr(urllib.request, "urlopen", _REAL_URLOPEN)


# ---------------------------------------------------------------------------
# The `az` boundary
# ---------------------------------------------------------------------------


@dataclass
class AzCall:
    """One recorded ``subprocess.run`` invocation from :func:`foundry.auth.run_az`."""

    argv: list[str]
    kwargs: dict[str, Any]

    @property
    def args(self) -> list[str]:
        """The ``az`` arguments, without the leading ``az``."""
        return list(self.argv[1:])

    @property
    def env(self) -> dict[str, str]:
        return dict(self.kwargs.get("env") or {})


@dataclass
class FakeAz:
    """Scriptable stand-in for the ``az`` binary.

    Responses are matched, in order of registration, against the argument list
    (``["account", "get-access-token", ...]``). The first rule whose *match*
    tokens all appear in the arguments wins. A queued response registered with
    :meth:`queue` is consumed once, so a retry can be given a different answer
    than the first attempt.
    """

    calls: list[AzCall] = field(default_factory=list)
    _rules: list[tuple[tuple[str, ...], tuple[int, str, str], bool]] = field(default_factory=list)
    default: tuple[int, str, str] = (0, "", "")

    # -- scripting ----------------------------------------------------------

    def add(self, *match: str, returncode: int = 0, stdout: str = "", stderr: str = "") -> FakeAz:
        """Register a persistent response for calls containing *match*."""
        self._rules.append((match, (returncode, stdout, stderr), False))
        return self

    def queue(self, *match: str, returncode: int = 0, stdout: str = "", stderr: str = "") -> FakeAz:
        """Register a one-shot response, consumed by the first matching call."""
        self._rules.append((match, (returncode, stdout, stderr), True))
        return self

    def json(self, *match: str, payload: Any, returncode: int = 0) -> FakeAz:
        """Register a persistent response whose stdout is ``payload`` as JSON."""
        return self.add(*match, returncode=returncode, stdout=json.dumps(payload))

    # -- inspection ---------------------------------------------------------

    @property
    def argvs(self) -> list[list[str]]:
        """Every recorded argv, including the leading ``az``."""
        return [call.argv for call in self.calls]

    @property
    def arg_lists(self) -> list[list[str]]:
        """Every recorded ``az`` argument list, without the leading ``az``."""
        return [call.args for call in self.calls]

    def calls_matching(self, *match: str) -> list[AzCall]:
        return [c for c in self.calls if all(tok in c.args for tok in match)]

    # -- the boundary itself ------------------------------------------------

    def __call__(self, argv: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        recorded = list(argv) if isinstance(argv, (list, tuple)) else [str(argv)]
        self.calls.append(AzCall(argv=recorded, kwargs=dict(kwargs)))
        args = recorded[1:]

        for index, (match, response, one_shot) in enumerate(self._rules):
            if all(tok in args for tok in match):
                if one_shot:
                    self._rules.pop(index)
                return self._completed(recorded, response)
        return self._completed(recorded, self.default)

    @staticmethod
    def _completed(
        argv: list[str], response: tuple[int, str, str]
    ) -> subprocess.CompletedProcess[str]:
        returncode, stdout, stderr = response
        return subprocess.CompletedProcess(
            args=argv, returncode=returncode, stdout=stdout, stderr=stderr
        )


@pytest.fixture
def fake_az(monkeypatch: pytest.MonkeyPatch) -> FakeAz:
    """Replace the subprocess call inside :mod:`foundry.auth` with a fake ``az``."""
    fake = FakeAz()
    monkeypatch.setattr(auth.subprocess, "run", fake)
    return fake


def token_payload(
    access_token: str = "tok", *, expires_on: Any = None, expires_on_str: str | None = None
) -> str:
    """The JSON ``az account get-access-token`` prints."""
    body: dict[str, Any] = {"accessToken": access_token, "tokenType": "Bearer"}
    if expires_on is not None:
        body["expires_on"] = expires_on
    if expires_on_str is not None:
        body["expiresOn"] = expires_on_str
    return json.dumps(body)


# ---------------------------------------------------------------------------
# The HTTP boundary
# ---------------------------------------------------------------------------


class FakeResponse:
    """Minimal ``http.client.HTTPResponse`` stand-in usable as a context manager."""

    def __init__(self, status: int = 200, body: bytes = b"{}") -> None:
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


@dataclass
class FakeUrlopen:
    """Records requests and returns a scripted response (or raises one)."""

    requests: list[Any] = field(default_factory=list)
    result: Any = None

    def __call__(self, request: Any, timeout: float | None = None) -> Any:
        self.requests.append(request)
        outcome = self.result() if callable(self.result) else self.result
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome if outcome is not None else FakeResponse()

    @property
    def urls(self) -> list[str]:
        return [r.full_url if hasattr(r, "full_url") else str(r) for r in self.requests]


@pytest.fixture
def fake_urlopen(monkeypatch: pytest.MonkeyPatch) -> FakeUrlopen:
    """Replace ``urllib.request.urlopen`` as seen by :mod:`foundry.auth`."""
    fake = FakeUrlopen()
    monkeypatch.setattr(auth.urllib.request, "urlopen", fake)
    return fake


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------


@pytest.fixture
def make_profile() -> Any:
    """Factory for a fully populated :class:`foundry.profile.Profile`."""

    def _make(**overrides: Any) -> profile.Profile:
        fields: dict[str, Any] = {
            "endpoint": "https://my-resource.services.ai.azure.com",
            "subscription": "11111111-2222-3333-4444-555555555555",
            "resource_group": "rg-example",
            "account": "my-resource",
            "deployments": {
                "anthropic": {
                    "opus": "claude-opus-4-7",
                    "sonnet": "claude-sonnet-4-6",
                    "haiku": "claude-haiku-4-5",
                },
                "openai": ["gpt-5.4-mini", "gpt-5.4-nano"],
            },
            "agents": {},
        }
        fields.update(overrides)
        return profile.Profile(**fields)

    return _make


@pytest.fixture
def written_config(isolate: Path) -> Any:
    """Write a raw ``config.json`` body and return its path."""

    def _write(body: str | dict[str, Any]) -> Path:
        isolate.mkdir(parents=True, exist_ok=True)
        path = isolate / profile.CONFIG_NAME
        text = body if isinstance(body, str) else json.dumps(body, indent=2)
        path.write_text(text, encoding="utf-8")
        return path

    return _write


def read_config(app: Path) -> dict[str, Any]:
    """Parse the state file the implementation wrote."""
    return json.loads((app / profile.CONFIG_NAME).read_text(encoding="utf-8"))


def collect_strings(value: Any) -> list[str]:
    """Every string appearing anywhere in a nested JSON-ish structure."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        out: list[str] = []
        for key, val in value.items():
            if isinstance(key, str):
                out.append(key)
            out.extend(collect_strings(val))
        return out
    if isinstance(value, (list, tuple)):
        out = []
        for item in value:
            out.extend(collect_strings(item))
        return out
    return []


def assert_env_clean() -> None:
    """Guard used by tests that must observe no ambient credential state."""
    leaked = [name for name in SCRUBBED_ENV_VARS if os.environ.get(name) and name != "FOUNDRY_HOME"]
    assert leaked == [], f"ambient environment leaked into the test: {leaked}"
