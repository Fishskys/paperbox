"""The healthcheck CLI: one registry, one exit code.

The script must agree with ``GET /health`` by construction (it calls the same
probes -- a second implementation is exactly what used to drift), and its exit
code must mean "a *required* dependency is not answering": an optional one may
fail loudly without failing an operator's run.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

import httpx
import pytest

from app.services import health_service as hs
from scripts import healthcheck


def transport(status: int, *, payload: object | None = None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if payload is not None:
            return httpx.Response(status, json=payload)
        return httpx.Response(status, text="")

    return httpx.MockTransport(handler)


def spec(name: str, *, mock: httpx.MockTransport, required: bool = True) -> hs.ProbeSpec:
    return hs.ProbeSpec(
        name=name,
        kind=hs.KIND_HTTP,
        target=lambda: "https://dep.test",
        path="/health",
        ok_status=(200,),
        required=required,
        transport=mock,
        detail=f"{name} probe",
    )


@pytest.fixture(autouse=True)
def _no_cache_leak() -> None:
    hs.clear_probe_cache()
    yield
    hs.clear_probe_cache()


def test_every_required_dependency_ok_exits_zero(capsys: pytest.CaptureFixture) -> None:
    code = healthcheck.main(
        (spec("postgres", mock=transport(200), required=False),),
        with_extras=False,
    )

    out = capsys.readouterr().out
    assert code == 0
    assert "all required dependencies ok" in out
    assert "ok" in out


def test_a_failing_required_dependency_exits_non_zero(
    capsys: pytest.CaptureFixture,
) -> None:
    code = healthcheck.main(
        (
            spec("postgres", mock=transport(200)),
            spec("embedding", mock=transport(401, payload={"error": "no key"})),
        ),
        with_extras=False,
    )

    out = capsys.readouterr().out
    assert code == 1
    assert "degraded - embedding=error" in out
    assert "401" in out, "失败原因要打出来，否则运维还得回翻日志"


def test_an_optional_dependency_failing_does_not_fail_the_run(
    capsys: pytest.CaptureFixture,
) -> None:
    code = healthcheck.main(
        (
            spec("postgres", mock=transport(200)),
            spec("rerank", mock=transport(503), required=False),
        ),
        with_extras=False,
    )

    out = capsys.readouterr().out
    assert code == 0
    assert "rerank" in out and "error" in out, "可选项仍要如实报出来"
    assert "(optional)" in out


def test_a_disabled_dependency_does_not_fail_the_run(
    capsys: pytest.CaptureFixture,
) -> None:
    disabled = hs.ProbeSpec(name="docling", target=lambda: "", path="/health")

    code = healthcheck.main((disabled,), with_extras=False)

    out = capsys.readouterr().out
    assert code == 0
    assert "disabled" in out


def test_the_cli_probes_the_api_itself() -> None:
    """脚本与 /health 不同进程：API 进程活着这件事只能由脚本自己探。"""
    api = healthcheck.api_spec()

    assert api.name == "api"
    assert api.path == "/health"
    assert api.kind == hs.KIND_HTTP
    assert api.target().startswith("http://127.0.0.1:")


def test_the_default_registry_is_the_endpoint_registry(monkeypatch) -> None:
    """默认清单 = /health 的注册表 + api，没有第二份实现。"""
    sentinel = spec("postgres", mock=transport(200))
    monkeypatch.setattr(hs, "health_specs", lambda: (sentinel,))
    captured: list[tuple] = []

    async def fake_probe_all(specs=None, *, use_cache=True):
        captured.append(tuple(specs or ()))
        return ()

    monkeypatch.setattr(hs, "probe_all", fake_probe_all)

    healthcheck.main(with_extras=False)

    names = [item.name for item in captured[0]]
    assert names == ["postgres", "api"]


def test_the_report_marks_a_cached_verdict(capsys: pytest.CaptureFixture) -> None:
    spec_with_ttl = replace(
        spec("embedding", mock=transport(200)), cache_ttl_s=60.0
    )
    clock = {"now": 1_000.0}
    real_clock = hs._CLOCK
    hs._CLOCK = lambda: clock["now"]
    try:
        asyncio.run(hs.run_probe(spec_with_ttl))
        healthcheck.main((spec_with_ttl,), with_extras=False)
    finally:
        hs._CLOCK = real_clock

    out = capsys.readouterr().out
    assert "cached verdict" in out
