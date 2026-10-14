"""配置解析与默认值（``app/core/config.py``）。

健康探针的超时覆盖是「一个字符串解析成 dict」的老问题：写错必须立刻报错，否则一个
typo 会让某个依赖静默留在错的超时上，等到它真的慢了才发现。
"""

from __future__ import annotations

import pytest

from app.core.config import parse_timeout_overrides, settings
from app.services import health_service as hs


# --------------------------------------------------------------------------- #
# HEALTH_TIMEOUT_OVERRIDES
# --------------------------------------------------------------------------- #


def test_pairs_are_parsed_into_a_map() -> None:
    assert parse_timeout_overrides("docling=5,embedding=10") == {
        "docling": 5.0,
        "embedding": 10.0,
    }


def test_separators_and_whitespace_are_tolerated() -> None:
    assert parse_timeout_overrides(" docling = 5 ; embedding=10 , ") == {
        "docling": 5.0,
        "embedding": 10.0,
    }


@pytest.mark.parametrize("value", [None, "", "   ", ",,"])
def test_an_empty_value_is_an_empty_map(value: str | None) -> None:
    assert parse_timeout_overrides(value) == {}


@pytest.mark.parametrize("value", ["docling", "docling=x", "docling=0", "docling=-2", "=5"])
def test_a_malformed_entry_raises_instead_of_being_ignored(value: str) -> None:
    with pytest.raises(ValueError):
        parse_timeout_overrides(value)


def test_settings_exposes_the_parsed_map(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "health_timeout_overrides", "docling=5", raising=False)

    assert settings.health_timeout_map == {"docling": 5.0}


# --------------------------------------------------------------------------- #
# which timeout each probe ends up with
# --------------------------------------------------------------------------- #


def test_the_external_default_applies_to_probes_that_may_leave_the_machine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "health_timeout_overrides", "", raising=False)
    monkeypatch.setattr(settings, "health_http_timeout", 12.0, raising=False)

    assert hs.docling_spec().timeout_s == 12.0
    # docling runs on the NAS here; the internal services keep the short default.
    assert hs.embedding_spec().timeout_s == hs.LOCAL_PROBE_TIMEOUT_S
    assert hs.opensearch_spec().timeout_s == hs.LOCAL_PROBE_TIMEOUT_S


def test_an_override_wins_over_both_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        settings, "health_timeout_overrides", "docling=5,embedding=2.5", raising=False
    )
    monkeypatch.setattr(settings, "health_http_timeout", 12.0, raising=False)

    assert hs.docling_spec().timeout_s == 5.0
    assert hs.embedding_spec().timeout_s == 2.5
    assert hs.opensearch_spec().timeout_s == hs.LOCAL_PROBE_TIMEOUT_S


def test_probe_retries_come_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "health_probe_retries", 3, raising=False)

    assert hs.docling_spec().retries == 3
    assert hs.embedding_spec().retries == 3


def test_an_unknown_embedding_health_mode_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pydantic import ValidationError

    from app.core.config import Settings

    monkeypatch.setenv("EMBEDDING_HEALTH_MODE", "nope")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_the_embedding_health_mode_defaults_to_the_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.config import Settings

    monkeypatch.delenv("EMBEDDING_HEALTH_MODE", raising=False)
    assert Settings(_env_file=None).embedding_health_mode == "container"


def test_the_embedding_batch_default_matches_the_deployed_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """默认值必须与 .env / .env.example 一致，并且低于提供方的单请求上限。

    ``.env`` 与模板早就是 16，只有代码默认值还停在 32 —— 而 32 会直接撞上第三方
    embedding API（实测上限 25 条/请求）。
    """
    from app.core.config import Settings

    monkeypatch.delenv("EMBEDDING_BATCH_SIZE", raising=False)

    assert Settings(_env_file=None).embedding_batch_size == 16
