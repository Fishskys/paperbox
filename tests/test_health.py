"""``GET /health``：五个依赖的轻量探针，含 docling。

2026-10-10 新增 docling：解析后端在 NAS 上（不在本机 docker 里），所以走 HTTP 探，
和容器 healthcheck 用同一个 ``/health`` 端点。三种取值要分清楚：
``ok`` / ``error`` / ``disabled``（配了才探；没配 = 没坏，不是 error）。
"""

from __future__ import annotations

import asyncio
import os

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api import health as health_module
from app.core.config import settings
from app.main import app

SERVICES = ("postgres", "opensearch", "minio", "embedding", "docling")


@pytest.fixture()
def client() -> TestClient:
    with TestClient(app) as test_client:
        yield test_client


def _stub(monkeypatch: pytest.MonkeyPatch, name: str, value: str) -> None:
    async def probe() -> str:
        return value

    monkeypatch.setattr(health_module, name, probe)


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
    seen: list[str] = []

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
            pass

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *exc) -> bool:  # noqa: ANN002
            return False

        async def get(self, url: str) -> httpx.Response:
            seen.append(url)
            return httpx.Response(200, request=httpx.Request("GET", url))

    monkeypatch.setattr(health_module.httpx, "AsyncClient", FakeClient)
    assert asyncio.run(health_module._check_docling()) == "ok"
    assert seen == ["http://docling.test:8091/health"], "末尾斜杠不能造成双斜杠"


def test_a_404_on_the_docling_port_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "docling_url", "http://docling.test:8091", raising=False)

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
            pass

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *exc) -> bool:  # noqa: ANN002
            return False

        async def get(self, url: str) -> httpx.Response:
            return httpx.Response(404, request=httpx.Request("GET", url))

    monkeypatch.setattr(health_module.httpx, "AsyncClient", FakeClient)
    assert asyncio.run(health_module._check_docling()) == "error"


def test_an_unreachable_docling_is_an_error_not_a_crash(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "docling_url", "http://127.0.0.1:1", raising=False)
    assert asyncio.run(health_module._check_docling()) == "error"


def test_a_failing_dependency_keeps_the_endpoint_at_200(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """探针失败只标那一项；/health 本身仍 200（它回答的是 API 自己的存活）。"""
    _stub(monkeypatch, "_check_docling", "error")
    _stub(monkeypatch, "_check_embedding", "error")
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
