"""Embedding health/info expose the configured model without downloading it."""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

SERVER_PATH = Path(__file__).resolve().parents[1] / "infra" / "embedding" / "server.py"

BUILTIN = "jinaai/jina-reranker-v2-base-multilingual"
CUSTOM = "temsa/mmarco-mMiniLMv2-L12-H384-v1-onnx-cpu-qint8"


class FakeModelSource:
    """Stand-in for ``fastembed.common.model_description.ModelSource``."""

    def __init__(self, hf=None, url=None, **_kwargs) -> None:
        if hf is None and url is None:
            raise ValueError("At least one source should be set")
        self.hf = hf
        self.url = url

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"ModelSource(hf={self.hf!r}, url={self.url!r})"


class FakeTextEmbedding:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def embed(self, texts):
        return [[0.0, 1.0] for _ in texts]


class FakeCrossEncoder:
    """Records registration and construction so the tests can assert on both."""

    supported = [{"model": BUILTIN}]
    registrations: list[dict] = []
    constructions: list[str] = []

    def __init__(self, *args, **kwargs) -> None:
        type(self).constructions.append(kwargs.get("model_name", args[0] if args else None))

    def rerank(self, query, documents):
        return [0.5 for _ in documents]

    @classmethod
    def list_supported_models(cls):
        return list(cls.supported)

    @classmethod
    def add_custom_model(cls, **kwargs):
        cls.registrations.append(kwargs)
        cls.supported.append({"model": kwargs["model"]})


def load_server(monkeypatch: pytest.MonkeyPatch, **env: str):
    """Import ``infra/embedding/server.py`` with ``fastembed`` stubbed."""
    fastembed = types.ModuleType("fastembed")
    fastembed.TextEmbedding = FakeTextEmbedding
    common = types.ModuleType("fastembed.common")
    model_description = types.ModuleType("fastembed.common.model_description")
    model_description.ModelSource = FakeModelSource
    rerank_module = types.ModuleType("fastembed.rerank")
    cross_encoder = types.ModuleType("fastembed.rerank.cross_encoder")
    cross_encoder.TextCrossEncoder = FakeCrossEncoder
    rerank_module.cross_encoder = cross_encoder

    for name, module in (
        ("fastembed", fastembed),
        ("fastembed.common", common),
        ("fastembed.common.model_description", model_description),
        ("fastembed.rerank", rerank_module),
        ("fastembed.rerank.cross_encoder", cross_encoder),
    ):
        monkeypatch.setitem(sys.modules, name, module)

    monkeypatch.delenv("RERANK_MODEL", raising=False)
    monkeypatch.delenv("RERANK_MODEL_FILE", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    spec = importlib.util.spec_from_file_location("paperbox_embedding_server_rerank", SERVER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "paperbox_embedding_server_rerank", module)
    spec.loader.exec_module(module)
    return module


def test_health_and_info_expose_the_resolved_model_pair(monkeypatch) -> None:
    """The running container's model *and* file must be checkable without logs.

    The embedding dimension stays null here on purpose: the model was never
    loaded in this test, and the container reports measured values only.
    """
    server = load_server(monkeypatch, RERANK_MODEL=CUSTOM, RERANK_MODEL_FILE="model.onnx")
    client = TestClient(server.app)

    for path in ("/health", "/info"):
        body = client.get(path).json()
        assert body["rerank_model"] == CUSTOM, path
        assert body["rerank_model_file"] == "model.onnx", path
        assert body["dimension"] is None, path
