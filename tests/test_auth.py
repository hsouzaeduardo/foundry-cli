# SPDX-License-Identifier: Apache-2.0
"""Azure CLI authentication (SPEC section 3).

Everything here mocks at the ``subprocess``/``urllib`` boundary: the
``fake_az`` fixture replaces the call inside :func:`foundry.auth.run_az`, and
``conftest`` fails any test that reaches a real one.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
from datetime import datetime
from typing import Any

import pytest
from conftest import FakeAz, FakeResponse, token_payload

from foundry import auth

DATA = auth.DATA_RESOURCE
FALLBACK = auth.DATA_FALLBACK
ARM = auth.ARM_RESOURCE

TENANT = "11111111-1111-1111-1111-111111111111"
OTHER_TENANT = "22222222-2222-2222-2222-222222222222"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeClock:
    """Stands in for the ``time`` module inside :mod:`foundry.auth` only."""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.now = start

    def time(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(auth, "time", fake)
    return fake


def token_argv(resource: str, subscription: str | None = None) -> list[str]:
    """The exact argv SPEC section 3 mandates for a token mint."""
    argv = ["az", "account", "get-access-token", "--resource", resource, "--output", "json"]
    if subscription:
        argv += ["--subscription", subscription]
    return argv


def live_token(value: str = "TOK", lifetime: int = 3600) -> str:
    return token_payload(value, expires_on=int(time.time()) + lifetime)


def account_json(tenant: str = TENANT, sub: str = "sub-1") -> str:
    return json.dumps(
        {
            "id": sub,
            "name": "Example",
            "tenantId": tenant,
            "user": {"name": "dev@example.com"},
            "isDefault": True,
        }
    )


def signed_in_az(fake_az: FakeAz, *, tenant: str = TENANT, token: str = "TOK") -> FakeAz:
    fake_az.add("show", stdout=account_json(tenant))
    fake_az.add("get-access-token", stdout=live_token(token))
    return fake_az


# ---------------------------------------------------------------------------
# run_az: the subprocess boundary
# ---------------------------------------------------------------------------


class TestRunAz:
    def test_argv_is_az_plus_arguments(self, fake_az: FakeAz) -> None:
        auth.run_az(["account", "show"])
        assert fake_az.argvs == [["az", "account", "show"]]

    def test_subprocess_keywords_match_the_windows_rules(self, fake_az: FakeAz) -> None:
        """SPEC section 3: shell=True on win32, explicit utf-8 with errors=replace."""
        auth.run_az(["account", "show"], timeout=17)
        kwargs = fake_az.calls[0].kwargs
        assert kwargs["capture_output"] is True
        assert kwargs["encoding"] == "utf-8"
        assert kwargs["errors"] == "replace"
        assert kwargs["timeout"] == 17
        assert kwargs["shell"] is (sys.platform == "win32")
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["check"] is False

    def test_default_timeout_is_sixty_seconds(self, fake_az: FakeAz) -> None:
        auth.run_az(["account", "show"])
        assert fake_az.calls[0].kwargs["timeout"] == 60

    def test_child_env_silences_az_banners(self, fake_az: FakeAz) -> None:
        auth.run_az(["account", "show"])
        env = fake_az.calls[0].env
        assert env["AZURE_CORE_ONLY_SHOW_ERRORS"] == "true"
        assert env["AZURE_CORE_NO_COLOR"] == "true"

    def test_child_env_does_not_override_an_explicit_setting(
        self, fake_az: FakeAz, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AZURE_CORE_NO_COLOR", "false")
        auth.run_az(["account", "show"])
        assert fake_az.calls[0].env["AZURE_CORE_NO_COLOR"] == "false"

    @pytest.mark.parametrize(
        "stdout, stderr",
        [
            pytest.param(None, None, id="both none"),
            pytest.param(None, "boom", id="stdout none"),
            pytest.param("{}", None, id="stderr none"),
        ],
    )
    def test_none_streams_become_empty_strings(
        self, monkeypatch: pytest.MonkeyPatch, stdout: str | None, stderr: str | None
    ) -> None:
        """Regression: the Windows encoding failure hands back ``stdout=None``.

        SPEC section 3 records this as measured, not theoretical. It must not
        become a ``TypeError`` three call frames away.
        """

        def fake_run(argv: Any, **kwargs: Any) -> subprocess.CompletedProcess[Any]:
            return subprocess.CompletedProcess(
                args=argv, returncode=0, stdout=stdout, stderr=stderr
            )

        monkeypatch.setattr(auth.subprocess, "run", fake_run)
        proc = auth.run_az(["account", "show"])
        assert proc.stdout == (stdout if stdout is not None else "")
        assert proc.stderr == (stderr if stderr is not None else "")
        assert isinstance(proc.stdout, str) and isinstance(proc.stderr, str)

    def test_none_stdout_does_not_crash_az_json(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            auth.subprocess,
            "run",
            lambda argv, **kw: subprocess.CompletedProcess(argv, 0, None, None),
        )
        assert auth.az_json(["account", "show"]) is None
        assert auth.account() is None
        assert auth.subscriptions() == []

    def test_none_stdout_becomes_an_auth_error_not_a_type_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            auth.subprocess,
            "run",
            lambda argv, **kw: subprocess.CompletedProcess(argv, 0, None, None),
        )
        with pytest.raises(auth.AuthError) as excinfo:
            auth.token()
        assert "az login" in str(excinfo.value)

    def test_missing_binary_raises_with_install_instructions(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(*args: Any, **kwargs: Any) -> Any:
            raise FileNotFoundError(2, "No such file or directory", "az")

        monkeypatch.setattr(auth.subprocess, "run", boom)
        with pytest.raises(auth.AuthError) as excinfo:
            auth.run_az(["account", "show"])
        message = str(excinfo.value)
        assert "https://aka.ms/InstallAzureCLI" in message
        assert "az login" in message

    @pytest.mark.parametrize(
        "stderr",
        [
            pytest.param("'az' is not recognized as an internal or external command", id="cmd.exe"),
            pytest.param("az: command not found", id="posix shell"),
            pytest.param("The system cannot find the path specified", id="cannot find the path"),
        ],
    )
    def test_shell_reporting_a_missing_az_raises(self, fake_az: FakeAz, stderr: str) -> None:
        fake_az.default = (1, "", stderr)
        with pytest.raises(auth.AuthError, match="aka.ms/InstallAzureCLI"):
            auth.run_az(["account", "show"])

    def test_ordinary_failure_is_returned_not_raised(self, fake_az: FakeAz) -> None:
        fake_az.default = (1, "", "ERROR: Please run 'az login' to set up an account")
        proc = auth.run_az(["account", "show"])
        assert proc.returncode == 1
        assert "az login" in proc.stderr

    def test_timeout_raises_with_the_command_and_the_budget(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(*args: Any, **kwargs: Any) -> Any:
            raise subprocess.TimeoutExpired(cmd="az", timeout=5)

        monkeypatch.setattr(auth.subprocess, "run", boom)
        with pytest.raises(auth.AuthError) as excinfo:
            auth.run_az(["account", "show"], timeout=5)
        message = str(excinfo.value)
        assert "`az account show` did not finish within 5s." in message
        assert "az account show" in message


class TestAzJson:
    @pytest.mark.parametrize(
        "args, expected",
        [
            pytest.param(
                ["account", "show"], ["account", "show", "--output", "json"], id="appended"
            ),
            pytest.param(
                ["account", "show", "-o", "tsv"],
                ["account", "show", "-o", "tsv"],
                id="-o respected",
            ),
            pytest.param(
                ["account", "show", "--output", "table"],
                ["account", "show", "--output", "table"],
                id="--output respected",
            ),
            pytest.param(
                ["account", "show", "--output=none"],
                ["account", "show", "--output=none"],
                id="--output= respected",
            ),
        ],
    )
    def test_output_json_is_added_only_when_absent(
        self, fake_az: FakeAz, args: list[str], expected: list[str]
    ) -> None:
        fake_az.default = (0, "{}", "")
        auth.az_json(list(args))
        assert fake_az.arg_lists == [expected]

    @pytest.mark.parametrize(
        "stdout, expected",
        [
            pytest.param('{"a": 1}', {"a": 1}, id="object"),
            pytest.param("[1, 2]", [1, 2], id="array"),
            pytest.param('﻿{"a": 1}', {"a": 1}, id="utf-8 bom"),
            pytest.param('WARNING: upgrade available\n{"a": 1}', {"a": 1}, id="warning banner"),
            pytest.param("WARNING: noise\n[1]", [1], id="warning before array"),
            pytest.param("", None, id="empty"),
            pytest.param("   \n ", None, id="blank"),
            pytest.param("not json at all", None, id="garbage"),
            pytest.param("WARNING: noise\n{broken", None, id="garbage after banner"),
        ],
    )
    def test_parsing(self, fake_az: FakeAz, stdout: str, expected: Any) -> None:
        fake_az.default = (0, stdout, "")
        assert auth.az_json(["account", "show"]) == expected

    def test_failure_returns_none(self, fake_az: FakeAz) -> None:
        fake_az.default = (1, '{"a": 1}', "ERROR: nope")
        assert auth.az_json(["account", "show"]) is None


# ---------------------------------------------------------------------------
# Token minting: argv, retry, cache
# ---------------------------------------------------------------------------


class TestTokenArgv:
    @pytest.mark.parametrize(
        "resource",
        [
            pytest.param(DATA, id="data plane"),
            pytest.param(FALLBACK, id="data plane fallback"),
            pytest.param(ARM, id="management plane"),
        ],
    )
    def test_exact_argv_per_resource(self, fake_az: FakeAz, resource: str) -> None:
        fake_az.add("get-access-token", stdout=live_token())
        auth.token(resource=resource)
        assert fake_az.argvs == [token_argv(resource)]

    def test_default_resource_is_the_data_plane(self, fake_az: FakeAz) -> None:
        fake_az.add("get-access-token", stdout=live_token())
        auth.token()
        assert fake_az.argvs == [token_argv("https://ai.azure.com")]

    def test_subscription_is_appended(self, fake_az: FakeAz) -> None:
        fake_az.add("get-access-token", stdout=live_token())
        auth.token(resource=ARM, subscription="sub-42")
        assert fake_az.argvs == [token_argv(ARM, "sub-42")]

    def test_resource_constants_match_the_spec(self) -> None:
        assert auth.DATA_RESOURCE == "https://ai.azure.com"
        assert auth.DATA_FALLBACK == "https://cognitiveservices.azure.com"
        assert auth.ARM_RESOURCE == "https://management.azure.com"

    def test_returned_value_is_the_access_token_field(self, fake_az: FakeAz) -> None:
        fake_az.add("get-access-token", stdout=live_token("HEADER.PAYLOAD.SIG"))
        assert auth.token() == "HEADER.PAYLOAD.SIG"

    def test_whitespace_around_the_token_is_stripped(self, fake_az: FakeAz) -> None:
        fake_az.add("get-access-token", stdout='{"accessToken": "  TOK  "}')
        assert auth.token() == "TOK"


class TestDataPlaneRetry:
    def test_failure_on_ai_azure_com_retries_cognitiveservices(self, fake_az: FakeAz) -> None:
        fake_az.add(DATA, returncode=1, stderr="ERROR: AADSTS500011 resource principal not found")
        fake_az.add(FALLBACK, stdout=live_token("FALLBACK-TOK"))

        assert auth.token() == "FALLBACK-TOK"
        assert fake_az.argvs == [token_argv(DATA), token_argv(FALLBACK)]

    def test_retry_result_is_cached_under_both_resource_keys(self, fake_az: FakeAz) -> None:
        fake_az.add(DATA, returncode=1, stderr="ERROR: nope")
        fake_az.add(FALLBACK, stdout=live_token("FALLBACK-TOK"))

        auth.token()
        assert set(auth._CACHE) == {(DATA, None), (FALLBACK, None)}
        assert auth._CACHE[(DATA, None)] is auth._CACHE[(FALLBACK, None)]
        assert auth._CACHE[(DATA, None)].value == "FALLBACK-TOK"

    def test_an_explicit_fallback_request_reuses_the_retry_token(self, fake_az: FakeAz) -> None:
        fake_az.add(DATA, returncode=1, stderr="ERROR: nope")
        fake_az.add(FALLBACK, stdout=live_token("FALLBACK-TOK"))

        auth.token()
        before = len(fake_az.calls)
        assert auth.token(resource=FALLBACK) == "FALLBACK-TOK"
        assert len(fake_az.calls) == before

    def test_the_retry_keeps_the_subscription(self, fake_az: FakeAz) -> None:
        fake_az.add(DATA, returncode=1, stderr="ERROR: nope")
        fake_az.add(FALLBACK, stdout=live_token())
        auth.token(subscription="sub-42")
        assert fake_az.argvs == [token_argv(DATA, "sub-42"), token_argv(FALLBACK, "sub-42")]

    def test_no_retry_for_the_management_plane(self, fake_az: FakeAz) -> None:
        """An inference token is rejected by ARM, so a retry would be nonsense."""
        fake_az.default = (1, "", "ERROR: nope")
        with pytest.raises(auth.AuthError):
            auth.token(resource=ARM)
        assert fake_az.argvs == [token_argv(ARM)]

    def test_no_retry_when_the_fallback_itself_was_requested(self, fake_az: FakeAz) -> None:
        fake_az.default = (1, "", "ERROR: nope")
        with pytest.raises(auth.AuthError):
            auth.token(resource=FALLBACK)
        assert fake_az.argvs == [token_argv(FALLBACK)]

    @pytest.mark.parametrize(
        "returncode, stdout",
        [
            pytest.param(1, "", id="nonzero exit"),
            pytest.param(0, "", id="empty stdout"),
            pytest.param(0, "not json", id="unparseable"),
            pytest.param(0, "[1, 2]", id="not an object"),
            pytest.param(0, '{"accessToken": ""}', id="blank token"),
            pytest.param(0, '{"tokenType": "Bearer"}', id="no token field"),
        ],
    )
    def test_every_unusable_mint_triggers_the_retry(
        self, fake_az: FakeAz, returncode: int, stdout: str
    ) -> None:
        fake_az.add(DATA, returncode=returncode, stdout=stdout)
        fake_az.add(FALLBACK, stdout=live_token("FALLBACK-TOK"))
        assert auth.token() == "FALLBACK-TOK"

    def test_both_failing_raises_and_caches_nothing(self, fake_az: FakeAz) -> None:
        fake_az.default = (1, "", "ERROR: nope")
        with pytest.raises(auth.AuthError):
            auth.token()
        assert auth._CACHE == {}


class TestTokenErrors:
    def test_not_signed_in_says_run_az_login(self, fake_az: FakeAz) -> None:
        fake_az.default = (1, "", "ERROR: Please run 'az login' to setup account.")
        with pytest.raises(auth.AuthError) as excinfo:
            auth.token()
        message = str(excinfo.value)
        assert message.startswith("Not signed in to Azure. Run: az login")
        assert "Please run 'az login' to setup account." in message

    def test_not_signed_in_names_the_subscription(self, fake_az: FakeAz) -> None:
        fake_az.default = (1, "", "ERROR: Please run az login")
        with pytest.raises(auth.AuthError) as excinfo:
            auth.token(resource=ARM, subscription="sub-42")
        assert "Not signed in to Azure for subscription sub-42." in str(excinfo.value)

    def test_unknown_subscription_says_how_to_list_them(self, fake_az: FakeAz) -> None:
        fake_az.default = (1, "", "ERROR: The subscription sub-42 was not found.")
        with pytest.raises(auth.AuthError) as excinfo:
            auth.token(resource=ARM, subscription="sub-42")
        message = str(excinfo.value)
        assert "Subscription sub-42 is not available to the signed-in account." in message
        assert "az account list -o table" in message
        assert "az login --tenant <tenant-id>" in message

    def test_generic_failure_names_the_resource_and_the_fix(self, fake_az: FakeAz) -> None:
        fake_az.default = (1, "", "ERROR: AADSTS900023 tenant not found")
        with pytest.raises(auth.AuthError) as excinfo:
            auth.token(resource=ARM)
        message = str(excinfo.value)
        assert "Could not get an Azure token for https://management.azure.com." in message
        assert "Run `az login`" in message
        assert "az said: ERROR: AADSTS900023 tenant not found" in message

    def test_detail_falls_back_to_stdout(self, fake_az: FakeAz) -> None:
        fake_az.default = (1, "\n\n  something on stdout  \n", "")
        with pytest.raises(auth.AuthError) as excinfo:
            auth.token(resource=ARM)
        assert "az said: something on stdout" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Escape hatches
# ---------------------------------------------------------------------------


class TestEnvironmentOverrides:
    def test_bearer_short_circuits_the_data_plane(
        self, fake_az: FakeAz, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FOUNDRY_BEARER", "PRE-MINTED")
        assert auth.token() == "PRE-MINTED"
        assert fake_az.calls == []
        assert auth._CACHE == {}

    def test_bearer_short_circuits_the_fallback_audience_too(
        self, fake_az: FakeAz, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FOUNDRY_BEARER", "PRE-MINTED")
        assert auth.token(resource=FALLBACK) == "PRE-MINTED"
        assert fake_az.calls == []

    def test_bearer_does_not_short_circuit_arm(
        self, fake_az: FakeAz, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SPEC section 3: ARM rejects an inference token, so it still shells out."""
        monkeypatch.setenv("FOUNDRY_BEARER", "PRE-MINTED")
        fake_az.add("get-access-token", stdout=live_token("ARM-TOK"))

        assert auth.token(resource=ARM) == "ARM-TOK"
        assert fake_az.argvs == [token_argv(ARM)]

    def test_bearer_wins_over_api_key(
        self, fake_az: FakeAz, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FOUNDRY_BEARER", "PRE-MINTED")
        monkeypatch.setenv("FOUNDRY_API_KEY", "RESOURCE-KEY")
        assert auth.token() == "PRE-MINTED"
        assert fake_az.calls == []

    def test_api_key_is_used_when_no_bearer_is_set(
        self, fake_az: FakeAz, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FOUNDRY_API_KEY", "RESOURCE-KEY")
        assert auth.token() == "RESOURCE-KEY"
        assert fake_az.calls == []

    def test_api_key_does_not_short_circuit_arm(
        self, fake_az: FakeAz, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SPEC section 3: model discovery still needs az; ARM has no key auth."""
        monkeypatch.setenv("FOUNDRY_API_KEY", "RESOURCE-KEY")
        fake_az.add("get-access-token", stdout=live_token("ARM-TOK"))
        assert auth.token(resource=ARM) == "ARM-TOK"
        assert fake_az.argvs == [token_argv(ARM)]

    @pytest.mark.parametrize(
        "value",
        [pytest.param("", id="empty"), pytest.param("   ", id="whitespace")],
    )
    def test_a_blank_override_is_ignored(
        self, fake_az: FakeAz, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        monkeypatch.setenv("FOUNDRY_BEARER", value)
        monkeypatch.setenv("FOUNDRY_API_KEY", value)
        fake_az.add("get-access-token", stdout=live_token("MINTED"))
        assert auth.token() == "MINTED"

    def test_override_is_stripped(self, fake_az: FakeAz, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FOUNDRY_BEARER", "  PRE-MINTED  ")
        assert auth.token() == "PRE-MINTED"

    def test_no_ambient_override_leaks_in(self, fake_az: FakeAz) -> None:
        """The conftest fixture must have deleted the developer's own exports."""
        fake_az.add("get-access-token", stdout=live_token("MINTED"))
        assert auth.token() == "MINTED"


# ---------------------------------------------------------------------------
# The token cache
# ---------------------------------------------------------------------------


class TestCachedTokenUsable:
    @pytest.mark.parametrize(
        "now, expires_at, refresh_at, expected",
        [
            pytest.param(100.0, 1000.0, 800.0, True, id="fresh"),
            pytest.param(799.0, 1000.0, 800.0, True, id="one second before refresh"),
            pytest.param(800.0, 1000.0, 800.0, False, id="at the refresh point"),
            pytest.param(801.0, 1000.0, 800.0, False, id="past the refresh point"),
            pytest.param(100.0, 200.0, 900.0, False, id="inside the 120s expiry margin"),
            pytest.param(79.0, 200.0, 900.0, True, id="just outside the expiry margin"),
            pytest.param(80.0, 200.0, 900.0, False, id="exactly at the expiry margin"),
            pytest.param(100.0, None, 800.0, True, id="unknown expiry, before refresh"),
            pytest.param(900.0, None, 800.0, False, id="unknown expiry, after refresh"),
            pytest.param(2000.0, 1000.0, 900.0, False, id="already expired"),
        ],
    )
    def test_usable(
        self, now: float, expires_at: float | None, refresh_at: float, expected: bool
    ) -> None:
        entry = auth._CachedToken(value="TOK", expires_at=expires_at, refresh_at=refresh_at)
        assert entry.usable(now) is expected

    def test_min_remaining_margin_is_two_minutes(self) -> None:
        assert auth._MIN_REMAINING_SECONDS == 120.0

    def test_refresh_fraction_is_eighty_percent(self) -> None:
        assert auth._REFRESH_FRACTION == 0.8


class TestRefreshAt:
    def test_eighty_percent_of_remaining_lifetime(self, clock: FakeClock) -> None:
        assert auth._refresh_at(clock.now + 1000.0) == clock.now + 800.0

    def test_unknown_expiry_uses_a_thirty_minute_cadence(self, clock: FakeClock) -> None:
        assert auth._refresh_at(None) == clock.now + 1800.0
        assert auth._UNKNOWN_LIFETIME_SECONDS == 1800.0

    def test_an_already_dead_token_refreshes_immediately(self, clock: FakeClock) -> None:
        assert auth._refresh_at(clock.now - 5.0) == clock.now


class TestTokenCache:
    def test_second_call_is_served_from_cache(self, fake_az: FakeAz, clock: FakeClock) -> None:
        fake_az.add("get-access-token", stdout=token_payload("TOK", expires_on=clock.now + 3600))
        assert auth.token() == "TOK"
        assert auth.token() == "TOK"
        assert len(fake_az.calls) == 1

    def test_re_mints_only_after_eighty_percent_of_the_lifetime(
        self, fake_az: FakeAz, clock: FakeClock
    ) -> None:
        fake_az.queue(
            "get-access-token", stdout=token_payload("FIRST", expires_on=clock.now + 1000)
        )
        fake_az.add("get-access-token", stdout=token_payload("SECOND", expires_on=clock.now + 5000))

        assert auth.token() == "FIRST"

        clock.advance(799)  # 79.9% spent
        assert auth.token() == "FIRST"
        assert len(fake_az.calls) == 1

        clock.advance(2)  # 80.1% spent
        assert auth.token() == "SECOND"
        assert len(fake_az.calls) == 2

    def test_never_serves_a_token_about_to_expire(self, fake_az: FakeAz, clock: FakeClock) -> None:
        """A short-lived token dies before its 80% mark; the 120s floor still holds."""
        fake_az.queue("get-access-token", stdout=token_payload("SHORT", expires_on=clock.now + 150))
        fake_az.add(
            "get-access-token", stdout=token_payload("RENEWED", expires_on=clock.now + 3600)
        )

        assert auth.token() == "SHORT"

        clock.advance(40)  # t+40: still before the refresh point at t+120 ...
        assert auth.token() == "RENEWED"  # ... but inside the 120s expiry margin
        assert len(fake_az.calls) == 2

    def test_cache_is_keyed_by_resource_and_subscription(
        self, fake_az: FakeAz, clock: FakeClock
    ) -> None:
        fake_az.add(ARM, stdout=token_payload("ARM", expires_on=clock.now + 3600))
        auth.token(resource=ARM)
        auth.token(resource=ARM, subscription="sub-1")
        auth.token(resource=ARM, subscription="sub-2")
        assert set(auth._CACHE) == {(ARM, None), (ARM, "sub-1"), (ARM, "sub-2")}
        assert len(fake_az.calls) == 3

    def test_clear_token_cache_forces_a_re_mint(self, fake_az: FakeAz, clock: FakeClock) -> None:
        fake_az.queue(
            "get-access-token", stdout=token_payload("FIRST", expires_on=clock.now + 3600)
        )
        fake_az.add("get-access-token", stdout=token_payload("SECOND", expires_on=clock.now + 3600))

        assert auth.token() == "FIRST"
        auth.clear_token_cache()
        assert auth._CACHE == {}
        assert auth.token() == "SECOND"
        assert len(fake_az.calls) == 2

    def test_cache_starts_empty_in_every_test(self) -> None:
        """The conftest fixture resets the in-process cache between tests."""
        assert auth._CACHE == {}


class TestExpiryParsing:
    NAIVE_LOCAL = "2026-01-01 12:00:00"

    @pytest.mark.parametrize(
        "payload, expected",
        [
            pytest.param({"expires_on": 1700000000}, 1700000000.0, id="epoch int"),
            pytest.param({"expires_on": 1700000000.5}, 1700000000.5, id="epoch float"),
            pytest.param({"expires_on": "1700000000"}, 1700000000.0, id="epoch string"),
            pytest.param({"expires_on": " 1700000000 "}, 1700000000.0, id="epoch string padded"),
            pytest.param(
                {"expiresOn": "2026-01-01T12:00:00Z"},
                datetime.fromisoformat("2026-01-01T12:00:00+00:00").timestamp(),
                id="iso utc with Z",
            ),
            pytest.param(
                {"expiresOn": "2026-01-01T12:00:00+00:00"},
                datetime.fromisoformat("2026-01-01T12:00:00+00:00").timestamp(),
                id="iso utc with offset",
            ),
            pytest.param(
                {"expiresOn": NAIVE_LOCAL},
                datetime.fromisoformat(NAIVE_LOCAL).astimezone().timestamp(),
                id="naive local string (older az)",
            ),
            pytest.param(
                {"expiresOn": "2026-01-01 12:00:00.123456"},
                datetime.fromisoformat("2026-01-01 12:00:00.123456").astimezone().timestamp(),
                id="naive local with microseconds",
            ),
            pytest.param(
                {"expires_on": 1700000000, "expiresOn": NAIVE_LOCAL},
                1700000000.0,
                id="epoch wins over the string",
            ),
            pytest.param(
                {"expires_on": True, "expiresOn": NAIVE_LOCAL},
                datetime.fromisoformat(NAIVE_LOCAL).astimezone().timestamp(),
                id="bool is not an epoch",
            ),
            pytest.param({}, None, id="nothing"),
            pytest.param({"expiresOn": ""}, None, id="empty string"),
            pytest.param({"expiresOn": "   "}, None, id="blank string"),
            pytest.param({"expiresOn": "not a date"}, None, id="unparseable"),
            pytest.param({"expiresOn": 1700000000}, None, id="expiresOn must be a string"),
            pytest.param({"expires_on": "later"}, None, id="non-numeric epoch"),
            pytest.param({"expires_on": None, "expiresOn": None}, None, id="explicit nulls"),
        ],
    )
    def test_expiry_epoch(self, payload: dict[str, Any], expected: float | None) -> None:
        assert auth._expiry_epoch(payload) == expected

    def test_epoch_form_drives_the_cache(self, fake_az: FakeAz, clock: FakeClock) -> None:
        fake_az.add("get-access-token", stdout=token_payload("TOK", expires_on=clock.now + 1000))
        auth.token()
        entry = auth._CACHE[(DATA, None)]
        assert entry.expires_at == clock.now + 1000
        assert entry.refresh_at == clock.now + 800

    def test_naive_local_form_drives_the_cache(self, fake_az: FakeAz, clock: FakeClock) -> None:
        expiry = datetime.fromtimestamp(clock.now + 1000).replace(microsecond=0)
        fake_az.add(
            "get-access-token",
            stdout=token_payload("TOK", expires_on_str=expiry.isoformat(sep=" ")),
        )
        auth.token()
        entry = auth._CACHE[(DATA, None)]
        assert entry.expires_at == pytest.approx(expiry.astimezone().timestamp())

    def test_unparseable_expiry_still_caches_with_a_thirty_minute_cadence(
        self, fake_az: FakeAz, clock: FakeClock
    ) -> None:
        fake_az.add("get-access-token", stdout=token_payload("TOK", expires_on_str="soon"))
        auth.token()
        entry = auth._CACHE[(DATA, None)]
        assert entry.expires_at is None
        assert entry.refresh_at == clock.now + 1800


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


class TestAccountAndSubscriptions:
    def test_account_argv_and_payload(self, fake_az: FakeAz) -> None:
        fake_az.add("show", stdout=account_json())
        assert auth.account() == {
            "id": "sub-1",
            "name": "Example",
            "tenantId": TENANT,
            "user": {"name": "dev@example.com"},
            "isDefault": True,
        }
        assert fake_az.argvs == [["az", "account", "show", "--output", "json"]]

    def test_account_returns_none_when_az_fails(self, fake_az: FakeAz) -> None:
        fake_az.default = (1, "", "ERROR: Please run 'az login'")
        assert auth.account() is None

    def test_account_returns_none_for_a_non_object_payload(self, fake_az: FakeAz) -> None:
        fake_az.default = (0, "[]", "")
        assert auth.account() is None

    def test_subscriptions_puts_the_default_first(self, fake_az: FakeAz) -> None:
        payload = (
            '[{"id": "b", "isDefault": false}, {"id": "a", "isDefault": true},'
            ' {"id": "c", "isDefault": false}]'
        )
        fake_az.add("list", stdout=payload)
        assert [s["id"] for s in auth.subscriptions()] == ["a", "b", "c"]
        assert fake_az.argvs == [["az", "account", "list", "--output", "json"]]
        assert fake_az.calls[0].kwargs["timeout"] == 90

    def test_subscriptions_drops_non_objects(self, fake_az: FakeAz) -> None:
        fake_az.add("list", stdout='[{"id": "a"}, "junk", null, 7]')
        assert auth.subscriptions() == [{"id": "a"}]

    def test_subscriptions_returns_empty_when_az_fails(self, fake_az: FakeAz) -> None:
        fake_az.default = (1, "", "ERROR: nope")
        assert auth.subscriptions() == []


class TestSignedIn:
    def test_requires_az_account_show(self, fake_az: FakeAz) -> None:
        fake_az.add("show", returncode=1, stderr="ERROR: Please run 'az login'")
        fake_az.add("get-access-token", stdout=live_token())
        assert auth.signed_in() is False
        assert fake_az.calls_matching("get-access-token") == []

    def test_requires_a_successful_mint_too(self, fake_az: FakeAz) -> None:
        """A stale refresh token still leaves ``az account show`` answering happily."""
        fake_az.add("show", stdout=account_json())
        fake_az.add("get-access-token", returncode=1, stderr="ERROR: refresh token expired")
        assert auth.signed_in() is False
        # Both data-plane audiences were tried before giving up.
        assert [c.args[3] for c in fake_az.calls_matching("get-access-token")] == [DATA, FALLBACK]

    def test_true_when_both_halves_work(self, fake_az: FakeAz) -> None:
        signed_in_az(fake_az)
        assert auth.signed_in() is True

    def test_a_non_object_account_payload_is_not_signed_in(self, fake_az: FakeAz) -> None:
        fake_az.add("show", stdout="null")
        assert auth.signed_in() is False


class TestLogin:
    @pytest.fixture
    def spy_login(self, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
        recorded: list[dict[str, Any]] = []
        monkeypatch.setattr(
            auth,
            "_interactive_login",
            lambda *, device_code: recorded.append({"device_code": device_code}),
        )
        return recorded

    def test_never_runs_az_login_over_a_working_session(
        self, fake_az: FakeAz, spy_login: list[dict[str, Any]]
    ) -> None:
        """SPEC section 3, in bold: check first, sign in only if there is nothing usable."""
        signed_in_az(fake_az)
        auth.login()
        assert spy_login == []

    def test_signs_in_when_there_is_no_session(
        self, fake_az: FakeAz, spy_login: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        states = iter([False, True])
        monkeypatch.setattr(auth, "signed_in", lambda: next(states))
        auth.login(device_code=True)
        assert spy_login == [{"device_code": True}]

    def test_a_login_that_produced_nothing_usable_raises(
        self, spy_login: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(auth, "signed_in", lambda: False)
        with pytest.raises(auth.AuthError) as excinfo:
            auth.login()
        assert "az login" in str(excinfo.value)
        assert "--tenant <tenant-id>" in str(excinfo.value)

    def test_an_invisible_subscription_raises(self, fake_az: FakeAz, spy_login: list) -> None:
        signed_in_az(fake_az)
        fake_az.add("list", stdout='[{"id": "sub-1", "name": "Example"}]')
        with pytest.raises(auth.AuthError) as excinfo:
            auth.login(subscription="sub-99")
        message = str(excinfo.value)
        assert "Subscription sub-99 is not visible to the signed-in account." in message
        assert "az account list -o table" in message

    @pytest.mark.parametrize(
        "wanted",
        [
            pytest.param("sub-1", id="by id"),
            pytest.param("SUB-1", id="by id, case-insensitively"),
            pytest.param("Example", id="by name"),
            pytest.param("  example  ", id="by name, trimmed"),
        ],
    )
    def test_a_visible_subscription_is_accepted(
        self, fake_az: FakeAz, spy_login: list, wanted: str
    ) -> None:
        signed_in_az(fake_az)
        fake_az.add("list", stdout='[{"id": "sub-1", "name": "Example"}]')
        auth.login(subscription=wanted)
        assert spy_login == []


# ---------------------------------------------------------------------------
# Resource API key
# ---------------------------------------------------------------------------

ENDPOINT = "https://my-resource.services.ai.azure.com"

ACCOUNT_LIST = [
    {
        "name": "other-resource",
        "resourceGroup": "rg-other",
        "properties": {"endpoint": "https://other-resource.services.ai.azure.com/"},
    },
    {
        "name": "my-resource",
        "resourceGroup": "rg-example",
        "id": "/subscriptions/s/resourceGroups/rg-example/providers/Microsoft.CognitiveServices/accounts/my-resource",
        "properties": {"customSubDomainName": "my-resource", "endpoint": ENDPOINT + "/"},
    },
]


class TestApiKey:
    def test_env_override_wins_outright(
        self, fake_az: FakeAz, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FOUNDRY_API_KEY", "  RESOURCE-KEY  ")
        assert auth.api_key(ENDPOINT) == "RESOURCE-KEY"
        assert fake_az.calls == []

    def test_exact_argv_for_the_two_arm_calls(self, fake_az: FakeAz) -> None:
        fake_az.add("keys", stdout='{"key1": "K1", "key2": "K2"}')
        fake_az.add("list", stdout=json.dumps(ACCOUNT_LIST))

        assert auth.api_key(ENDPOINT) == "K1"
        assert fake_az.argvs == [
            ["az", "cognitiveservices", "account", "list", "--output", "json"],
            [
                "az",
                "cognitiveservices",
                "account",
                "keys",
                "list",
                "--name",
                "my-resource",
                "--resource-group",
                "rg-example",
                "--output",
                "json",
            ],
        ]

    def test_subscription_scopes_both_calls(self, fake_az: FakeAz) -> None:
        fake_az.add("keys", stdout='{"key1": "K1"}')
        fake_az.add("list", stdout=json.dumps(ACCOUNT_LIST))

        assert auth.api_key(ENDPOINT, "sub-42") == "K1"
        for argv in fake_az.argvs:
            assert argv[-4:-2] == ["--subscription", "sub-42"]

    def test_key2_is_used_when_key1_is_absent(self, fake_az: FakeAz) -> None:
        fake_az.add("keys", stdout='{"key2": "K2"}')
        fake_az.add("list", stdout=json.dumps(ACCOUNT_LIST))
        assert auth.api_key(ENDPOINT) == "K2"

    def test_matches_by_published_endpoint_when_the_name_differs(self, fake_az: FakeAz) -> None:
        """The portal may publish the legacy host; it still names the same account."""
        listing = [
            {
                "name": "renamed-account",
                "resourceGroup": "rg-example",
                "properties": {"endpoint": "https://my-resource.openai.azure.com/"},
            }
        ]
        fake_az.add("keys", stdout='{"key1": "K1"}')
        fake_az.add("list", stdout=json.dumps(listing))
        assert auth.api_key(ENDPOINT) == "K1"
        assert fake_az.calls_matching("keys")[0].args[5] == "renamed-account"

    def test_matches_by_custom_subdomain(self, fake_az: FakeAz) -> None:
        listing = [
            {
                "name": "arm-name",
                "resourceGroup": "rg-example",
                "properties": {"customSubDomainName": "my-resource"},
            }
        ]
        fake_az.add("keys", stdout='{"key1": "K1"}')
        fake_az.add("list", stdout=json.dumps(listing))
        assert auth.api_key(ENDPOINT) == "K1"

    def test_resource_group_is_recovered_from_the_arm_id(self, fake_az: FakeAz) -> None:
        listing = [
            {
                "name": "my-resource",
                "id": "/subscriptions/s/resourceGroups/rg-from-id/providers/Microsoft.CognitiveServices/accounts/my-resource",
            }
        ]
        fake_az.add("keys", stdout='{"key1": "K1"}')
        fake_az.add("list", stdout=json.dumps(listing))
        assert auth.api_key(ENDPOINT) == "K1"
        assert fake_az.calls_matching("keys")[0].args[7] == "rg-from-id"

    @pytest.mark.parametrize(
        "listing",
        [
            pytest.param("[]", id="no accounts"),
            pytest.param('[{"name": "someone-else"}]', id="no match"),
            pytest.param('{"not": "a list"}', id="not a list"),
            pytest.param('[{"name": "my-resource"}]', id="match without a resource group"),
            pytest.param('[{"name": "my-resource", "resourceGroup": ""}]', id="blank group"),
        ],
    )
    def test_returns_none_rather_than_raising(self, fake_az: FakeAz, listing: str) -> None:
        fake_az.add("list", stdout=listing)
        assert auth.api_key(ENDPOINT) is None

    def test_returns_none_when_the_keys_call_fails(self, fake_az: FakeAz) -> None:
        fake_az.add("keys", returncode=1, stderr="ERROR: AuthorizationFailed")
        fake_az.add("list", stdout=json.dumps(ACCOUNT_LIST))
        assert auth.api_key(ENDPOINT) is None

    def test_an_unusable_endpoint_never_reaches_arm(self, fake_az: FakeAz) -> None:
        assert auth.api_key("_bad") is None
        assert fake_az.calls == []


# ---------------------------------------------------------------------------
# scrubbed_env
# ---------------------------------------------------------------------------

SP_ENV = {
    "AZURE_CLIENT_ID": "client-abc",
    "AZURE_CLIENT_SECRET": "secret-xyz",
    "AZURE_TENANT_ID": TENANT,
}
INNOCENT = {"PATH": "/usr/bin", "TERM": "xterm"}


class TestScrubbedEnv:
    def test_nothing_set_is_returned_untouched(self, fake_az: FakeAz) -> None:
        base = dict(INNOCENT)
        result = auth.scrubbed_env(base)
        assert result == INNOCENT
        assert result is not base  # a copy: the caller's dict is never mutated
        assert fake_az.calls == []  # no need to ask az anything

    def test_tenant_mismatch_drops_all_three(self, fake_az: FakeAz) -> None:
        fake_az.add("show", stdout=account_json(tenant=TENANT))
        base = {**INNOCENT, **SP_ENV, "AZURE_TENANT_ID": OTHER_TENANT}

        result = auth.scrubbed_env(base)

        assert result == INNOCENT
        assert set(auth.SP_ENV_VARS).isdisjoint(result)
        assert base["AZURE_CLIENT_ID"] == "client-abc"  # caller's dict untouched

    def test_tenant_match_with_a_working_sp_is_kept(
        self, fake_az: FakeAz, fake_urlopen: Any
    ) -> None:
        fake_az.add("show", stdout=account_json(tenant=TENANT))
        fake_urlopen.result = FakeResponse(status=200)

        result = auth.scrubbed_env({**INNOCENT, **SP_ENV})

        assert result == {**INNOCENT, **SP_ENV}
        assert len(fake_urlopen.requests) == 1

    def test_the_probe_is_the_request_environmentcredential_would_make(
        self, fake_az: FakeAz, fake_urlopen: Any
    ) -> None:
        fake_az.add("show", stdout=account_json(tenant=TENANT))
        fake_urlopen.result = FakeResponse(status=200)

        auth.scrubbed_env({**INNOCENT, **SP_ENV})

        request = fake_urlopen.requests[0]
        assert request.full_url == f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token"
        assert request.get_method() == "POST"
        assert request.headers["Content-type"] == "application/x-www-form-urlencoded"
        body = dict(pair.split("=", 1) for pair in request.data.decode().split("&"))
        assert body["grant_type"] == "client_credentials"
        assert body["client_id"] == "client-abc"
        assert body["client_secret"] == "secret-xyz"
        assert body["scope"] == "https%3A%2F%2Fai.azure.com%2F.default"

    def test_the_authority_host_is_configurable(
        self, fake_az: FakeAz, fake_urlopen: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AZURE_AUTHORITY_HOST", "https://login.microsoftonline.us/")
        fake_az.add("show", stdout=account_json(tenant=TENANT))
        fake_urlopen.result = FakeResponse(status=200)

        auth.scrubbed_env({**INNOCENT, **SP_ENV})

        assert fake_urlopen.urls == [f"https://login.microsoftonline.us/{TENANT}/oauth2/v2.0/token"]

    @pytest.mark.parametrize(
        "outcome",
        [
            pytest.param(urllib.error.URLError("no route to host"), id="network down"),
            pytest.param(
                urllib.error.HTTPError("url", 401, "Unauthorized", {}, None),  # type: ignore[arg-type]
                id="AADSTS7000222 expired secret",
            ),
            pytest.param(OSError("connection reset"), id="socket error"),
            pytest.param(FakeResponse(status=401), id="non-200 status"),
        ],
    )
    def test_tenant_match_with_a_failing_probe_drops_all_three(
        self, fake_az: FakeAz, fake_urlopen: Any, outcome: Any
    ) -> None:
        fake_az.add("show", stdout=account_json(tenant=TENANT))
        fake_urlopen.result = outcome

        result = auth.scrubbed_env({**INNOCENT, **SP_ENV})

        assert result == INNOCENT

    def test_a_lone_client_id_is_dropped(self, fake_az: FakeAz, fake_urlopen: Any) -> None:
        """A bare AZURE_CLIENT_ID also selects a user-assigned managed identity."""
        fake_az.add("show", stdout=account_json(tenant=TENANT))

        result = auth.scrubbed_env({**INNOCENT, "AZURE_CLIENT_ID": "client-abc"})

        assert result == INNOCENT
        assert fake_urlopen.requests == []  # nothing to probe with

    def test_a_lone_client_id_beside_a_matching_tenant_is_still_dropped(
        self, fake_az: FakeAz, fake_urlopen: Any
    ) -> None:
        fake_az.add("show", stdout=account_json(tenant=TENANT))

        result = auth.scrubbed_env(
            {**INNOCENT, "AZURE_CLIENT_ID": "client-abc", "AZURE_TENANT_ID": TENANT}
        )

        assert result == INNOCENT
        assert fake_urlopen.requests == []

    def test_a_lone_matching_tenant_id_is_harmless_and_kept(
        self, fake_az: FakeAz, fake_urlopen: Any
    ) -> None:
        fake_az.add("show", stdout=account_json(tenant=TENANT))

        result = auth.scrubbed_env({**INNOCENT, "AZURE_TENANT_ID": TENANT})

        assert result == {**INNOCENT, "AZURE_TENANT_ID": TENANT}
        assert fake_urlopen.requests == []

    def test_a_lone_mismatching_tenant_id_is_dropped(self, fake_az: FakeAz) -> None:
        fake_az.add("show", stdout=account_json(tenant=TENANT))
        assert auth.scrubbed_env({**INNOCENT, "AZURE_TENANT_ID": OTHER_TENANT}) == INNOCENT

    def test_a_secret_without_a_client_id_beside_a_matching_tenant_is_kept(
        self, fake_az: FakeAz, fake_urlopen: Any
    ) -> None:
        """Harmless: EnvironmentCredential cannot build a credential with no client id."""
        fake_az.add("show", stdout=account_json(tenant=TENANT))
        base = {**INNOCENT, "AZURE_CLIENT_SECRET": "secret-xyz", "AZURE_TENANT_ID": TENANT}
        assert auth.scrubbed_env(base) == base
        assert fake_urlopen.requests == []

    def test_a_secret_without_a_client_id_beside_a_foreign_tenant_is_dropped(
        self, fake_az: FakeAz
    ) -> None:
        fake_az.add("show", stdout=account_json(tenant=TENANT))
        base = {**INNOCENT, "AZURE_CLIENT_SECRET": "secret-xyz", "AZURE_TENANT_ID": OTHER_TENANT}
        assert auth.scrubbed_env(base) == INNOCENT

    def test_a_lone_secret_with_no_tenant_is_dropped(self, fake_az: FakeAz) -> None:
        fake_az.add("show", stdout=account_json(tenant=TENANT))
        base = {**INNOCENT, "AZURE_CLIENT_SECRET": "secret-xyz"}
        assert auth.scrubbed_env(base) == INNOCENT

    def test_unverifiable_identity_drops_the_sp(self, fake_az: FakeAz) -> None:
        """``az account show`` unavailable: dropping costs less than keeping a bad SP."""
        fake_az.add("show", returncode=1, stderr="ERROR: Please run 'az login'")
        assert auth.scrubbed_env({**INNOCENT, **SP_ENV}) == INNOCENT

    def test_an_account_with_no_tenant_id_drops_the_sp(self, fake_az: FakeAz) -> None:
        fake_az.add("show", stdout='{"id": "sub-1", "tenantId": ""}')
        assert auth.scrubbed_env({**INNOCENT, **SP_ENV}) == INNOCENT

    def test_tenant_comparison_is_case_insensitive(
        self, fake_az: FakeAz, fake_urlopen: Any
    ) -> None:
        fake_az.add("show", stdout=account_json(tenant=TENANT.upper()))
        fake_urlopen.result = FakeResponse(status=200)
        base = {**INNOCENT, **SP_ENV, "AZURE_TENANT_ID": TENANT.lower()}
        assert auth.scrubbed_env(base) == base

    def test_blank_values_do_not_count_as_present(self, fake_az: FakeAz) -> None:
        base = {**INNOCENT, "AZURE_CLIENT_ID": "   ", "AZURE_CLIENT_SECRET": ""}
        assert auth.scrubbed_env(base) == base
        assert fake_az.calls == []

    def test_the_probe_result_is_cached_per_credential(
        self, fake_az: FakeAz, fake_urlopen: Any
    ) -> None:
        fake_az.add("show", stdout=account_json(tenant=TENANT))
        fake_urlopen.result = FakeResponse(status=200)

        auth.scrubbed_env({**INNOCENT, **SP_ENV})
        auth.scrubbed_env({**INNOCENT, **SP_ENV})

        assert len(fake_urlopen.requests) == 1

    def test_a_different_secret_is_probed_again(self, fake_az: FakeAz, fake_urlopen: Any) -> None:
        fake_az.add("show", stdout=account_json(tenant=TENANT))
        fake_urlopen.result = FakeResponse(status=200)

        auth.scrubbed_env({**INNOCENT, **SP_ENV})
        auth.scrubbed_env({**INNOCENT, **SP_ENV, "AZURE_CLIENT_SECRET": "rotated"})

        assert len(fake_urlopen.requests) == 2

    def test_the_secret_itself_is_never_retained(self, fake_az: FakeAz, fake_urlopen: Any) -> None:
        fake_az.add("show", stdout=account_json(tenant=TENANT))
        fake_urlopen.result = FakeResponse(status=200)

        auth.scrubbed_env({**INNOCENT, **SP_ENV})

        assert "secret-xyz" not in repr(auth._SP_PROBE)

    def test_defaults_to_os_environ(self, fake_az: FakeAz, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_az.add("show", stdout=account_json(tenant=TENANT))
        monkeypatch.setenv("AZURE_CLIENT_ID", "client-abc")
        monkeypatch.setenv("AZURE_CLIENT_SECRET", "secret-xyz")
        monkeypatch.setenv("AZURE_TENANT_ID", OTHER_TENANT)
        monkeypatch.setenv("FOUNDRY_MARKER", "kept")

        result = auth.scrubbed_env()

        assert set(auth.SP_ENV_VARS).isdisjoint(result)
        assert result["FOUNDRY_MARKER"] == "kept"

    def test_sp_env_var_names(self) -> None:
        assert auth.SP_ENV_VARS == ("AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET", "AZURE_TENANT_ID")
