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

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app
from app.services import health_service as hs

SERVICES = ("postgres", "opensearch", "minio", "embedding", "docling")

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


def test_health_reports_all_five_dependencies(client: TestClient) -> None:
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert set(body["services"]) == set(SERVICES), "docling 必须出现在服务清单里"


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
