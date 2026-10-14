"""配置解析与默认值（``app/core/config.py``）。

健康探针的超时覆盖是「一个字符串解析成 dict」的老问题：写错必须立刻报错，否则一个
typo 会让某个依赖静默留在错的超时上，等到它真的慢了才发现。
"""

from __future__ import annotations

import pytest

from app.core.config import Settings, parse_seconds_overrides, parse_timeout_overrides, settings
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


def test_every_probe_gets_the_same_default_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """超时不按"本机/外部"分档：每个依赖默认都是 HEALTH_HTTP_TIMEOUT。"""
    monkeypatch.setattr(settings, "health_timeout_overrides", "", raising=False)
    monkeypatch.setattr(settings, "health_http_timeout", 12.0, raising=False)

    for spec in hs.health_specs():
        assert spec.timeout_s == 12.0, spec.name


def test_an_override_wins_over_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        settings, "health_timeout_overrides", "docling=5,embedding=2.5", raising=False
    )
    monkeypatch.setattr(settings, "health_http_timeout", 12.0, raising=False)

    assert hs.docling_spec().timeout_s == 5.0
    assert hs.embedding_spec().timeout_s == 2.5
    assert hs.opensearch_spec().timeout_s == 12.0, "没被覆盖的仍用统一默认值"


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


# --------------------------------------------------------------------------- #
# 每个依赖的探针结论都有缓存时长（默认 5 分钟，可按依赖覆盖）
# --------------------------------------------------------------------------- #


def test_the_shipped_default_is_five_minutes() -> None:
    """诊断边界：默认 TTL 就是"新鲜度"—— 依赖挂了最多 5 分钟内会被 /health 复用旧结论。"""
    assert Settings(_env_file=None).health_probe_cache_ttl == 300.0


def test_every_probe_caches_its_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    """本地依赖也要缓存：批量上传时 /health 被反复调用，不该每次都重探整个栈。"""
    monkeypatch.setattr(settings, "health_cache_ttl_overrides", "", raising=False)
    monkeypatch.setattr(settings, "health_probe_cache_ttl", 300.0, raising=False)

    for spec in hs.health_specs():
        assert spec.cache_ttl_s == 300.0, spec.name


def test_a_cache_ttl_override_is_per_dependency(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        settings, "health_cache_ttl_overrides", "opensearch=30,embedding=900", raising=False
    )
    monkeypatch.setattr(settings, "health_probe_cache_ttl", 300.0, raising=False)

    assert hs.opensearch_spec().cache_ttl_s == 30.0
    assert hs.embedding_spec().cache_ttl_s == 900.0
    assert hs.postgres_spec().cache_ttl_s == 300.0, "没被覆盖的用统一默认值"


def test_a_malformed_cache_override_names_its_own_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """报错要说清是哪个配置键写错了 —— 超时与缓存用的是同一个解析器。"""
    monkeypatch.setattr(settings, "health_cache_ttl_overrides", "opensearch=x", raising=False)

    with pytest.raises(ValueError, match="HEALTH_CACHE_TTL_OVERRIDES"):
        settings.health_cache_ttl_map


def test_the_shared_parser_labels_the_env_var_it_was_given() -> None:
    with pytest.raises(ValueError, match="SOMETHING_ELSE"):
        parse_seconds_overrides("a=b", env="SOMETHING_ELSE")
