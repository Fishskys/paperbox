"""``app.services.health_service``: declarative probes with explicit verdicts.

The old endpoint judged a dependency by ``status < 500``; a 401 from a
third-party API, a 404 for a renamed path or a 429 all counted as healthy. These
tests pin the opposite: success is a whitelist, everything else is a verdict.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import httpx
import pytest

from app.core.config import settings
from app.services import health_service as hs


def transport(
    status: int,
    *,
    text: str | None = None,
    payload: object | None = None,
    calls: list[httpx.Request] | None = None,
    raise_error: Exception | None = None,
) -> httpx.MockTransport:
    """A transport that answers ``status`` and records the requests it saw."""

    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        if raise_error is not None:
            raise raise_error
        if payload is not None:
            return httpx.Response(status, json=payload)
        return httpx.Response(status, text=text or "")

    return httpx.MockTransport(handler)


def probe(**overrides) -> hs.ProbeSpec:
    base = dict(
        name="embedding",
        kind=hs.KIND_HTTP,
        target=lambda: "https://api.example/v1",
        path="/embeddings",
        method="POST",
        json_body={"model": "m", "input": ["probe"]},
    )
    base.update(overrides)
    return hs.ProbeSpec(**base)


def run(spec: hs.ProbeSpec) -> hs.ProbeResult:
    return asyncio.run(hs.run_probe(spec))


# 缓存隔离由 tests/conftest.py 的 autouse fixture 统一负责（每个依赖都带 TTL，
# 结论是进程级共享的，不能让一个用例喂给下一个）。


def fake_clock(monkeypatch: pytest.MonkeyPatch, start: float = 1_000.0) -> dict:
    clock = {"now": start}
    monkeypatch.setattr(hs, "_CLOCK", lambda: clock["now"])
    return clock


# --------------------------------------------------------------------------- #
# success is a whitelist, not "< 500"
# --------------------------------------------------------------------------- #


def test_a_200_with_the_expected_shape_is_ok() -> None:
    spec = probe(
        ok_when=lambda body: len(body["data"][0]["embedding"]) == 4,
        transport=transport(200, payload={"data": [{"embedding": [0.1, 0.2, 0.3, 0.4]}]}),
    )

    result = run(spec)

    assert result.status == hs.OK
    assert result.latency_ms is not None and result.latency_ms >= 0
    assert result.cached is False
    assert result.checked_at


def test_a_401_is_an_error_not_an_answer() -> None:
    """The regression this whole module exists for: a wrong key looked healthy."""
    spec = probe(
        transport=transport(401, text='{"error":{"message":"No API-key provided."}}')
    )

    result = run(spec)

    assert result.status == hs.ERROR
    assert "401" in result.reason


@pytest.mark.parametrize("status", [301, 403, 404, 429])
def test_every_non_whitelisted_status_is_an_error(status: int) -> None:
    result = run(probe(transport=transport(status, text="nope")))

    assert result.status == hs.ERROR
    assert str(status) in result.reason


def test_a_200_that_fails_the_body_check_is_an_error() -> None:
    spec = probe(
        ok_when=lambda body: len(body["data"][0]["embedding"]) == 1024,
        transport=transport(200, payload={"data": [{"embedding": [0.0] * 768}]}),
    )

    result = run(spec)

    assert result.status == hs.ERROR
    assert "criteria" in result.reason


def test_a_non_json_body_where_json_is_required_is_an_error() -> None:
    spec = probe(ok_when=lambda body: True, transport=transport(200, text="<html>"))

    result = run(spec)

    assert result.status == hs.ERROR
    assert "JSON" in result.reason


def test_only_the_configured_statuses_count() -> None:
    spec = probe(
        ok_status=(200, 201), transport=transport(201, payload={"ok": True})
    )

    assert run(spec).status == hs.OK


# --------------------------------------------------------------------------- #
# credentials
# --------------------------------------------------------------------------- #


def test_a_missing_token_is_an_error_and_no_request_is_sent() -> None:
    calls: list[httpx.Request] = []
    spec = probe(
        auth=hs.AUTH_BEARER,
        token=lambda: "   ",
        transport=transport(200, payload={}, calls=calls),
    )

    result = run(spec)

    assert result.status == hs.ERROR
    assert "credential" in result.reason
    assert calls == [], "a request that can only 401 must not be sent"


def test_a_bearer_token_is_sent_as_an_authorization_header() -> None:
    calls: list[httpx.Request] = []
    spec = probe(
        auth=hs.AUTH_BEARER,
        token=lambda: "secret-token",
        transport=transport(200, payload={"data": []}, calls=calls),
    )

    assert run(spec).status == hs.OK
    assert calls[0].headers["authorization"] == "Bearer secret-token"
    assert json.loads(calls[0].content) == {"model": "m", "input": ["probe"]}


# --------------------------------------------------------------------------- #
# transport failures, retries, unknown targets
# --------------------------------------------------------------------------- #


def test_a_connection_error_is_an_error_with_the_exception_named() -> None:
    spec = probe(transport=transport(200, raise_error=httpx.ConnectError("refused")))

    result = run(spec)

    assert result.status == hs.ERROR
    assert "ConnectError" in result.reason


def test_retries_are_attempts_on_top_of_the_first_and_default_to_none() -> None:
    calls: list[httpx.Request] = []
    failing = transport(200, raise_error=httpx.ConnectError("refused"), calls=calls)

    assert run(probe(transport=failing)).status == hs.ERROR
    assert len(calls) == 1

    calls.clear()
    assert run(probe(transport=failing, retries=1)).status == hs.ERROR
    assert len(calls) == 2


def test_an_empty_http_target_is_disabled_not_an_error() -> None:
    spec = probe(target=lambda: "   ", transport=transport(200))

    result = run(spec)

    assert result.status == hs.DISABLED
    assert result.status not in (hs.OK, hs.ERROR)
    assert result.healthy is True


def test_the_result_carries_the_spec_context() -> None:
    spec = probe(
        required=False,
        detail="OpenAI-compatible /embeddings",
        transport=transport(200, payload={"data": []}),
    )

    result = run(spec)

    assert result.name == "embedding"
    assert result.required is False
    assert result.detail == "OpenAI-compatible /embeddings"


# --------------------------------------------------------------------------- #
# the verdict cache (billable probes must not be re-bought every poll)
# --------------------------------------------------------------------------- #


def test_a_verdict_is_reused_within_its_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = fake_clock(monkeypatch)
    calls: list[httpx.Request] = []
    spec = probe(
        cache_ttl_s=60.0,
        transport=transport(200, payload={"data": []}, calls=calls),
    )

    first = run(spec)
    clock["now"] += 30
    second = run(spec)

    assert first.status == second.status == hs.OK
    assert first.cached is False and second.cached is True
    assert len(calls) == 1, "the second call must come from the cache"
    assert second.checked_at == first.checked_at, "the verdict keeps its own time"


def test_the_verdict_is_probed_again_once_the_ttl_passed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = fake_clock(monkeypatch)
    calls: list[httpx.Request] = []
    spec = probe(
        cache_ttl_s=60.0,
        transport=transport(200, payload={"data": []}, calls=calls),
    )

    run(spec)
    clock["now"] += 61
    fresh = run(spec)

    assert fresh.cached is False
    assert len(calls) == 2


def test_a_ttl_of_zero_never_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_clock(monkeypatch)
    calls: list[httpx.Request] = []
    spec = probe(transport=transport(200, payload={"data": []}, calls=calls))

    run(spec)
    run(spec)

    assert len(calls) == 2


def test_a_failing_verdict_is_cached_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """A broken (billable) endpoint must not be re-billed on every poll."""
    fake_clock(monkeypatch)
    calls: list[httpx.Request] = []
    spec = probe(cache_ttl_s=60.0, transport=transport(401, text="no", calls=calls))

    assert run(spec).status == hs.ERROR
    second = run(spec)

    assert second.status == hs.ERROR and second.cached is True
    assert len(calls) == 1


def test_use_cache_false_forces_a_real_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_clock(monkeypatch)
    calls: list[httpx.Request] = []
    spec = probe(
        cache_ttl_s=60.0,
        transport=transport(200, payload={"data": []}, calls=calls),
    )

    asyncio.run(hs.run_probe(spec))
    asyncio.run(hs.run_probe(spec, use_cache=False))

    assert len(calls) == 2


def test_each_dependency_keeps_its_own_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_clock(monkeypatch)
    calls: list[httpx.Request] = []
    mock = transport(200, payload={"data": []}, calls=calls)

    asyncio.run(hs.run_probe(probe(name="a", cache_ttl_s=60.0, transport=mock)))
    asyncio.run(hs.run_probe(probe(name="b", cache_ttl_s=60.0, transport=mock)))

    assert len(calls) == 2, "one dependency's verdict must not answer for another"


# --------------------------------------------------------------------------- #
# report helpers (degraded / payload)
# --------------------------------------------------------------------------- #


def test_degraded_ignores_disabled_and_optional(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_clock(monkeypatch)

    def verdict(name: str, status: str, *, required: bool = True) -> hs.ProbeResult:
        spec = probe(name=name, required=required)
        return hs.ProbeResult(name=name, status=status, required=required)

    assert hs.is_degraded([verdict("a", hs.OK), verdict("b", hs.DISABLED)]) is False
    assert hs.is_degraded([verdict("a", hs.OK), verdict("b", hs.ERROR, required=False)]) is False
    assert hs.is_degraded([verdict("a", hs.OK), verdict("b", hs.ERROR)]) is True
    assert hs.is_degraded([verdict("a", hs.UNKNOWN)]) is True, "还没结论不算健康"
    assert hs.is_degraded([]) is False


def test_a_payload_keeps_only_the_fields_that_mean_something() -> None:
    minimal = hs.ProbeResult(name="docling", status=hs.DISABLED, required=True)

    assert hs.dependency_payload(minimal) == {"status": "disabled", "required": True}

    full = hs.ProbeResult(
        name="embedding",
        status=hs.OK,
        reason="",
        latency_ms=12.5,
        checked_at="2026-10-14T00:00:00+00:00",
        cached=True,
        required=True,
        detail="container GET /health",
        meta={"dimension": 1024},
    )

    payload = hs.dependency_payload(full)
    assert payload["latency_ms"] == 12.5
    assert payload["cached"] is True
    assert payload["meta"] == {"dimension": 1024}
    assert "reason" not in payload, "空的 reason 不该出现"


def test_redaction_covers_a_dsn_and_an_echoed_key() -> None:
    assert "secret" not in hs.redact("postgresql://postgres:secret@127.0.0.1/db")
    assert hs.redact("postgresql://postgres:secret@127.0.0.1/db").startswith(
        "postgresql://***@"
    )
    assert "pw" not in hs.redact("key user:pw@host rejected")
    assert "abcd1234" not in hs.redact("token abcd1234 rejected", secret="abcd1234")


# --------------------------------------------------------------------------- #
# the embedding probe: container contract vs OpenAI-compatible API
# --------------------------------------------------------------------------- #


def embeddings_body(width: int) -> dict:
    return {
        "object": "list",
        "data": [{"object": "embedding", "index": 0, "embedding": [0.01] * width}],
        "usage": {"prompt_tokens": 4, "total_tokens": 4},
    }


def openai_settings(monkeypatch: pytest.MonkeyPatch, **overrides) -> None:
    values = {
        "embedding_health_mode": "openai_compatible",
        "embedding_url": "https://api.example/v1",
        "embedding_model": "embedding-model-x",
        "embedding_api_key": "k-1234567890",
        "embedding_dimension": 1024,
        "health_probe_cache_ttl": 0.0,
    }
    values.update(overrides)
    for key, value in values.items():
        monkeypatch.setattr(settings, key, value, raising=False)


def test_the_container_mode_keeps_its_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "embedding_health_mode", "container", raising=False)
    monkeypatch.setattr(settings, "embedding_url", "http://embed.test:8090", raising=False)

    spec = hs.embedding_spec()
    assert spec.path == "/health" and spec.method == "GET"

    loaded = run(replace(spec, transport=transport(200, payload={"status": "ok", "loaded": True})))
    assert loaded.status == hs.OK


def test_a_container_that_has_not_loaded_its_model_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """答得上话 ≠ 能干活：容器 load 完模型前不能算 ok，也不该算 error。"""
    monkeypatch.setattr(settings, "embedding_health_mode", "container", raising=False)

    result = run(
        replace(
            hs.embedding_spec(),
            transport=transport(200, payload={"status": "ok", "loaded": False}),
        )
    )

    assert result.status == hs.UNKNOWN
    assert hs.is_degraded([result]) is True, "unknown 不是健康"


def test_the_openai_mode_checks_the_vector_width(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    openai_settings(monkeypatch)
    calls: list[httpx.Request] = []

    result = run(
        replace(
            hs.embedding_spec(),
            transport=transport(200, payload=embeddings_body(1024), calls=calls),
        )
    )

    assert result.status == hs.OK
    request = calls[0]
    assert str(request.url) == "https://api.example/v1/embeddings"
    assert request.method == "POST"
    assert request.headers["authorization"] == "Bearer k-1234567890"
    assert json.loads(request.content) == {
        "model": "embedding-model-x",
        "input": [hs.PROBE_TEXT],
    }


def test_the_openai_mode_reports_a_rejected_key(monkeypatch: pytest.MonkeyPatch) -> None:
    openai_settings(monkeypatch)

    result = run(
        replace(hs.embedding_spec(), transport=transport(401, text="No API-key provided."))
    )

    assert result.status == hs.ERROR
    assert "401" in result.reason


def test_the_openai_mode_reports_a_model_of_the_wrong_width(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    openai_settings(monkeypatch)

    result = run(
        replace(hs.embedding_spec(), transport=transport(200, payload=embeddings_body(768)))
    )

    assert result.status == hs.ERROR
    assert "criteria" in result.reason


def test_the_openai_probe_fails_without_a_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    openai_settings(monkeypatch, embedding_api_key="")
    calls: list[httpx.Request] = []

    result = run(
        replace(
            hs.embedding_spec(),
            transport=transport(200, payload=embeddings_body(1024), calls=calls),
        )
    )

    assert result.status == hs.ERROR
    assert calls == []


def test_the_openai_probe_never_retries_and_caches_its_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    openai_settings(monkeypatch, health_probe_cache_ttl=60.0)
    fake_clock(monkeypatch)
    calls: list[httpx.Request] = []
    spec = replace(
        hs.embedding_spec(),
        transport=transport(200, payload=embeddings_body(1024), calls=calls),
    )

    assert spec.retries == 0, "计费调用不重试"
    assert spec.cache_ttl_s == 60.0, "计费调用必须带缓存 TTL"

    run(spec)
    second = run(spec)

    assert second.cached is True
    assert len(calls) == 1


def test_the_openai_probe_gets_the_configured_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    openai_settings(monkeypatch, health_http_timeout=15.0)

    assert hs.embedding_spec().timeout_s == 15.0


# --------------------------------------------------------------------------- #
# the rerank probe (optional dependency, same container as embedding)
# --------------------------------------------------------------------------- #


def test_rerank_is_disabled_when_the_feature_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "rerank_enabled", False, raising=False)

    result = asyncio.run(hs.run_probe(replace(hs.rerank_spec(), transport=transport(200))))

    assert result.status == hs.DISABLED
    assert result.required is False


def test_rerank_is_ok_when_the_model_is_there(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "rerank_enabled", True, raising=False)
    monkeypatch.setattr(settings, "rerank_url", "http://embed.test:8090", raising=False)

    result = asyncio.run(
        hs.run_probe(
            replace(
                hs.rerank_spec(),
                transport=transport(
                    200, payload={"status": "ok", "rerank_model": "m", "rerank_loaded": True}
                ),
            )
        )
    )

    assert result.status == hs.OK


def test_a_reranker_that_is_not_loaded_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "rerank_enabled", True, raising=False)

    result = asyncio.run(
        hs.run_probe(
            replace(
                hs.rerank_spec(),
                transport=transport(200, payload={"status": "ok", "rerank_loaded": False}),
            )
        )
    )

    assert result.status == hs.UNKNOWN
    assert hs.is_degraded([result]) is False, "可选项不影响 degraded"


def test_a_failing_rerank_does_not_degrade_the_deployment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "rerank_enabled", True, raising=False)

    result = asyncio.run(hs.run_probe(replace(hs.rerank_spec(), transport=transport(503))))

    assert result.status == hs.ERROR
    assert hs.is_degraded([result]) is False


# --------------------------------------------------------------------------- #
# one budget for every dependency (10s by default, overridable by name)
# --------------------------------------------------------------------------- #


def test_a_spec_without_a_timeout_takes_the_configured_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "health_http_timeout", 7.5, raising=False)
    spec = hs.ProbeSpec(name="api", target=lambda: "https://dep.test", path="/health")

    assert hs._effective_timeout(spec) == 7.5


def test_a_blocking_probe_is_cut_off_by_its_budget() -> None:
    """PG / 对象存储的检查跑在线程里；超预算时判 error，而不是让 /health 一直等。"""
    import time as _time

    def slow_check() -> None:
        _time.sleep(0.3)

    spec = hs.ProbeSpec(name="minio", kind=hs.KIND_S3, timeout_s=0.05)

    result = asyncio.run(hs._probe_blocking(spec, slow_check))

    assert result.status == hs.ERROR
    assert "timed out after 0.05s" in result.reason


# --------------------------------------------------------------------------- #
# startup: one probe while booting, then on demand -- never a timer
# --------------------------------------------------------------------------- #


def test_startup_lines_only_flag_required_problems() -> None:
    results = (
        hs.ProbeResult(name="postgres", status=hs.OK, latency_ms=12.3, required=True),
        hs.ProbeResult(name="rerank", status=hs.ERROR, reason="boom", required=False),
        hs.ProbeResult(name="docling", status=hs.DISABLED, required=True),
    )

    lines = hs.startup_report_lines(results)

    assert [problem for problem, _, _ in lines] == [False, False, False]
    assert lines[0][1] == "startup health: postgres=ok (12 ms)"
    assert lines[1][2]["reason"] == "boom"


def test_a_failing_dependency_at_startup_is_logged_not_raised(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """依赖没起来不该拦住进程：只留告警，/health 才是判定入口。"""

    async def fake_probe_all(*, use_cache: bool = True):
        assert use_cache is True, "启动这一次必须真探（缓存此时也是空的）"
        return (
            hs.ProbeResult(
                name="postgres",
                status=hs.ERROR,
                reason="OperationalError: could not connect",
                required=True,
            ),
            hs.ProbeResult(name="minio", status=hs.OK, latency_ms=9.0, required=True),
        )

    monkeypatch.setattr(hs, "probe_all", fake_probe_all)
    # INFO, not WARNING: the "no periodic probing" note and the degraded summary
    # are INFO lines -- they must be in the log too, not just the failures.
    with caplog.at_level("INFO"):
        results = asyncio.run(hs.log_startup_report())

    assert [result.status for result in results] == [hs.ERROR, hs.OK]
    assert "startup health: postgres=error" in caplog.text
    assert "could not connect" in caplog.text
    assert "degraded=postgres" in caplog.text
    assert "no periodic probing" in caplog.text
