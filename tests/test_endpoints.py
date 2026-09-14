# SPDX-License-Identifier: Apache-2.0
"""Endpoint normalisation and route construction (SPEC section 2 and section 5).

Table-driven, because normalisation is a pure function over a fixed set of
input shapes and the only way to be sure every shape is covered is to list
them.
"""

from __future__ import annotations

import pytest

from foundry import endpoints

ROOT = "https://my-resource.services.ai.azure.com"

#: (label, input, expected canonical root).
ACCEPTED: list[tuple[str, str, str]] = [
    ("bare resource name", "my-resource", ROOT),
    ("bare name uppercase", "MY-RESOURCE", ROOT),
    ("bare name, digits", "res01", "https://res01.services.ai.azure.com"),
    ("canonical url", ROOT, ROOT),
    ("canonical url, trailing slash", ROOT + "/", ROOT),
    ("canonical url, many trailing slashes", ROOT + "///", ROOT),
    ("services.ai host, no scheme", "my-resource.services.ai.azure.com", ROOT),
    ("services.ai host, protocol-relative", "//my-resource.services.ai.azure.com", ROOT),
    ("openai.azure.com host", "https://my-resource.openai.azure.com", ROOT),
    ("openai.azure.com host, no scheme", "my-resource.openai.azure.com/", ROOT),
    ("cognitiveservices host", "https://my-resource.cognitiveservices.azure.com", ROOT),
    ("inference.ai host", "https://my-resource.inference.ai.azure.com", ROOT),
    ("api.cognitive.microsoft.com host", "https://my-resource.api.cognitive.microsoft.com", ROOT),
    ("http scheme is upgraded", "http://my-resource.services.ai.azure.com", ROOT),
    ("uppercase url", "HTTPS://MY-RESOURCE.SERVICES.AI.AZURE.COM", ROOT),
    ("mixed case host", "https://My-Resource.Services.AI.Azure.Com/", ROOT),
    ("explicit port", "https://my-resource.services.ai.azure.com:443", ROOT),
    ("explicit non-default port", "https://my-resource.services.ai.azure.com:8443/api", ROOT),
    ("project endpoint", f"{ROOT}/api/projects/my-project", ROOT),
    ("project endpoint, trailing slash", f"{ROOT}/api/projects/my-project/", ROOT),
    (
        "project endpoint on the openai host",
        "https://my-resource.openai.azure.com/api/projects/p",
        ROOT,
    ),
    ("query string", f"{ROOT}/?api-version=2024-10-21", ROOT),
    ("fragment", f"{ROOT}#models", ROOT),
    ("surrounding whitespace", f"  {ROOT}  ", ROOT),
    ("double-quoted (shell paste)", f'"{ROOT}"', ROOT),
    ("single-quoted (shell paste)", f"'{ROOT}'", ROOT),
    ("fully-qualified trailing dot", "my-resource.services.ai.azure.com.", ROOT),
    ("credentials in the url", "https://user:pass@my-resource.services.ai.azure.com", ROOT),
    (
        "unknown host kept verbatim",
        "https://foundry.contoso.local",
        "https://foundry.contoso.local",
    ),
    (
        "sovereign cloud host kept verbatim",
        "my-resource.services.ai.azure.us",
        "https://my-resource.services.ai.azure.us",
    ),
]

#: Inputs that cannot be an endpoint at all.
REJECTED: list[tuple[str, str]] = [
    ("empty", ""),
    ("whitespace only", "   "),
    ("quotes around nothing", '""'),
    ("bare slash", "/"),
    ("scheme only", "https://"),
    ("protocol-relative with no host", "//"),
    ("query only", "?"),
    ("fragment only", "#models"),
    ("leading underscore", "_bad"),
    ("leading hyphen", "-my-resource"),
    ("embedded space", "my resource"),
    ("underscore inside", "my_resource"),
    ("empty first label", "https://.services.ai.azure.com"),
    ("bare dot", "."),
]


class TestNormalize:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [pytest.param(v, e, id=label) for label, v, e in ACCEPTED],
    )
    def test_accepted_forms(self, value: str, expected: str) -> None:
        assert endpoints.normalize(value) == expected

    @pytest.mark.parametrize(
        ("value", "expected"),
        [pytest.param(v, e, id=label) for label, v, e in ACCEPTED],
    )
    def test_is_idempotent(self, value: str, expected: str) -> None:
        once = endpoints.normalize(value)
        assert endpoints.normalize(once) == once
        assert endpoints.normalize(endpoints.normalize(once)) == expected

    @pytest.mark.parametrize(
        ("value", "expected"),
        [pytest.param(v, e, id=label) for label, v, e in ACCEPTED],
    )
    def test_result_shape(self, value: str, expected: str) -> None:
        result = endpoints.normalize(value)
        assert result.startswith("https://")
        assert not result.endswith("/")
        assert result == result.lower()
        assert "/api/projects" not in result

    @pytest.mark.parametrize(
        "value",
        [pytest.param(v, id=label) for label, v in REJECTED],
    )
    def test_rejected_forms_raise(self, value: str) -> None:
        with pytest.raises(ValueError):
            endpoints.normalize(value)

    @pytest.mark.parametrize(
        "value",
        [pytest.param(v, id=label) for label, v in REJECTED],
    )
    def test_rejection_message_names_the_fix(self, value: str) -> None:
        """SPEC section 9: every failure names the concrete fix."""
        with pytest.raises(ValueError) as excinfo:
            endpoints.normalize(value)
        message = str(excinfo.value)
        assert "--endpoint my-resource" in message
        assert "--endpoint https://my-resource.services.ai.azure.com" in message
        assert message.startswith(("No Foundry endpoint given.", repr(value)))

    def test_empty_message_says_nothing_was_given(self) -> None:
        with pytest.raises(ValueError, match=r"^No Foundry endpoint given\."):
            endpoints.normalize("")

    def test_bare_junk_message_quotes_the_input(self) -> None:
        with pytest.raises(ValueError) as excinfo:
            endpoints.normalize("_bad")
        assert str(excinfo.value).startswith("'_bad' is not a Foundry endpoint or resource name.")

    def test_none_is_treated_as_missing(self) -> None:
        """``--endpoint`` is optional, so ``None`` must not raise ``AttributeError``."""
        with pytest.raises(ValueError, match="No Foundry endpoint given"):
            endpoints.normalize(None)  # type: ignore[arg-type]

    def test_every_alias_suffix_collapses_onto_the_resource_root(self) -> None:
        """Whatever host the portal showed, the same account normalises identically."""
        results = {
            endpoints.normalize(f"https://my-resource.{suffix}")
            for suffix in endpoints._ALIAS_SUFFIXES
        }
        assert results == {ROOT}

    def test_resource_suffix_constant(self) -> None:
        assert endpoints.RESOURCE_SUFFIX == "services.ai.azure.com"
        assert endpoints.RESOURCE_SUFFIX in endpoints._ALIAS_SUFFIXES

    def test_distinct_resources_stay_distinct(self) -> None:
        assert endpoints.normalize("alpha") != endpoints.normalize("beta")


class TestResourceName:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            pytest.param("my-resource", "my-resource", id="bare name"),
            pytest.param(ROOT, "my-resource", id="canonical url"),
            pytest.param("MY-RESOURCE", "my-resource", id="uppercase name"),
            pytest.param("https://my-resource.openai.azure.com/", "my-resource", id="alias host"),
            pytest.param(f"{ROOT}/api/projects/p", "my-resource", id="project endpoint"),
            pytest.param("https://my-resource.services.ai.azure.com:443", "my-resource", id="port"),
            pytest.param("https://foundry.contoso.local", "foundry", id="unknown host"),
        ],
    )
    def test_first_host_label(self, value: str, expected: str) -> None:
        assert endpoints.resource_name(value) == expected

    def test_rejects_what_normalize_rejects(self) -> None:
        with pytest.raises(ValueError):
            endpoints.resource_name("_bad")


class TestRoutes:
    """SPEC section 5: routes are built from the normalised resource root."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [pytest.param(v, e, id=label) for label, v, e in ACCEPTED],
    )
    def test_anthropic_base_is_root_plus_anthropic(self, value: str, expected: str) -> None:
        assert endpoints.anthropic_base(value) == f"{expected}/anthropic"

    @pytest.mark.parametrize(
        ("value", "expected"),
        [pytest.param(v, e, id=label) for label, v, e in ACCEPTED],
    )
    def test_openai_base_is_root_plus_openai_v1(self, value: str, expected: str) -> None:
        assert endpoints.openai_base(value) == f"{expected}/openai/v1"

    def test_exact_route_strings(self) -> None:
        assert endpoints.anthropic_base("my-resource") == (
            "https://my-resource.services.ai.azure.com/anthropic"
        )
        assert endpoints.openai_base("my-resource") == (
            "https://my-resource.services.ai.azure.com/openai/v1"
        )

    def test_routes_carry_no_api_version(self) -> None:
        """foundry-api-notes: no Foundry inference route takes ``api-version``."""
        for route in (endpoints.anthropic_base(ROOT), endpoints.openai_base(ROOT)):
            assert "api-version" not in route
            assert "?" not in route

    def test_routes_take_the_project_path_off_first(self) -> None:
        project = f"{ROOT}/api/projects/my-project"
        assert endpoints.anthropic_base(project) == f"{ROOT}/anthropic"
        assert endpoints.openai_base(project) == f"{ROOT}/openai/v1"

    def test_routes_never_double_a_slash(self) -> None:
        for value in (ROOT, ROOT + "/", ROOT + "///"):
            for route in (endpoints.anthropic_base(value), endpoints.openai_base(value)):
                assert "//" not in route.removeprefix("https://")

    def test_routes_reject_junk(self) -> None:
        for func in (endpoints.anthropic_base, endpoints.openai_base):
            with pytest.raises(ValueError, match="--endpoint"):
                func("")


class TestRejectsJunkThatLooksLikeANameOnceStripped:
    r"""Regression: the port/query/path strip used to run BEFORE the bare-name
    check, so `what?` became `what` and `C:\path` became `c` -- each then matched
    the resource-name pattern and was silently turned into a fabricated endpoint.
    """

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param("what?", id="query turns junk into a label"),
            pytest.param(r"C:\path\to\thing", id="drive letter looks like host:port"),
            pytest.param("localhost:8080", id="dotless host with a port"),
            pytest.param("my-resource:443", id="bare name with a port"),
            pytest.param("my-resource/extra", id="bare name with a path"),
            pytest.param("my-resource#frag", id="bare name with a fragment"),
        ],
    )
    def test_is_rejected_rather_than_fabricated(self, value: str) -> None:
        with pytest.raises(ValueError) as excinfo:
            endpoints.normalize(value)
        assert "--endpoint" in str(excinfo.value)

    def test_a_trailing_slash_is_still_fine_on_a_bare_name(self) -> None:
        assert endpoints.normalize("my-resource/") == ROOT
