"""Query rewriting: pure helpers, HTTP degradation and API wiring (P1 I1).

No test touches the network: ``httpx.post`` is monkeypatched everywhere, and
the API-level cases stub the service so the contract (and the "disabled means
zero calls" guarantee) can be asserted without a real LLM.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.core.config import Settings, settings
from app.schemas.search import SearchRequest, SearchResponse, SearchRewriteInfo
from app.services import query_rewrite_service as rewrite


@pytest.fixture()
def rewrite_on(monkeypatch):
    """Enable rewriting with a fake endpoint for the duration of one test."""
    monkeypatch.setattr(settings, "query_rewrite_enabled", True)
    monkeypatch.setattr(settings, "query_rewrite_url", "https://llm.test/v1")
    monkeypatch.setattr(settings, "query_rewrite_model", "test-chat-model")
    monkeypatch.setattr(settings, "query_rewrite_api_key", "secret-key")
    monkeypatch.setattr(settings, "query_rewrite_timeout", 5.0)
    monkeypatch.setattr(settings, "query_rewrite_max_chars", 300)
    return settings


class FakeResponse:
    def __init__(self, status_code: int = 200, payload=None, raw: str | None = None):
        self.status_code = status_code
        self._payload = payload
        self._raw = raw

    def json(self):
        if self._raw is not None:
            raise json.JSONDecodeError("bad", self._raw, 0)
        return self._payload


def openai_body(content: str) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


# --------------------------------------------------------------------------- #
# needs_rewrite
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "query",
    [
        "自注意力机制的 Transformer 架构",
        "低功耗 SRAM 的读能耗优化",
        "時系列データの異常検知",
        "딥러닝 기반 검색",
        "mixed 中英文 query",
    ],
)
def test_needs_rewrite_for_cjk_queries(query: str) -> None:
    assert rewrite.needs_rewrite(query) is True


@pytest.mark.parametrize(
    "query",
    [
        "stochastic time-to-digital converter",
        "voltage scalable time-domain ADC with asynchronous pipeline",
        "e5-large kNn 1024",  # digits/punctuation only, no CJK
    ],
)
def test_needs_rewrite_is_false_for_ascii(query: str) -> None:
    assert rewrite.needs_rewrite(query) is False


@pytest.mark.parametrize("query", ["", "   ", "\n\t"])
def test_needs_rewrite_is_false_for_blank(query: str) -> None:
    assert rewrite.needs_rewrite(query) is False


def test_needs_rewrite_is_false_beyond_max_chars(monkeypatch) -> None:
    monkeypatch.setattr(settings, "query_rewrite_max_chars", 10)

    assert rewrite.needs_rewrite("中" * 10) is True   # exactly at the limit
    assert rewrite.needs_rewrite("中" * 11) is False  # one over


# --------------------------------------------------------------------------- #
# build_rewrite_messages
# --------------------------------------------------------------------------- #
def test_build_rewrite_messages_shape() -> None:
    messages = rewrite.build_rewrite_messages("  中文查询  ")

    assert [message["role"] for message in messages] == ["system", "user"]
    assert messages[1]["content"] == "中文查询"
    system = messages[0]["content"].lower()
    # The instruction must pin down language, output shape and no chatter.
    assert "english" in system
    assert "no quotes" in system
    assert "no explanation" in system
    assert "no numbering" in system
    assert "only" in system


# --------------------------------------------------------------------------- #
# clean_rewrite
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('"low power SRAM read energy"', "low power SRAM read energy"),
        ("'stochastic TDC'", "stochastic TDC"),
        ("\u201csmart quotes\u201d", "smart quotes"),
        ("  spaced out  ", "spaced out"),
        ("multi\nline\tanswer", "multi line answer"),
        ("", ""),
        ("   ", ""),
    ],
)
def test_clean_rewrite_normalizes(raw: str, expected: str) -> None:
    assert rewrite.clean_rewrite(raw) == expected


def test_clean_rewrite_truncates_to_max_chars(monkeypatch) -> None:
    monkeypatch.setattr(settings, "query_rewrite_max_chars", 12)

    assert rewrite.clean_rewrite("a" * 50) == "a" * 12


# --------------------------------------------------------------------------- #
# rewrite_query - success
# --------------------------------------------------------------------------- #
def test_rewrite_query_success(rewrite_on, monkeypatch) -> None:
    captured: dict = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured.update({"url": url, "json": json, "headers": headers, "timeout": timeout})
        return FakeResponse(200, openai_body('"stochastic time-to-digital converter"'))

    monkeypatch.setattr(rewrite.httpx, "post", fake_post)

    outcome = rewrite.rewrite_query("随机时间数字转换器")

    assert outcome.applied is True
    assert outcome.rewritten == "stochastic time-to-digital converter"
    assert outcome.original == "随机时间数字转换器"
    assert outcome.model == "test-chat-model"
    assert outcome.took_ms is not None and outcome.took_ms >= 0
    assert outcome.reason is None
    # The call went to the chat completions endpoint with the pinned params.
    assert captured["url"] == "https://llm.test/v1/chat/completions"
    assert captured["json"]["model"] == "test-chat-model"
    assert captured["json"]["temperature"] == 0
    assert captured["json"]["max_tokens"] == settings.query_rewrite_max_tokens
    assert captured["headers"]["Authorization"] == "Bearer secret-key"
    assert captured["timeout"] == 5.0


def test_rewrite_query_accepts_a_trailing_slash_url(rewrite_on, monkeypatch) -> None:
    seen: dict = {}
    monkeypatch.setattr(settings, "query_rewrite_url", "https://llm.test/v1/")

    def fake_post(url, json=None, headers=None, timeout=None):
        seen["url"] = url
        return FakeResponse(200, openai_body("english query"))

    monkeypatch.setattr(rewrite.httpx, "post", fake_post)

    assert rewrite.rewrite_query("中文").applied is True
    assert seen["url"] == "https://llm.test/v1/chat/completions"


# --------------------------------------------------------------------------- #
# rewrite_query - degradation
# --------------------------------------------------------------------------- #
def test_rewrite_query_degrades_on_transport_error(rewrite_on, monkeypatch) -> None:
    def boom(*args, **kwargs):
        raise rewrite.httpx.ConnectError("refused")

    monkeypatch.setattr(rewrite.httpx, "post", boom)

    outcome = rewrite.rewrite_query("中文查询")

    assert outcome.applied is False
    assert outcome.rewritten == "中文查询"
    assert outcome.reason == "ConnectError"


@pytest.mark.parametrize("status_code", [400, 401, 429, 500, 503])
def test_rewrite_query_degrades_on_non_200(rewrite_on, monkeypatch, status_code) -> None:
    monkeypatch.setattr(
        rewrite.httpx, "post", lambda *a, **k: FakeResponse(status_code, {})
    )

    outcome = rewrite.rewrite_query("中文查询")

    assert outcome.applied is False
    assert outcome.rewritten == "中文查询"
    assert outcome.reason == f"http {status_code}"


def test_rewrite_query_degrades_on_invalid_json(rewrite_on, monkeypatch) -> None:
    monkeypatch.setattr(
        rewrite.httpx, "post", lambda *a, **k: FakeResponse(200, None, raw="<html>")
    )

    outcome = rewrite.rewrite_query("中文查询")

    assert outcome.applied is False
    assert outcome.reason == "invalid json"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"choices": []},
        {"choices": [{}]},
        {"choices": [{"message": {}}]},
        {"choices": [{"message": {"content": ""}}]},
        {"choices": [{"message": {"content": "   "}}]},
        "not-a-dict",
    ],
)
def test_rewrite_query_degrades_without_content(rewrite_on, monkeypatch, payload) -> None:
    monkeypatch.setattr(rewrite.httpx, "post", lambda *a, **k: FakeResponse(200, payload))

    outcome = rewrite.rewrite_query("中文查询")

    assert outcome.applied is False
    assert outcome.rewritten == "中文查询"
    assert outcome.reason == "no choices"


def test_rewrite_query_degrades_when_cleaning_leaves_nothing(
    rewrite_on, monkeypatch
) -> None:
    monkeypatch.setattr(
        rewrite.httpx, "post", lambda *a, **k: FakeResponse(200, openai_body('""'))
    )

    outcome = rewrite.rewrite_query("中文查询")

    assert outcome.applied is False
    assert outcome.reason == "empty rewrite"


def test_rewrite_query_degrades_when_the_answer_is_unchanged(
    rewrite_on, monkeypatch
) -> None:
    monkeypatch.setattr(
        rewrite.httpx,
        "post",
        lambda *a, **k: FakeResponse(200, openai_body("中文查询")),
    )

    outcome = rewrite.rewrite_query("中文查询")

    assert outcome.applied is False
    assert outcome.rewritten == "中文查询"
    assert outcome.reason == "unchanged"


def test_rewrite_query_degrades_when_disabled(monkeypatch) -> None:
    def boom(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("httpx must not be called while rewriting is disabled")

    monkeypatch.setattr(settings, "query_rewrite_enabled", False)
    monkeypatch.setattr(rewrite.httpx, "post", boom)

    outcome = rewrite.rewrite_query("中文查询")

    assert outcome.applied is False
    assert outcome.reason == "rewrite disabled"


def test_rewrite_query_handles_an_empty_query(rewrite_on, monkeypatch) -> None:
    monkeypatch.setattr(
        rewrite.httpx,
        "post",
        lambda *a, **k: pytest.fail("no call for an empty query"),
    )

    outcome = rewrite.rewrite_query("   ")

    assert outcome.applied is False
    assert outcome.reason == "empty query"


def test_rewrite_query_accepts_a_gateway_text_field(rewrite_on, monkeypatch) -> None:
    monkeypatch.setattr(
        rewrite.httpx,
        "post",
        lambda *a, **k: FakeResponse(200, {"choices": [{"text": "english query"}]}),
    )

    assert rewrite.rewrite_query("中文").rewritten == "english query"


# --------------------------------------------------------------------------- #
# config validation
# --------------------------------------------------------------------------- #
def test_config_defaults_disable_rewriting() -> None:
    fresh = Settings(_env_file=None)

    assert fresh.query_rewrite_enabled is False
    assert fresh.query_rewrite_timeout == 10.0
    assert fresh.query_rewrite_max_chars == 300
    assert fresh.query_rewrite_target_language == "en"


@pytest.mark.parametrize(
    "missing",
    [
        {"query_rewrite_url": "", "query_rewrite_model": "m", "query_rewrite_api_key": "k"},
        {"query_rewrite_url": "http://x", "query_rewrite_model": "", "query_rewrite_api_key": "k"},
        {"query_rewrite_url": "http://x", "query_rewrite_model": "m", "query_rewrite_api_key": ""},
        {"query_rewrite_url": "", "query_rewrite_model": "", "query_rewrite_api_key": ""},
    ],
)
def test_enabling_rewrite_requires_url_model_and_key(missing: dict) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, query_rewrite_enabled=True, **missing)


def test_enabling_rewrite_with_complete_config_is_accepted() -> None:
    fresh = Settings(
        _env_file=None,
        query_rewrite_enabled=True,
        query_rewrite_url="https://llm.test/v1",
        query_rewrite_model="m",
        query_rewrite_api_key="k",
    )

    assert fresh.query_rewrite_enabled is True


def test_rewrite_max_chars_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, query_rewrite_max_chars=0)


# --------------------------------------------------------------------------- #
# API layer
# --------------------------------------------------------------------------- #
def test_api_gate_skips_the_llm_when_disabled(monkeypatch) -> None:
    from app.api import search as search_api

    def boom(query):  # pragma: no cover - must never run
        raise AssertionError("rewrite_query must not run while disabled")

    monkeypatch.setattr(settings, "query_rewrite_enabled", False)
    monkeypatch.setattr(search_api.query_rewrite_service, "rewrite_query", boom)

    outcome = search_api._maybe_rewrite("中文查询")

    assert outcome.applied is False
    assert outcome.rewritten == "中文查询"


def test_api_gate_skips_ascii_queries_even_when_enabled(monkeypatch) -> None:
    from app.api import search as search_api

    def boom(query):  # pragma: no cover - must never run
        raise AssertionError("ASCII queries must not be rewritten")

    monkeypatch.setattr(settings, "query_rewrite_enabled", True)
    monkeypatch.setattr(search_api.query_rewrite_service, "rewrite_query", boom)

    outcome = search_api._maybe_rewrite("stochastic time-to-digital converter")

    assert outcome.applied is False
    assert outcome.rewritten == "stochastic time-to-digital converter"


def test_api_gate_rewrites_cjk_queries_when_enabled(monkeypatch) -> None:
    from app.api import search as search_api

    called: list[str] = []

    def fake(query):
        called.append(query)
        return rewrite.RewriteOutcome(query, "english query", True, model="m", took_ms=7)

    monkeypatch.setattr(settings, "query_rewrite_enabled", True)
    monkeypatch.setattr(search_api.query_rewrite_service, "rewrite_query", fake)

    outcome = search_api._maybe_rewrite("中文查询")

    assert called == ["中文查询"]
    assert outcome.applied is True
    assert outcome.rewritten == "english query"
    assert outcome.took_ms == 7


def test_response_exposes_the_rewrite_contract() -> None:
    response = SearchResponse(query="中文查询", mode="hybrid", total=2, took_ms=12.0)

    payload = response.model_dump()

    # The caller always sees their own query; the rewrite is reported apart.
    assert payload["query"] == "中文查询"
    assert payload["rewritten_query"] is None
    assert payload["rewrite"] == {
        "enabled": False,
        "applied": False,
        "model": None,
        "took_ms": None,
    }


def test_response_reports_an_applied_rewrite() -> None:
    response = SearchResponse(
        query="中文查询",
        rewritten_query="english query",
        mode="hybrid",
        total=1,
        took_ms=9.0,
        rewrite=SearchRewriteInfo(
            enabled=True, applied=True, model="test-chat-model", took_ms=42
        ),
    )

    payload = response.model_dump()

    assert payload["query"] == "中文查询"
    assert payload["rewritten_query"] == "english query"
    assert payload["rewrite"]["applied"] is True
    assert payload["rewrite"]["took_ms"] == 42


def test_search_request_has_no_rewrite_switch() -> None:
    """The feature is server-side only: clients keep sending plain queries."""
    request = SearchRequest(query="中文查询")

    assert set(request.model_dump()) == {"query", "mode", "top_k", "filters", "rerank"}
