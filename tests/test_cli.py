# SPDX-License-Identifier: Apache-2.0
"""The command surface (SPEC section 7).

What is protected here, in order of how expensive it is to get wrong:

*``auth-token`` owns stdout.* Codex invokes it as a credential command and reads
its standard output as the bearer, so the contract is "the token, one newline,
nothing else" -- a single stray log line silently corrupts Codex's
authentication. :func:`test_auth_token_prints_only_the_token` asserts the whole
stream byte for byte.

*Passthrough is total.* ``foundry claude -r`` must reach the agent as ``-r``,
including for flags that collide with foundry's own options.

*Failures are exit 1 with a remedy, never a traceback* (SPEC sections 7 and 9).

*``--dry-run`` writes nothing.* Not "writes nothing important": the assertion is
that the state directory does not exist afterwards.

Every test drives :func:`foundry.cli.main` through ``sys.argv``, because the
mapping from an exception to an exit code lives in ``main`` and a test that
called the command function directly would not exercise it. ``az``, HTTP, the
agent binaries and ``~/.foundry`` are all replaced.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from foundry import __version__, auth, cli, console, discovery
from foundry import profile as profile_mod
from foundry.agents import opencode as opencode_mod
from foundry.agents import pi as pi_mod
from foundry.profile import Profile

ENDPOINT = "https://my-resource.services.ai.azure.com"
OTHER_ENDPOINT = "https://other-resource.services.ai.azure.com"

TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJmb3VuZHJ5LXRlc3QifQ.c2lnbmF0dXJlLXZhbHVl"
SUBSCRIPTION = "11111111-2222-3333-4444-555555555555"

OPUS, SONNET, HAIKU = "alpha-1", "bravo-2", "charlie-3"
OPENAI_NEW, OPENAI_OLD = "delta-4", "echo-5"

#: Ambient state that must never reach a test (see tests/test_agents_rest.py).
LEAKY_ENV: tuple[str, ...] = (
    "FOUNDRY_BEARER",
    "FOUNDRY_API_KEY",
    "FOUNDRY_EXECUTABLE",
    "FOUNDRY_DEBUG",
    "AZURE_CLIENT_ID",
    "AZURE_CLIENT_SECRET",
    "AZURE_TENANT_ID",
    "NO_COLOR",
    "COPILOT_HOME",
    "COPILOT_MODEL",
    "COPILOT_PROVIDER_API_KEY",
    "COPILOT_PROVIDER_BEARER_TOKEN",
    "COPILOT_PROVIDER_WIRE_API",
    "COPILOT_PROVIDER_AZURE_API_VERSION",
    "OPENCODE_CONFIG",
    "OPENCODE_CONFIG_DIR",
    "PI_CODING_AGENT_DIR",
    "CODEX_HOME",
    "CLAUDE_CONFIG_DIR",
)


# --------------------------------------------------------------------------- #
# Harness                                                                       #
# --------------------------------------------------------------------------- #


@dataclass
class Run:
    """The observable result of one ``foundry ...`` invocation."""

    code: int
    out: str
    err: str

    @property
    def text(self) -> str:
        return self.out + self.err

    def assert_no_traceback(self) -> None:
        """SPEC section 9: a user-fixable failure prints a sentence, not a stack."""
        assert "Traceback" not in self.text
        assert "most recent call last" not in self.text


@pytest.fixture(autouse=True)
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate ``~/.foundry``, forbid `az`/HTTP, and drop ambient environment."""
    home = tmp_path / "dot-foundry"
    monkeypatch.setenv(profile_mod.HOME_ENV, str(home))
    for name in LEAKY_ENV:
        monkeypatch.delenv(name, raising=False)

    def no_az(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(f"a test tried to run az: {args!r} {kwargs!r}")

    def no_network(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(f"a test tried to open a socket: {args!r}")

    monkeypatch.setattr(auth, "run_az", no_az)
    monkeypatch.setattr(urllib.request, "urlopen", no_network)
    # A refresher would outlive the test as a daemon thread rewriting tmp_path.
    monkeypatch.setattr(opencode_mod, "start_refresher", lambda *a, **k: None)
    monkeypatch.setattr(pi_mod, "start_refresher", lambda *a, **k: None)
    # console tracks whether anything has been printed, purely for spacing.
    monkeypatch.setattr(console, "_printed", False)
    cli._AGENT_CACHE.clear()
    return home


@pytest.fixture
def run(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> Any:
    """Invoke ``foundry <argv>`` exactly as the console script does."""

    def invoke(*argv: str) -> Run:
        monkeypatch.setattr(sys, "argv", ["foundry", *argv])
        capsys.readouterr()
        code = 0
        try:
            cli.main()
        except SystemExit as exit_signal:
            code = 0 if exit_signal.code is None else int(exit_signal.code)
        captured = capsys.readouterr()
        return Run(code=code, out=captured.out, err=captured.err)

    return invoke


class FakeAgent:
    """A stand-in agent, so no test depends on an agent CLI being installed."""

    display = "Claude Code"

    def __init__(self, *, name: str = "claude", installed: bool = True) -> None:
        self.name = name
        self.binary = f"{name}-fake"
        self._installed = installed
        self.configured: list[str | None] = []
        self.env_calls: list[str | None] = []
        self.argv_calls: list[list[str]] = []

    def is_installed(self) -> bool:
        return self._installed

    def default_model(self, profile: Profile) -> str | None:
        del profile
        return SONNET

    def configure(self, profile: Profile, model: str | None) -> None:
        self.configured.append(model)
        profile_mod.record_agent(profile, self.name, home=profile_mod.agent_home(self.name))

    def launch_env(self, profile: Profile, model: str | None) -> dict[str, str]:
        del profile
        self.env_calls.append(model)
        return {"FAKE_AGENT_MODEL": model or ""}

    def launch_argv(self, profile: Profile, args: list[str]) -> list[str]:
        del profile
        self.argv_calls.append(list(args))
        return [self.binary, *args]


@dataclass
class Launch:
    """What ``cli._exec`` was handed."""

    argv: list[str]
    env: dict[str, str]


@pytest.fixture
def agent(monkeypatch: pytest.MonkeyPatch) -> FakeAgent:
    fake = FakeAgent()
    monkeypatch.setattr(cli, "_load_agent", lambda tool: fake)
    return fake


@pytest.fixture
def launched(monkeypatch: pytest.MonkeyPatch) -> list[Launch]:
    """Capture the exec instead of replacing the test process with an agent."""
    calls: list[Launch] = []

    def fake_exec(argv: list[str], env: dict[str, str], agent: Any) -> None:
        del agent
        calls.append(Launch(argv=list(argv), env=dict(env)))
        raise SystemExit(0)

    monkeypatch.setattr(cli, "_exec", fake_exec)
    return calls


def saved_profile(**overrides: Any) -> Profile:
    """Write a configured profile into the isolated ``~/.foundry``."""
    fields: dict[str, Any] = {
        "endpoint": ENDPOINT,
        "subscription": SUBSCRIPTION,
        "resource_group": "rg-example",
        "account": "my-resource",
        "deployments": {
            "anthropic": {"opus": OPUS, "sonnet": SONNET, "haiku": HAIKU},
            "openai": [OPENAI_NEW, OPENAI_OLD],
        },
        "agents": {"claude": {"configured_at": "2026-01-02T03:04:05Z"}},
    }
    fields.update(overrides)
    profile = Profile(**fields)
    profile_mod.save(profile)
    return profile


def kv_line(key: str, value: str) -> str:
    """One ``console.kv`` line, exactly as it is printed."""
    return "  " + key.ljust(15) + value


# --------------------------------------------------------------------------- #
# --version                                                                     #
# --------------------------------------------------------------------------- #


def test_version_prints_the_version_and_exits_zero(run: Any) -> None:
    result = run("--version")
    assert result.code == 0
    assert result.out == f"foundry {__version__}\n"
    assert result.err == ""


def test_version_does_not_create_the_state_directory(run: Any, sandbox: Path) -> None:
    """``foundry --version`` must not touch the filesystem (profile.app_dir is pure)."""
    assert run("--version").code == 0
    assert not sandbox.exists()


# --------------------------------------------------------------------------- #
# Launch: argument passthrough (SPEC section 7)                                  #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("argv", "expected_passthrough"),
    [
        # The example from SPEC section 7.
        (["claude", "-r"], ["-r"]),
        ([], []),
        (["claude", "-p", "explain this repo"], ["-p", "explain this repo"]),
        (["claude", "--dangerously-skip-permissions"], ["--dangerously-skip-permissions"]),
        # Order is preserved exactly, including a trailing value that looks like a flag.
        (["claude", "-c", "--verbose", "--", "-x"], ["-c", "--verbose", "-x"]),
        # Collides with foundry's own --version, which is a root option only.
        (["claude", "--version"], ["--version"]),
        # Collides with foundry's own options: `--` hands them to the agent.
        (["claude", "--", "--model", "opus"], ["--model", "opus"]),
        (["claude", "--", "--endpoint", "https://elsewhere"], ["--endpoint", "https://elsewhere"]),
        (["claude", "--", "--help"], ["--help"]),
        # foundry's own --model is consumed, and everything else still passes.
        (["claude", "--model", SONNET, "-r", "--", "--model", "other"], ["-r", "--model", "other"]),
    ],
)
def test_arguments_reach_the_agent_untouched(
    argv: list[str],
    expected_passthrough: list[str],
    run: Any,
    agent: FakeAgent,
    launched: list[Launch],
) -> None:
    saved_profile()
    result = run("claude", *argv[1:]) if argv else run("claude")

    assert result.code == 0
    assert len(launched) == 1
    assert launched[0].argv == [agent.binary, *expected_passthrough]
    assert agent.argv_calls == [expected_passthrough]


def test_launch_merges_the_agent_environment_over_the_scrubbed_one(
    run: Any, agent: FakeAgent, launched: list[Launch], monkeypatch: pytest.MonkeyPatch
) -> None:
    saved_profile()
    monkeypatch.setenv("FOUNDRY_TEST_MARKER", "inherited")

    assert run("claude").code == 0
    env = launched[0].env
    assert env["FAKE_AGENT_MODEL"] == SONNET  # the agent's own default deployment
    assert env["FOUNDRY_TEST_MARKER"] == "inherited"


def test_explicit_model_must_name_a_known_deployment(
    run: Any, agent: FakeAgent, launched: list[Launch]
) -> None:
    saved_profile()

    assert run("claude", "--model", OPENAI_OLD).code == 0
    assert agent.env_calls == [OPENAI_OLD]

    result = run("claude", "--model", "never-published")
    assert result.code == 1
    # console.fail writes the headline to stderr and console.note the remedy to
    # stdout (see foundry/console.py); the assertions follow that split exactly.
    assert "'never-published' is not a deployment on my-resource." in result.err
    assert f"{OPUS}, {SONNET}, {HAIKU}, {OPENAI_NEW}, {OPENAI_OLD}" in result.out
    assert f"foundry configure --endpoint {ENDPOINT}" in result.out
    result.assert_no_traceback()


def test_launching_an_unconfigured_agent_configures_it_first(
    run: Any, agent: FakeAgent, launched: list[Launch]
) -> None:
    saved_profile(agents={})

    assert run("claude").code == 0
    assert agent.configured == [None]
    reloaded = profile_mod.load()
    assert reloaded is not None
    assert reloaded.is_configured("claude")


# --------------------------------------------------------------------------- #
# Exit codes (SPEC section 7) and messages (SPEC section 9)                      #
# --------------------------------------------------------------------------- #


def test_not_signed_in_is_exit_one_and_says_az_login(
    run: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(auth, "signed_in", lambda: False)

    result = run("configure")

    assert result.code == cli.EXIT_USER == 1
    assert "Not signed in to Azure." in result.err
    assert "Run: az login" in result.out
    result.assert_no_traceback()


def test_no_deployment_is_exit_one_and_says_publish_one(
    run: Any, agent: FakeAgent, launched: list[Launch]
) -> None:
    saved_profile(deployments={"anthropic": {}, "openai": []})

    result = run("claude")

    assert result.code == 1
    assert "No chat deployment was found on my-resource." in result.err
    assert "Publish one in the Microsoft Foundry portal (https://ai.azure.com)" in result.out
    assert "foundry configure" in result.out
    assert launched == []
    result.assert_no_traceback()


def test_missing_agent_binary_is_exit_one_and_says_how_to_install(
    run: Any, monkeypatch: pytest.MonkeyPatch, launched: list[Launch]
) -> None:
    saved_profile()
    monkeypatch.setattr(cli, "_load_agent", lambda tool: FakeAgent(installed=False))

    result = run("claude")

    assert result.code == 1
    assert "Claude Code is not installed: `claude-fake` is not on PATH." in result.err
    assert "Install it with: npm install -g @anthropic-ai/claude-code" in result.out
    assert launched == []
    result.assert_no_traceback()


def test_an_unexpected_failure_is_exit_two_and_names_itself_a_bug(
    run: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(tool: str) -> Any:
        raise ValueError("something the launcher never anticipated")

    monkeypatch.setattr(cli, "_load_agent", explode)
    saved_profile()

    result = run("claude")

    assert result.code == cli.EXIT_INTERNAL == 2
    assert "Internal error: ValueError: something the launcher never anticipated" in result.err
    assert "This is a foundry bug." in result.out
    assert "FOUNDRY_DEBUG=1 for the traceback" in result.out
    assert "Traceback" not in result.text


def test_status_without_a_profile_is_exit_one(run: Any) -> None:
    result = run("status")

    assert result.code == 1
    assert "foundry is not configured yet." in result.err
    assert "az login && foundry configure" in result.out
    result.assert_no_traceback()


# --------------------------------------------------------------------------- #
# auth-token: stdout belongs to Codex                                           #
# --------------------------------------------------------------------------- #


def test_auth_token_prints_only_the_token(
    run: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Codex parses this stdout as a bearer: the token, a newline, nothing else."""
    saved_profile()
    minted: list[str | None] = []

    def fake_token(*, resource: str = auth.DATA_RESOURCE, subscription: str | None = None) -> str:
        del resource
        minted.append(subscription)
        # A credential command runs while other code may want to chatter; anything
        # that lands on stdout here would corrupt the token Codex reads.
        console.warn("a diagnostic that must not reach stdout")
        return TOKEN

    monkeypatch.setattr(auth, "token", fake_token)

    result = run("auth-token")

    assert result.code == 0
    assert result.out == TOKEN + "\n"
    assert result.out.count("\n") == 1
    assert not result.out.startswith(("\n", " "))
    assert "a diagnostic that must not reach stdout" in result.err
    assert minted == [SUBSCRIPTION]


@pytest.mark.parametrize(
    ("argv", "expected_subscription"),
    [
        # No endpoint given: the recorded profile's subscription is used.
        ([], SUBSCRIPTION),
        # The profile's own endpoint, spelled differently: still the same resource.
        (["--endpoint", "my-resource"], SUBSCRIPTION),
        (["--endpoint", f"{ENDPOINT}/api/projects/demo"], SUBSCRIPTION),
        # A different resource: the profile's subscription must not be assumed.
        (["--endpoint", OTHER_ENDPOINT], None),
        # An explicit subscription always wins.
        (["--subscription", "explicit-sub"], "explicit-sub"),
        (["--endpoint", OTHER_ENDPOINT, "--subscription", "explicit-sub"], "explicit-sub"),
    ],
)
def test_auth_token_picks_the_subscription(
    argv: list[str],
    expected_subscription: str | None,
    run: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saved_profile()
    minted: list[str | None] = []

    def fake_token(*, resource: str = auth.DATA_RESOURCE, subscription: str | None = None) -> str:
        del resource
        minted.append(subscription)
        return TOKEN

    monkeypatch.setattr(auth, "token", fake_token)

    result = run("auth-token", *argv)

    assert result.code == 0
    assert result.out == TOKEN + "\n"
    assert minted == [expected_subscription]


def _refusing_token(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(**kwargs: Any) -> str:
        raise auth.AuthError("Not signed in to Azure.\n  Run: az login")

    monkeypatch.setattr(auth, "token", refuse)


def test_auth_token_failure_is_exit_one_with_the_remedy(
    run: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    saved_profile()
    _refusing_token(monkeypatch)

    result = run("auth-token")

    assert result.code == 1
    assert "Not signed in to Azure." in result.err
    assert "Run: az login" in result.text
    assert TOKEN not in result.text
    result.assert_no_traceback()


@pytest.mark.xfail(
    strict=True,
    reason=(
        "DEFECT in src/foundry/cli.py: _report() prints the headline with console.fail "
        "(stderr) but every remaining line with console.note, which writes to STDOUT "
        "(foundry/console.py). A failing `foundry auth-token` therefore emits "
        "'  Run: az login' on stdout -- the stream Codex reads as the bearer token "
        "(agent reference section 2), contradicting this command's own docstring "
        "('failures go to stderr and exit 1'). Fix: route _report's note lines to "
        "stderr, or give console a note_err(). Remove this xfail when it is fixed."
    ),
)
def test_auth_token_failure_leaves_stdout_empty(run: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed mint must never put an error message where a token is expected.

    Codex reads this command's stdout as the credential. On failure it should read
    nothing at all -- an empty credential is a clean failure, while ``  Run: az
    login`` is a bearer token Codex will happily send to Foundry.
    """
    saved_profile()
    _refusing_token(monkeypatch)

    result = run("auth-token")

    assert result.code == 1
    assert result.out == ""


def test_auth_token_is_hidden_from_help(run: Any) -> None:
    result = run("--help")
    assert result.code == 0
    assert "auth-token" not in result.out
    for tool in cli.AGENT_ORDER:
        assert tool in result.out


# --------------------------------------------------------------------------- #
# configure --dry-run                                                           #
# --------------------------------------------------------------------------- #


@pytest.fixture
def azure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A resource with three Anthropic tiers and two OpenAI deployments, in ARM."""
    account = discovery.Account(
        subscription=SUBSCRIPTION,
        resource_group="rg-example",
        name="my-resource",
        endpoint=ENDPOINT,
        location="eastus",
    )
    published = [
        discovery.Deployment(OPUS, "claude-opus-4-7", "Anthropic", "1", "GlobalStandard"),
        discovery.Deployment(SONNET, "claude-sonnet-4-6", "Anthropic", "1", "GlobalStandard"),
        discovery.Deployment(HAIKU, "claude-haiku-4-5", "Anthropic", "1", "GlobalStandard"),
        discovery.Deployment(OPENAI_NEW, "gpt-5.4-mini", "OpenAI", "2026-01-01", "GlobalStandard"),
        discovery.Deployment(OPENAI_OLD, "gpt-5.4-nano", "OpenAI", "2026-01-01", "GlobalStandard"),
    ]

    monkeypatch.setattr(auth, "signed_in", lambda: True)
    monkeypatch.setattr(auth, "token", lambda **kwargs: TOKEN)
    monkeypatch.setattr(auth, "api_key", lambda *args, **kwargs: None)
    monkeypatch.setattr(auth, "account", lambda: {"user": {"name": "dev@example.com"}})
    monkeypatch.setattr(discovery, "reachable", lambda endpoint: None)
    monkeypatch.setattr(discovery, "find_account", lambda endpoint, sub=None: account)
    monkeypatch.setattr(discovery, "deployments", lambda acc: published)


def test_configure_dry_run_writes_nothing_at_all(run: Any, sandbox: Path, azure: None) -> None:
    result = run(
        "configure",
        "--endpoint",
        ENDPOINT,
        "--agents",
        "claude,codex,copilot,opencode,pi",
        "--dry-run",
    )

    assert result.code == 0
    # Not "no config.json": the state directory itself was never created.
    assert not sandbox.exists()
    assert profile_mod.load() is None
    assert "Nothing was written." in result.out


def test_configure_dry_run_names_the_files_it_would_write(
    run: Any, sandbox: Path, azure: None
) -> None:
    result = run(
        "configure",
        "--endpoint",
        ENDPOINT,
        "--agents",
        "codex,opencode,pi",
        "--dry-run",
    )

    assert result.code == 0
    # The paths are rewritten back to the real ~/.foundry, not the scratch dir the
    # dry run actually used -- otherwise it would advertise a path no real run
    # produces.
    for expected in (
        sandbox / "config.json",
        sandbox / "agents" / "codex" / "config.toml",
        sandbox / "agents" / "opencode" / "opencode.json",
        sandbox / "agents" / "pi" / "providers.json",
        sandbox / "agents" / "pi" / "settings.json",
    ):
        assert kv_line("file", str(expected)) in result.out.splitlines()


def test_configure_dry_run_redacts_every_credential(run: Any, azure: None) -> None:
    result = run(
        "configure",
        "--endpoint",
        ENDPOINT,
        "--agents",
        "copilot,opencode,pi",
        "--dry-run",
    )

    assert result.code == 0
    assert TOKEN not in result.text
    assert "<redacted" in result.out


def test_configure_writes_the_profile_and_reports_it(run: Any, sandbox: Path, azure: None) -> None:
    result = run("configure", "--endpoint", "my-resource", "--agents", "opencode")

    assert result.code == 0
    written = json.loads((sandbox / "config.json").read_text(encoding="utf-8"))
    assert written["version"] == 1
    assert written["endpoint"] == ENDPOINT
    assert written["subscription"] == SUBSCRIPTION
    assert written["resource_group"] == "rg-example"
    assert written["account"] == "my-resource"
    assert written["deployments"] == {
        "anthropic": {"opus": OPUS, "sonnet": SONNET, "haiku": HAIKU},
        "openai": [OPENAI_NEW, OPENAI_OLD],
    }
    assert list(written["agents"]) == ["opencode"]
    assert written["agents"]["opencode"]["home"] == str(sandbox / "agents" / "opencode")

    # No token reached disk (SPEC section 8), even though opencode.json holds one.
    assert TOKEN not in (sandbox / "config.json").read_text(encoding="utf-8")
    on_disk = json.loads(
        (sandbox / "agents" / "opencode" / "opencode.json").read_text(encoding="utf-8")
    )
    assert on_disk["provider"]["foundry-anthropic"]["options"]["apiKey"] == TOKEN


def test_configure_rejects_an_unknown_agent_name(run: Any, azure: None) -> None:
    result = run("configure", "--endpoint", ENDPOINT, "--agents", "claude,emacs")

    assert result.code == 1
    assert "Unknown agent(s): emacs." in result.err
    assert "Known agents: claude, codex, copilot, opencode, pi (or 'all')." in result.out
    result.assert_no_traceback()


# --------------------------------------------------------------------------- #
# status                                                                        #
# --------------------------------------------------------------------------- #


def test_status_reports_the_endpoint_routes_and_agents(
    run: Any, monkeypatch: pytest.MonkeyPatch, sandbox: Path
) -> None:
    saved_profile(
        agents={
            "claude": {
                "configured_at": "2026-01-02T03:04:05Z",
                "home": str(sandbox / "agents" / "claude"),
            }
        }
    )
    monkeypatch.setattr(auth, "account", lambda: {"user": {"name": "dev@example.com"}})

    result = run("status")
    lines = result.out.splitlines()

    assert result.code == 0
    assert kv_line("endpoint", ENDPOINT) in lines
    # The two routes, exactly as SPEC section 5 defines them.
    assert kv_line("anthropic", f"{ENDPOINT}/anthropic") in lines
    assert kv_line("openai", f"{ENDPOINT}/openai/v1") in lines
    assert kv_line("subscription", SUBSCRIPTION) in lines
    assert kv_line("resource group", "rg-example") in lines
    assert kv_line("account", "my-resource") in lines
    assert kv_line("signed in as", "dev@example.com") in lines
    assert kv_line("opus", OPUS) in lines
    assert kv_line("sonnet", SONNET) in lines
    assert kv_line("haiku", HAIKU) in lines
    assert kv_line("openai", f"{OPENAI_NEW}, {OPENAI_OLD}") in lines

    configured = [line for line in lines if line.startswith("  claude")][0]
    assert "configured" in configured
    assert "2026-01-02T03:04:05Z" in configured
    for tool in ("codex", "copilot", "opencode", "pi"):
        assert kv_line(tool, "not configured") in lines


def test_status_survives_a_machine_with_no_az(run: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """The recorded profile is still describable without a working Azure CLI."""
    saved_profile()

    def refuse() -> dict[str, Any]:
        raise auth.AuthError("The Azure CLI (`az`) is not on PATH")

    monkeypatch.setattr(auth, "account", refuse)

    result = run("status")

    assert result.code == 0
    assert kv_line("endpoint", ENDPOINT) in result.out.splitlines()
    assert "signed in as" not in result.out


def test_status_says_when_no_deployment_is_recorded(
    run: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    saved_profile(deployments={})
    monkeypatch.setattr(auth, "account", lambda: None)

    result = run("status")

    assert result.code == 0
    assert "none recorded -- run `foundry configure` to rediscover" in result.out


# --------------------------------------------------------------------------- #
# revert                                                                        #
# --------------------------------------------------------------------------- #


def test_revert_removes_the_whole_app_directory(run: Any, sandbox: Path) -> None:
    saved_profile()
    for tool in ("claude", "codex", "copilot", "opencode", "pi"):
        directory = sandbox / "agents" / tool
        directory.mkdir(parents=True)
        (directory / "config").write_text("owned by foundry\n", encoding="utf-8")

    result = run("revert", "--yes")

    assert result.code == 0
    # Nothing outside ~/.foundry was ever written, so deleting it is the whole undo.
    assert not sandbox.exists()
    assert profile_mod.load() is None
    assert "foundry state removed." in result.out
    for tool in ("claude", "codex", "copilot", "opencode", "pi"):
        assert kv_line(tool, str(sandbox / "agents" / tool)) in result.out.splitlines()


def test_revert_keeps_backups_and_says_so(run: Any, sandbox: Path) -> None:
    """``~/.foundry/backups`` holds the only copy of any user file foundry edited."""
    saved_profile()
    backups = sandbox / "backups"
    backups.mkdir(parents=True)
    (backups / "config.toml.20260102T030405Z.bak").write_text("original\n", encoding="utf-8")

    result = run("revert", "--yes")

    assert result.code == 0
    assert not (sandbox / "config.json").exists()
    assert not (sandbox / "agents").exists()
    assert (backups / "config.toml.20260102T030405Z.bak").read_text(
        encoding="utf-8"
    ) == "original\n"
    assert f"Kept {backups}" in result.err


def test_revert_with_nothing_to_remove_is_a_no_op(run: Any, sandbox: Path) -> None:
    result = run("revert", "--yes")

    assert result.code == 0
    assert "Nothing to remove" in result.out
    assert not sandbox.exists()
