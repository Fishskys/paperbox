"""``GET /health``：五个依赖的探针，契约在 ``app.services.health_service``。

端点只负责渲染：每个依赖的判据（白名单状态码 + body 语义）、鉴权、超时、必需性都写在
那份注册表里。三种基本取值要分清楚：``ok`` / ``error`` / ``disabled``（配了才探；没配 = 没坏）。

这些用例走 ``httpx.MockTransport`` 注入，不连真实依赖；只有"连不上"那条故意打本机
关闭端口（``127.0.0.1:1``）来拿一个真实的连接错误。
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app
from app.services import health_service as hs

SERVICES = ("postgres", "opensearch", "minio", "embedding", "rerank", "docling")

#: A port nothing listens on: the probe must report an error, not raise.
CLOSED_PORT_URL = "http://127.0.0.1:1"


@pytest.fixture()
def client() -> TestClient:
    with TestClient(app) as test_client:
        yield test_client


def spec(name: str) -> hs.ProbeSpec:
    """The registry's spec for one dependency (targets read settings lazily)."""
    return next(item for item in hs.health_specs() if item.name == name)


def transport(
    status: int, *, payload: object | None = None, calls: list[str] | None = None
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(str(request.url))
        if payload is not None:
            return httpx.Response(status, json=payload)
        return httpx.Response(status, text="")

    return httpx.MockTransport(handler)


def run(name: str, mock: httpx.MockTransport) -> hs.ProbeResult:
    return asyncio.run(hs.run_probe(replace(spec(name), transport=mock)))


def test_health_reports_every_dependency(client: TestClient) -> None:
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert set(body["services"]) == set(SERVICES), "docling 与 rerank 都要在服务清单里"
    assert body["dependencies"]["rerank"]["required"] is False, "rerank 是可选项"


def test_docling_is_disabled_when_no_url_is_configured(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pypdf 部署下 DOCLING_URL 是空的：这是 disabled，不是 error。"""
    monkeypatch.setattr(settings, "docling_url", "", raising=False)
    body = client.get("/health").json()
    assert body["services"]["docling"] == "disabled"
    assert body["status"] == "ok", "没配的依赖不该让 /health 变脸"


def test_docling_is_probed_against_its_health_endpoint(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """配了 URL 就用 ``{DOCLING_URL}/health``，且必须是 200（404 说明端口上不是它）。"""
    monkeypatch.setattr(settings, "docling_url", "http://docling.test:8091/", raising=False)
    calls: list[str] = []

    result = run("docling", transport(200, payload={"status": "ok"}, calls=calls))

    assert result.status == hs.OK
    assert calls == ["http://docling.test:8091/health"], "末尾斜杠不能造成双斜杠"


def test_a_404_on_the_docling_port_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "docling_url", "http://docling.test:8091", raising=False)

    assert run("docling", transport(404)).status == hs.ERROR


def test_an_unreachable_docling_is_an_error_not_a_crash(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "docling_url", CLOSED_PORT_URL, raising=False)

    result = asyncio.run(hs.run_probe(spec("docling")))

    assert result.status == hs.ERROR
    assert "ConnectError" in result.reason


def test_a_401_from_a_dependency_is_an_error_not_an_answer(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """旧判据（``status < 500`` 即 ok）会把 401 报成健康；这是收紧后的回归。"""
    monkeypatch.setattr(settings, "opensearch_url", "http://search.test:9200", raising=False)

    result = run("opensearch", transport(401, payload={"error": "unauthorized"}))

    assert result.status == hs.ERROR
    assert "401" in result.reason


def test_a_red_cluster_is_an_error_even_with_a_200() -> None:
    """200 只说明进程在听；red 集群回答不了检索。"""
    result = run("opensearch", transport(200, payload={"status": "red"}))

    assert result.status == hs.ERROR


def test_a_failing_dependency_keeps_the_endpoint_at_200(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """探针失败只标那一项；/health 本身仍 200（它回答的是 API 自己的存活）。"""
    monkeypatch.setattr(settings, "docling_url", CLOSED_PORT_URL, raising=False)
    monkeypatch.setattr(settings, "embedding_url", CLOSED_PORT_URL, raising=False)

    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["services"]["docling"] == "error"
    assert body["services"]["embedding"] == "error"


def test_health_needs_no_credentials_and_leaks_nothing(client: TestClient) -> None:
    """免鉴权端点，且不能把 URL/密钥回显出来。"""
    os.environ.pop("PAPER_API_KEY", None)
    body = client.get("/health").json()
    assert "docling" in body["services"]
    blob = str(body)
    for secret in ("@", "password", "token"):
        assert secret not in blob.lower().replace("opensearch", "")


# --------------------------------------------------------------------------- #
# 输出增强：degraded + dependencies（用假注册表，不连真实依赖）
# --------------------------------------------------------------------------- #


def ok_spec(name: str) -> hs.ProbeSpec:
    return hs.ProbeSpec(
        name=name,
        target=lambda: "https://dep.test",
        path="/health",
        transport=transport(200, payload={"status": "ok"}),
        detail=f"{name} probe",
    )


def bad_spec(name: str, *, required: bool = True) -> hs.ProbeSpec:
    return hs.ProbeSpec(
        name=name,
        target=lambda: "https://dep.test",
        path="/health",
        transport=transport(503, payload={"status": "boom"}),
        required=required,
        detail=f"{name} probe",
    )


def disabled_spec(name: str) -> hs.ProbeSpec:
    return hs.ProbeSpec(name=name, target=lambda: "", path="/health")


def registry(monkeypatch: pytest.MonkeyPatch, *specs: hs.ProbeSpec) -> None:
    monkeypatch.setattr(hs, "health_specs", lambda: tuple(specs))


def test_a_failing_required_dependency_degrades_the_deployment(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry(monkeypatch, ok_spec("postgres"), bad_spec("opensearch"))

    body = client.get("/health").json()

    assert body["degraded"] is True
    assert body["status"] == "ok", "端点本身仍回答 API 活着"
    assert body["services"]["opensearch"] == "error"
    detail = body["dependencies"]["opensearch"]
    assert detail["status"] == "error"
    assert detail["required"] is True
    assert "503" in detail["reason"]
    assert detail["latency_ms"] is not None
    assert detail["checked_at"]
    assert detail["detail"] == "opensearch probe"
    assert body["dependencies"]["postgres"]["status"] == "ok"


def test_an_optional_dependency_failing_does_not_degrade(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry(monkeypatch, ok_spec("embedding"), bad_spec("rerank", required=False))

    body = client.get("/health").json()

    assert body["services"]["rerank"] == "error", "仍然如实报告"
    assert body["degraded"] is False, "但可选项不影响就绪判定"


def test_a_disabled_dependency_is_not_a_degradation(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry(monkeypatch, ok_spec("embedding"), disabled_spec("docling"))

    body = client.get("/health").json()

    assert body["services"]["docling"] == "disabled"
    assert body["degraded"] is False
    assert body["dependencies"]["docling"]["required"] is True


def test_the_report_never_echoes_the_credential(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """一个把刚拒绝的 key 回显在 body 里的服务，不能顺着 /health 漏出去。"""
    secret = "s3cr3t-value-0123456789"
    spec = hs.ProbeSpec(
        name="embedding",
        target=lambda: "https://api.example/v1",
        path="/embeddings",
        method="POST",
        json_body={"model": "m", "input": ["probe"]},
        auth=hs.AUTH_BEARER,
        token=lambda: secret,
        transport=transport(
            401, payload={"error": f"your key {secret} is invalid, see user:pw@host"}
        ),
    )
    registry(monkeypatch, spec)

    blob = client.get("/health").text

    assert secret not in blob
    assert "user:pw@" not in blob
    assert "401" in blob, "判据仍要说明失败原因"


def test_dependencies_do_not_leak_urls(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """detail 是人写的一句说明，不能顺手把内网地址写进去。"""
    registry(monkeypatch, bad_spec("opensearch"))

    detail = client.get("/health").json()["dependencies"]["opensearch"]

    assert "://" not in detail["detail"]
    assert "dep.test" not in detail["detail"]


# --------------------------------------------------------------------------- #
# 容器 healthcheck 的静态守卫（2026-10-14：从"看 200"改成"看 degraded"）
# --------------------------------------------------------------------------- #

COMPOSE = Path(__file__).resolve().parents[1] / "docker-compose.yml"


def test_the_container_healthcheck_gates_on_dependencies() -> None:
    """只看 200 的 healthcheck 在依赖全挂时照样报健康；必须解析 degraded。"""
    import yaml

    services = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]
    app_service = services["api"]
    test = app_service["healthcheck"]["test"]
    blob = "\n".join(test) if isinstance(test, list) else str(test)

    assert "degraded" in blob, "容器健康必须反映依赖，而不是只看 HTTP 200"
    assert "8077/health" in blob


def test_the_suite_pins_the_startup_probe_off() -> None:
    """用例绝不连真实依赖：这个开关被 conftest 钉住，防止有人顺手改回去。"""
    assert settings.health_probe_on_startup is False


def test_the_lifespan_probes_every_dependency_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """启动探一次（并且只探一次），之后完全按需 —— 没有定时器。"""
    monkeypatch.setattr(settings, "health_probe_on_startup", True, raising=False)
    calls: list[bool] = []

    async def fake_report(*, use_cache: bool = True):
        calls.append(use_cache)
        return ()

    monkeypatch.setattr(hs, "log_startup_report", fake_report)

    with TestClient(app):
        pass

    assert calls == [True], "启动应当恰好真探一次"


def test_the_lifespan_probe_is_off_when_the_switch_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "health_probe_on_startup", False, raising=False)
    calls: list[bool] = []

    async def fake_report(*, use_cache: bool = True):
        calls.append(use_cache)
        return ()

    monkeypatch.setattr(hs, "log_startup_report", fake_report)

    with TestClient(app):
        pass

    assert calls == []


def test_a_broken_probe_never_stops_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "health_probe_on_startup", True, raising=False)

    async def exploding(*, use_cache: bool = True):
        raise RuntimeError("probe machinery is broken")

    monkeypatch.setattr(hs, "log_startup_report", exploding)

    with TestClient(app) as test_client:
        assert test_client.get("/health").status_code == 200
