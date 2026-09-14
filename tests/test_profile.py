# SPDX-License-Identifier: Apache-2.0
"""On-disk state: ``~/.foundry/config.json`` (SPEC section 8).

Every test runs against the ``tmp_path``-backed app directory installed by the
autouse ``isolate`` fixture, so nothing here can reach a developer's real
``~/.foundry``.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import pytest
from conftest import collect_strings, read_config

from foundry import profile
from foundry.profile import Profile, ProfileError

ENDPOINT = "https://my-resource.services.ai.azure.com"


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


class TestPaths:
    def test_app_dir_honours_foundry_home(self, isolate: Path) -> None:
        assert profile.app_dir() == isolate

    def test_app_dir_is_absolute(self) -> None:
        assert profile.app_dir().is_absolute()

    def test_app_dir_does_not_create_anything(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``foundry --version`` must not create directories."""
        target = tmp_path / "never-created"
        monkeypatch.setenv(profile.HOME_ENV, str(target))
        assert profile.app_dir() == target
        assert not target.exists()

    def test_app_dir_expands_a_tilde(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(profile.HOME_ENV, "~/somewhere")
        assert profile.app_dir() == Path(os.path.abspath(Path.home() / "somewhere"))

    def test_app_dir_falls_back_to_dot_foundry_under_home(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(profile.HOME_ENV, raising=False)
        assert profile.app_dir() == Path(os.path.abspath(Path.home() / ".foundry"))

    def test_app_dir_relative_override_is_absolutised(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(profile.HOME_ENV, "relative-dir")
        result = profile.app_dir()
        assert result.is_absolute()
        assert result.name == "relative-dir"

    def test_config_path(self, isolate: Path) -> None:
        assert profile.config_path() == isolate / "config.json"
        assert profile.CONFIG_NAME == "config.json"

    def test_backups_dir_is_created_on_demand(self, isolate: Path) -> None:
        assert not (isolate / "backups").exists()
        assert profile.backups_dir() == isolate / "backups"
        assert (isolate / "backups").is_dir()

    def test_unwritable_app_dir_raises_a_named_fix(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        monkeypatch.setenv(profile.HOME_ENV, str(blocker / "foundry"))

        with pytest.raises(ProfileError) as excinfo:
            profile.backups_dir()
        assert "FOUNDRY_HOME" in str(excinfo.value)


class TestAgentHome:
    def test_is_absolute_under_the_app_dir_and_created(self, isolate: Path) -> None:
        home = profile.agent_home("claude")
        assert home == isolate / "agents" / "claude"
        assert home.is_absolute()
        assert home.is_dir()

    def test_creates_intermediate_directories(self, isolate: Path) -> None:
        assert not (isolate / "agents").exists()
        profile.agent_home("codex")
        assert (isolate / "agents").is_dir()

    def test_is_idempotent(self, isolate: Path) -> None:
        first = profile.agent_home("claude")
        (first / "settings.json").write_text("{}", encoding="utf-8")
        second = profile.agent_home("claude")
        assert second == first
        assert (second / "settings.json").read_text(encoding="utf-8") == "{}"

    @pytest.mark.parametrize(
        "tool",
        ["claude", "codex", "copilot", "opencode", "pi", "a.b", "a_b", "a-b", "A1", "0"],
    )
    def test_accepted_names(self, isolate: Path, tool: str) -> None:
        assert profile.agent_home(tool).parent == isolate / "agents"

    @pytest.mark.parametrize(
        "tool",
        [
            pytest.param("", id="empty"),
            pytest.param(".", id="dot"),
            pytest.param("..", id="parent"),
            pytest.param("../evil", id="traversal"),
            pytest.param("a/b", id="posix separator"),
            pytest.param("a\\b", id="windows separator"),
            pytest.param(".hidden", id="leading dot"),
            pytest.param("-lead", id="leading hyphen"),
            pytest.param("_lead", id="leading underscore"),
            pytest.param("a b", id="space"),
            pytest.param("C:agent", id="drive letter"),
            pytest.param(None, id="none"),
            pytest.param(5, id="not a string"),
        ],
    )
    def test_rejected_names(self, tool: Any) -> None:
        with pytest.raises(ValueError) as excinfo:
            profile.agent_home(tool)
        assert "~/.foundry/agents" in str(excinfo.value)

    def test_a_rejected_name_creates_nothing(self, isolate: Path) -> None:
        with pytest.raises(ValueError):
            profile.agent_home("../evil")
        assert not (isolate / "agents").exists()


# ---------------------------------------------------------------------------
# Round trip
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_save_then_load_returns_an_equal_profile(self, make_profile: Any) -> None:
        original = make_profile()
        profile.save(original)
        loaded = profile.load()
        assert loaded == original

    def test_written_json_is_exact(self, make_profile: Any, isolate: Path) -> None:
        profile.save(make_profile())
        data = read_config(isolate)
        assert data == {
            "version": 1,
            "endpoint": ENDPOINT,
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

    def test_version_is_written_first(self, make_profile: Any, isolate: Path) -> None:
        profile.save(make_profile())
        assert list(read_config(isolate)) == [
            "version",
            "endpoint",
            "subscription",
            "resource_group",
            "account",
            "deployments",
            "agents",
        ]

    def test_file_formatting(self, make_profile: Any, isolate: Path) -> None:
        profile.save(make_profile())
        text = (isolate / "config.json").read_text(encoding="utf-8")
        assert text.endswith("}\n")
        assert '\n  "endpoint": ' in text  # indent=2
        assert "\r\n" not in text  # newline="\n", even on Windows

    def test_non_ascii_survives_verbatim(self, make_profile: Any, isolate: Path) -> None:
        profile.save(make_profile(resource_group="rg-diretório-padrão"))
        text = (isolate / "config.json").read_text(encoding="utf-8")
        assert "rg-diretório-padrão" in text  # ensure_ascii=False
        assert "\\u" not in text
        assert profile.load().resource_group == "rg-diretório-padrão"

    def test_save_creates_the_app_dir(self, make_profile: Any, isolate: Path) -> None:
        assert not isolate.exists()
        profile.save(make_profile())
        assert (isolate / "config.json").is_file()

    def test_save_overwrites_a_previous_profile_entirely(
        self, make_profile: Any, isolate: Path
    ) -> None:
        profile.save(make_profile())
        profile.save(make_profile(endpoint="https://other.services.ai.azure.com", deployments={}))
        data = read_config(isolate)
        assert data["endpoint"] == "https://other.services.ai.azure.com"
        assert data["deployments"] == {}

    def test_agents_round_trip(self, make_profile: Any) -> None:
        prof = make_profile()
        profile.record_agent(prof, "claude", home="/tmp/x", owns=["settings.json"])
        profile.save(prof)
        loaded = profile.load()
        assert loaded.agents["claude"]["owns"] == ["settings.json"]
        assert loaded.is_configured("claude") is True


# ---------------------------------------------------------------------------
# Atomic write
# ---------------------------------------------------------------------------


def _temp_leftovers(app: Path) -> list[Path]:
    return sorted(app.glob(".config-*"))


class TestAtomicWrite:
    def test_the_swap_is_a_same_directory_os_replace(
        self, make_profile: Any, isolate: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[tuple[str, str, str]] = []
        real_replace = os.replace

        def spy(src: Any, dst: Any) -> None:
            # The temp file must already hold the complete new document, or the
            # swap would publish a half-written config.
            seen.append((str(src), str(dst), Path(src).read_text(encoding="utf-8")))
            real_replace(src, dst)

        monkeypatch.setattr(profile.os, "replace", spy)
        profile.save(make_profile())

        assert len(seen) == 1
        src, dst, body = seen[0]
        assert Path(src).parent == Path(dst).parent == isolate
        assert Path(dst) == isolate / "config.json"
        assert json.loads(body)["endpoint"] == ENDPOINT

    def test_a_failed_swap_leaves_the_existing_file_intact(
        self, make_profile: Any, isolate: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        profile.save(make_profile())
        before = (isolate / "config.json").read_bytes()

        def boom(src: Any, dst: Any) -> None:
            raise OSError(13, "Permission denied")

        monkeypatch.setattr(profile.os, "replace", boom)
        with pytest.raises(ProfileError) as excinfo:
            profile.save(make_profile(endpoint="https://other.services.ai.azure.com"))

        assert (isolate / "config.json").read_bytes() == before
        assert profile.load().endpoint == ENDPOINT
        assert "Permission denied" in str(excinfo.value)
        assert profile.HOME_ENV in str(excinfo.value)
        assert _temp_leftovers(isolate) == []

    def test_an_interrupted_write_does_not_truncate_the_existing_file(
        self, make_profile: Any, isolate: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Ctrl-C during a configure must leave the old config or the new, never half."""
        profile.save(make_profile())
        before = (isolate / "config.json").read_bytes()

        def interrupted(src: Any, dst: Any) -> None:
            raise KeyboardInterrupt

        monkeypatch.setattr(profile.os, "replace", interrupted)
        with pytest.raises(KeyboardInterrupt):
            profile.save(make_profile(account="half-written"))

        assert (isolate / "config.json").read_bytes() == before
        assert json.loads(before)["account"] == "my-resource"
        assert _temp_leftovers(isolate) == []

    def test_a_failed_first_write_creates_no_config_at_all(
        self, make_profile: Any, isolate: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            profile.os, "replace", lambda src, dst: (_ for _ in ()).throw(OSError(28, "No space"))
        )
        with pytest.raises(ProfileError):
            profile.save(make_profile())

        assert not (isolate / "config.json").exists()
        assert _temp_leftovers(isolate) == []
        assert profile.load() is None

    def test_the_temp_file_is_removed_on_success(self, make_profile: Any, isolate: Path) -> None:
        profile.save(make_profile())
        assert _temp_leftovers(isolate) == []
        assert sorted(p.name for p in isolate.iterdir()) == ["config.json"]


# ---------------------------------------------------------------------------
# Loading: no migration
# ---------------------------------------------------------------------------


class TestLoad:
    def test_missing_file_is_none(self) -> None:
        assert profile.load() is None

    @pytest.mark.parametrize(
        "body",
        [
            pytest.param("", id="empty file"),
            pytest.param("   ", id="whitespace"),
            pytest.param("{not json", id="malformed json"),
            pytest.param("[]", id="array, not an object"),
            pytest.param('"a string"', id="string, not an object"),
            pytest.param("null", id="null"),
            pytest.param("{}", id="no version"),
            pytest.param('{"version": 1}', id="no endpoint"),
            pytest.param('{"version": 1, "endpoint": ""}', id="blank endpoint"),
            pytest.param('{"version": 1, "endpoint": 7}', id="non-string endpoint"),
            pytest.param('{"version": 1, "endpoint": null}', id="null endpoint"),
        ],
    )
    def test_unusable_files_are_none(self, written_config: Any, body: str) -> None:
        written_config(body)
        assert profile.load() is None

    @pytest.mark.parametrize(
        "version",
        [
            pytest.param(0, id="zero"),
            pytest.param(2, id="next version"),
            pytest.param(999, id="far future"),
            pytest.param("1", id="stringified"),
            pytest.param(None, id="null"),
            pytest.param([1], id="list"),
        ],
    )
    def test_an_unrecognised_version_is_discarded(self, written_config: Any, version: Any) -> None:
        written_config({"version": version, "endpoint": ENDPOINT, "account": "my-resource"})
        assert profile.load() is None

    def test_a_discarded_file_is_left_on_disk_for_the_next_save(self, written_config: Any) -> None:
        """load() does not delete; the next save() replaces the bytes."""
        path = written_config({"version": 999, "endpoint": ENDPOINT})
        assert profile.load() is None
        assert path.exists()

    def test_an_unrecognised_version_is_rebuilt_not_migrated(
        self, written_config: Any, make_profile: Any, isolate: Path
    ) -> None:
        written_config(
            {
                "version": 999,
                "endpoint": "https://legacy.services.ai.azure.com",
                "legacy_field": "carried over?",
                "deployments": {"anthropic": {"opus": "legacy-opus"}},
                "agents": {"claude": {"configured_at": "2020-01-01T00:00:00Z"}},
            }
        )
        assert profile.load() is None

        profile.save(make_profile())
        data = read_config(isolate)

        assert data["version"] == profile.VERSION == 1
        assert "legacy_field" not in data
        assert data["endpoint"] == ENDPOINT
        assert data["agents"] == {}
        assert data["deployments"]["anthropic"]["opus"] == "claude-opus-4-7"

    def test_missing_optional_fields_become_empty_strings(self, written_config: Any) -> None:
        written_config({"version": 1, "endpoint": ENDPOINT})
        loaded = profile.load()
        assert loaded.endpoint == ENDPOINT
        assert loaded.subscription == ""
        assert loaded.resource_group == ""
        assert loaded.account == ""
        assert loaded.deployments == {}
        assert loaded.agents == {}

    @pytest.mark.parametrize("bad", [7, None, "text", []])
    def test_non_string_scalars_become_empty_strings(self, written_config: Any, bad: Any) -> None:
        written_config({"version": 1, "endpoint": ENDPOINT, "subscription": bad})
        expected = bad if isinstance(bad, str) else ""
        assert profile.load().subscription == expected

    @pytest.mark.parametrize("bad", ["text", 7, None, []])
    def test_a_non_object_deployments_block_becomes_empty(
        self, written_config: Any, bad: Any
    ) -> None:
        written_config({"version": 1, "endpoint": ENDPOINT, "deployments": bad, "agents": bad})
        loaded = profile.load()
        assert loaded.deployments == {}
        assert loaded.agents == {}

    def test_an_unreadable_file_is_none(self, isolate: Path) -> None:
        isolate.mkdir(parents=True, exist_ok=True)
        (isolate / "config.json").write_bytes(b"\xff\xfe\x00\x00 not utf-8 \xc3\x28")
        assert profile.load() is None

    def test_a_directory_where_the_config_should_be_is_none(self, isolate: Path) -> None:
        (isolate / "config.json").mkdir(parents=True)
        assert profile.load() is None


# ---------------------------------------------------------------------------
# No secrets on disk
# ---------------------------------------------------------------------------

SECRET_VALUE = "SECRET-VALUE-MUST-NOT-REACH-DISK"

SECRET_KEYS = [
    "apikey",
    "apiKey",
    "api_key",
    "API_KEY",
    "authorization",
    "Authorization",
    "bearer",
    "credential",
    "credentials",
    "key",
    "token",
    "access_token",
    "ANTHROPIC_AUTH_TOKEN",
    "refreshToken",
    "secret",
    "client_secret",
    "AZURE_CLIENT_SECRET",
    "password",
    "PASSWORD",
    "passwd",
]


class TestNoSecretsPersisted:
    def test_every_credential_shaped_key_is_stripped(
        self, make_profile: Any, isolate: Path
    ) -> None:
        agents = {"claude": dict.fromkeys(SECRET_KEYS, SECRET_VALUE)}
        agents["claude"]["configured_at"] = "2026-01-01T00:00:00Z"
        agents["claude"]["owns"] = ["settings.json"]

        profile.save(make_profile(agents=agents))

        written = read_config(isolate)["agents"]["claude"]
        assert written == {"configured_at": "2026-01-01T00:00:00Z", "owns": ["settings.json"]}

    @pytest.mark.parametrize("name", SECRET_KEYS)
    def test_each_key_individually(self, make_profile: Any, isolate: Path, name: str) -> None:
        profile.save(make_profile(agents={"claude": {name: SECRET_VALUE, "home": "/x"}}))
        written = read_config(isolate)["agents"]["claude"]
        assert name not in written
        assert written == {"home": "/x"}

    def test_the_secret_value_appears_nowhere_in_the_file(
        self, make_profile: Any, isolate: Path
    ) -> None:
        prof = make_profile(
            agents={
                "claude": {"api_key": SECRET_VALUE, "env": {"ANTHROPIC_AUTH_TOKEN": SECRET_VALUE}},
                "codex": {"steps": [{"bearer": SECRET_VALUE}, {"note": "fine"}]},
            }
        )
        profile.save(prof)

        text = (isolate / "config.json").read_text(encoding="utf-8")
        assert SECRET_VALUE not in text
        assert SECRET_VALUE not in collect_strings(read_config(isolate))

    def test_scrubbing_reaches_inside_lists(self, make_profile: Any, isolate: Path) -> None:
        prof = make_profile(agents={"codex": {"steps": [{"token": SECRET_VALUE, "id": 1}]}})
        profile.save(prof)
        assert read_config(isolate)["agents"]["codex"]["steps"] == [{"id": 1}]

    def test_scrubbing_reaches_the_deployments_block(
        self, make_profile: Any, isolate: Path
    ) -> None:
        prof = make_profile(deployments={"anthropic": {"opus": "o", "key": SECRET_VALUE}})
        profile.save(prof)
        assert read_config(isolate)["deployments"]["anthropic"] == {"opus": "o"}

    def test_innocent_lookalikes_survive(self, make_profile: Any, isolate: Path) -> None:
        """Only exact ``key`` is a secret; a deployment named ``keyring`` is data."""
        prof = make_profile(agents={"claude": {"keyring": "gnome", "monkey": "no", "home": "/x"}})
        profile.save(prof)
        assert read_config(isolate)["agents"]["claude"] == {
            "keyring": "gnome",
            "monkey": "no",
            "home": "/x",
        }

    def test_save_does_not_mutate_the_in_memory_profile(self, make_profile: Any) -> None:
        prof = make_profile(agents={"claude": {"api_key": SECRET_VALUE}})
        profile.save(prof)
        assert prof.agents["claude"]["api_key"] == SECRET_VALUE

    def test_a_full_configure_writes_no_token(self, make_profile: Any, isolate: Path) -> None:
        prof = make_profile()
        profile.record_agent(
            prof, "claude", home=profile.agent_home("claude"), owns=["settings.json"]
        )
        profile.save(prof)

        strings = collect_strings(read_config(isolate))
        assert not [s for s in strings if s.startswith("eyJ")]  # no JWT
        assert not [s for s in strings if "token" in s.lower()]


# ---------------------------------------------------------------------------
# Bookkeeping
# ---------------------------------------------------------------------------


class TestRecordAgent:
    def test_minimal_entry_is_just_a_timestamp(self, make_profile: Any) -> None:
        prof = make_profile()
        profile.record_agent(prof, "pi")
        assert set(prof.agents["pi"]) == {"configured_at"}
        assert re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", prof.agents["pi"]["configured_at"]
        )

    def test_home_is_absolutised(self, make_profile: Any, isolate: Path) -> None:
        prof = make_profile()
        profile.record_agent(prof, "claude", home=isolate / "agents" / "claude")
        recorded = Path(prof.agents["claude"]["home"])
        assert recorded.is_absolute()
        assert recorded == isolate / "agents" / "claude"

    def test_a_tilde_home_is_expanded(self, make_profile: Any) -> None:
        prof = make_profile()
        profile.record_agent(prof, "claude", home="~/.foundry/agents/claude")
        recorded = Path(prof.agents["claude"]["home"])
        assert recorded.is_absolute()
        assert "~" not in str(recorded)

    def test_a_relative_home_is_absolutised(self, make_profile: Any) -> None:
        prof = make_profile()
        profile.record_agent(prof, "claude", home="agents/claude")
        assert Path(prof.agents["claude"]["home"]).is_absolute()

    def test_owns_is_copied_not_aliased(self, make_profile: Any) -> None:
        prof = make_profile()
        owned = ["settings.json"]
        profile.record_agent(prof, "claude", owns=owned)
        owned.append("mutated-afterwards")
        assert prof.agents["claude"]["owns"] == ["settings.json"]

    def test_an_empty_owns_list_is_still_recorded(self, make_profile: Any) -> None:
        prof = make_profile()
        profile.record_agent(prof, "claude", owns=[])
        assert prof.agents["claude"]["owns"] == []

    def test_re_recording_replaces_the_entry(self, make_profile: Any) -> None:
        prof = make_profile()
        profile.record_agent(prof, "claude", owns=["a"], home="/x")
        profile.record_agent(prof, "claude", owns=["b"])
        assert prof.agents["claude"]["owns"] == ["b"]
        assert "home" not in prof.agents["claude"]

    def test_forget_agent_removes_the_entry(self, make_profile: Any) -> None:
        prof = make_profile()
        profile.record_agent(prof, "claude")
        profile.forget_agent(prof, "claude")
        assert prof.agents == {}
        assert prof.is_configured("claude") is False

    def test_forget_an_unknown_agent_is_a_no_op(self, make_profile: Any) -> None:
        prof = make_profile()
        profile.forget_agent(prof, "nothing-here")
        assert prof.agents == {}

    def test_now_iso_is_utc_second_resolution(self) -> None:
        stamp = profile.now_iso()
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", stamp)


class TestBackup:
    def test_a_missing_source_returns_none(self, tmp_path: Path, isolate: Path) -> None:
        assert profile.backup(tmp_path / "absent.json") is None
        assert not (isolate / "backups").exists()

    def test_a_directory_source_returns_none(self, tmp_path: Path) -> None:
        assert profile.backup(tmp_path) is None

    def test_the_copy_is_byte_identical_and_named_for_the_source(
        self, tmp_path: Path, isolate: Path
    ) -> None:
        source = tmp_path / "settings.json"
        source.write_text('{"a": 1}\n', encoding="utf-8")

        destination = profile.backup(source)

        assert destination is not None
        assert destination.parent == isolate / "backups"
        assert re.fullmatch(r"settings\.json\.\d{8}T\d{6}Z\.bak", destination.name)
        assert destination.read_bytes() == source.read_bytes()
        assert source.exists()  # the original is copied, not moved


class TestClear:
    def _populate(self, isolate: Path, make_profile: Any) -> Path:
        profile.save(make_profile())
        profile.agent_home("claude").joinpath("settings.json").write_text("{}", encoding="utf-8")
        profile.agent_home("codex")
        backup_source = isolate / "source.txt"
        backup_source.write_text("original", encoding="utf-8")
        profile.backup(backup_source)
        return backup_source

    def test_removes_the_config_and_every_agent_home(
        self, isolate: Path, make_profile: Any
    ) -> None:
        self._populate(isolate, make_profile)
        profile.clear()
        assert not (isolate / "config.json").exists()
        assert not (isolate / "agents").exists()
        assert profile.load() is None

    def test_keeps_backups_by_default(self, isolate: Path, make_profile: Any) -> None:
        """Backups hold the only copy of a user file edited in place (SPEC section 6)."""
        self._populate(isolate, make_profile)
        backups = sorted(p.name for p in (isolate / "backups").iterdir())
        profile.clear()
        assert sorted(p.name for p in (isolate / "backups").iterdir()) == backups

    def test_full_teardown_removes_backups_too(self, isolate: Path, make_profile: Any) -> None:
        self._populate(isolate, make_profile)
        (isolate / "source.txt").unlink()
        profile.clear(keep_backups=False)
        assert not (isolate / "backups").exists()
        assert not isolate.exists()  # the root was empty, so it went too

    def test_full_teardown_keeps_a_root_that_holds_foreign_files(
        self, isolate: Path, make_profile: Any
    ) -> None:
        self._populate(isolate, make_profile)
        profile.clear(keep_backups=False)
        assert isolate.exists()
        assert (isolate / "source.txt").read_text(encoding="utf-8") == "original"

    def test_clearing_nothing_is_not_an_error(self, isolate: Path) -> None:
        profile.clear()
        profile.clear(keep_backups=False)
        assert not (isolate / "config.json").exists()


# ---------------------------------------------------------------------------
# Convenience readers
# ---------------------------------------------------------------------------


class TestProfileReaders:
    @pytest.mark.parametrize(
        "deployments, family, expected",
        [
            pytest.param({"anthropic": {"opus": "o"}}, "opus", "o", id="present"),
            pytest.param({"anthropic": {"opus": "o"}}, "sonnet", None, id="absent family"),
            pytest.param({"anthropic": {"opus": ""}}, "opus", None, id="blank name"),
            pytest.param({"anthropic": {"opus": 7}}, "opus", None, id="non-string name"),
            pytest.param({"anthropic": {"opus": None}}, "opus", None, id="null name"),
            pytest.param({"anthropic": []}, "opus", None, id="anthropic not an object"),
            pytest.param({}, "opus", None, id="no anthropic block"),
        ],
    )
    def test_anthropic(
        self, make_profile: Any, deployments: dict, family: str, expected: str | None
    ) -> None:
        assert make_profile(deployments=deployments).anthropic(family) == expected

    @pytest.mark.parametrize(
        "deployments, expected",
        [
            pytest.param({"openai": ["a", "b"]}, ["a", "b"], id="ordered"),
            pytest.param({"openai": ["a", "", None, 7, "b"]}, ["a", "b"], id="junk filtered"),
            pytest.param({"openai": {}}, [], id="not a list"),
            pytest.param({}, [], id="absent"),
        ],
    )
    def test_openai(self, make_profile: Any, deployments: dict, expected: list[str]) -> None:
        assert make_profile(deployments=deployments).openai() == expected

    @pytest.mark.parametrize(
        "deployments, expected",
        [
            pytest.param(
                {"anthropic": {"opus": "o", "sonnet": "s", "haiku": "h"}, "openai": ["g"]},
                ["o", "s", "h", "g"],
                id="anthropic first, in family order",
            ),
            pytest.param(
                {"anthropic": {"sonnet": "s"}, "openai": ["s", "g"]},
                ["s", "g"],
                id="duplicates collapsed",
            ),
            pytest.param(
                {"anthropic": {"opus": "o", "haiku": "o"}},
                ["o"],
                id="duplicate anthropic names collapsed",
            ),
            pytest.param({}, [], id="nothing published"),
        ],
    )
    def test_all_deployments(
        self, make_profile: Any, deployments: dict, expected: list[str]
    ) -> None:
        assert make_profile(deployments=deployments).all_deployments() == expected

    @pytest.mark.parametrize(
        "agents, tool, expected",
        [
            pytest.param({"claude": {}}, "claude", True, id="empty dict counts"),
            pytest.param({"claude": {"owns": []}}, "claude", True, id="recorded"),
            pytest.param({}, "claude", False, id="absent"),
            pytest.param({"claude": "yes"}, "claude", False, id="not a dict"),
            pytest.param({"claude": None}, "claude", False, id="null"),
        ],
    )
    def test_is_configured(
        self, make_profile: Any, agents: dict, tool: str, expected: bool
    ) -> None:
        assert make_profile(agents=agents).is_configured(tool) is expected

    def test_to_dict_matches_the_spec_example_shape(self, make_profile: Any) -> None:
        prof = make_profile(
            deployments={
                "anthropic": {"opus": "claude-opus-4-7", "sonnet": "claude-sonnet-4-6"},
                "openai": ["gpt-5.4-mini", "gpt-5.4-nano"],
            },
            agents={"claude": {"configured_at": "2026-01-01T00:00:00Z", "owns": ["x"]}},
        )
        assert prof.to_dict() == {
            "version": 1,
            "endpoint": ENDPOINT,
            "subscription": "11111111-2222-3333-4444-555555555555",
            "resource_group": "rg-example",
            "account": "my-resource",
            "deployments": {
                "anthropic": {"opus": "claude-opus-4-7", "sonnet": "claude-sonnet-4-6"},
                "openai": ["gpt-5.4-mini", "gpt-5.4-nano"],
            },
            "agents": {"claude": {"configured_at": "2026-01-01T00:00:00Z", "owns": ["x"]}},
        }

    def test_deployments_and_agents_default_to_independent_dicts(self) -> None:
        first = Profile(endpoint=ENDPOINT, subscription="", resource_group="", account="")
        second = Profile(endpoint=ENDPOINT, subscription="", resource_group="", account="")
        first.agents["claude"] = {}
        assert second.agents == {}


class TestVersionMustBeAnActualInteger:
    """Regression: `data.get("version") != VERSION` accepted anything numerically
    equal to 1. In Python `True == 1` and `1.0 == 1`, so a file written by some
    other tool -- or a corrupted one -- loaded as a valid v1 profile.
    """

    @pytest.mark.parametrize(
        "version",
        [
            pytest.param(True, id="bool True equals 1"),
            pytest.param(1.0, id="float 1.0 equals 1"),
            pytest.param("1", id="string"),
            pytest.param(None, id="null"),
        ],
    )
    def test_non_integer_version_is_discarded(self, version: object) -> None:
        payload = {
            "version": version,
            "endpoint": "https://my-resource.services.ai.azure.com",
            "subscription": "sub",
            "resource_group": "rg",
            "account": "my-resource",
            "deployments": {},
            "agents": {},
        }
        path = profile.app_dir() / "config.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
        assert profile.load() is None

    def test_the_integer_one_still_loads(self) -> None:
        payload = {
            "version": 1,
            "endpoint": "https://my-resource.services.ai.azure.com",
            "subscription": "sub",
            "resource_group": "rg",
            "account": "my-resource",
            "deployments": {},
            "agents": {},
        }
        path = profile.app_dir() / "config.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
        loaded = profile.load()
        assert loaded is not None
        assert loaded.endpoint == "https://my-resource.services.ai.azure.com"
