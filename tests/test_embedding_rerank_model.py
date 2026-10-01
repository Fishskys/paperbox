"""Embedding server: declaring cross-encoders fastembed doesn't ship (plan T-C2).

fastembed knows exactly six rerankers, and the int8 ONNX export we want to run is
not one of them, so it has to be declared through ``add_custom_model`` before the
first use. That declaration is the only thing between "swap the model" and "swap
the architecture": after it, loading and inference take the same ONNX Runtime path
as the built-in models. So the seams are pinned here -- built-ins are never
re-declared, a custom repo is declared exactly once (with the ONNX file path taken
from ``RERANK_MODEL_FILE``, because quantized exports usually put ``model.onnx`` at
the repo root instead of ``onnx/model.onnx``), and the pair the server actually
resolved is readable from ``/health`` and ``/info``.

Like ``test_embedding_server_queue.py`` this loads ``infra/embedding/server.py``
from disk with ``fastembed`` stubbed -- nothing here downloads a model.
"""

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


@pytest.fixture(autouse=True)
def _reset_fastembed_fakes():
    FakeCrossEncoder.supported = [{"model": BUILTIN}]
    FakeCrossEncoder.registrations = []
    FakeCrossEncoder.constructions = []
    yield
    FakeCrossEncoder.supported = [{"model": BUILTIN}]
    FakeCrossEncoder.registrations = []
    FakeCrossEncoder.constructions = []


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


def test_builtin_model_is_never_re_declared(monkeypatch) -> None:
    """Declaring a model fastembed already ships would shadow its metadata."""
    server = load_server(monkeypatch, RERANK_MODEL=BUILTIN)

    assert server.register_custom_reranker(BUILTIN, "onnx/model.onnx") is False
    assert FakeCrossEncoder.registrations == []


def test_custom_model_is_declared_with_repo_and_onnx_file(monkeypatch) -> None:
    """The quantized export lives at the repo root, so the file path must travel."""
    server = load_server(monkeypatch, RERANK_MODEL=CUSTOM, RERANK_MODEL_FILE="model.onnx")

    assert server.register_custom_reranker(CUSTOM, server.RERANK_MODEL_FILE) is True
    assert len(FakeCrossEncoder.registrations) == 1
    call = FakeCrossEncoder.registrations[0]
    assert call["model"] == CUSTOM
    assert call["sources"].hf == CUSTOM
    assert call["model_file"] == "model.onnx"


def test_rerank_model_file_defaults_to_the_onnx_subdirectory(monkeypatch) -> None:
    """Most HF exports use ``onnx/model.onnx``; only the odd one needs an override."""
    server = load_server(monkeypatch)
    assert server.RERANK_MODEL_FILE == "onnx/model.onnx"


def test_registration_happens_once_on_first_use(monkeypatch) -> None:
    """``get_reranker`` is the only entry point and must stay idempotent."""
    server = load_server(monkeypatch, RERANK_MODEL=CUSTOM, RERANK_MODEL_FILE="model.onnx")

    first = server.get_reranker()
    second = server.get_reranker()

    assert first is second
    assert len(FakeCrossEncoder.registrations) == 1
    assert FakeCrossEncoder.constructions == [CUSTOM]


def test_health_and_info_expose_the_resolved_model_pair(monkeypatch) -> None:
    """The running container's model *and* file must be checkable without logs."""
    server = load_server(monkeypatch, RERANK_MODEL=CUSTOM, RERANK_MODEL_FILE="model.onnx")
    client = TestClient(server.app)

    for path in ("/health", "/info"):
        body = client.get(path).json()
        assert body["rerank_model"] == CUSTOM, path
        assert body["rerank_model_file"] == "model.onnx", path
