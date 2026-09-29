"""Embedding server: the inference queue in front of every ONNX call (plan T7.3).

``infra/embedding/server.py`` is deployed as its own container, not imported by
the app, so these tests load the file directly with ``fastembed`` stubbed out --
the queue is pure stdlib and must be verifiable without a model download. What is
pinned here: one inference at a time per worker, FIFO order, a bounded backlog
that answers 503 + Retry-After instead of queueing forever, and counters that
make the wait visible through ``/info``.
"""

from __future__ import annotations

import importlib.util
import sys
import threading
import time
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

SERVER_PATH = Path(__file__).resolve().parents[1] / "infra" / "embedding" / "server.py"


class FakeTextEmbedding:
    def __init__(self, *args, **kwargs) -> None:
        self.batches: list[list[str]] = []

    def embed(self, texts):
        self.batches.append(list(texts))
        return [[0.0, 1.0] for _ in texts]


class FakeCrossEncoder:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def rerank(self, query, documents):
        return [0.5 for _ in documents]


def load_server(monkeypatch: pytest.MonkeyPatch, *, workers: int = 1, depth: int = 32):
    """Import ``infra/embedding/server.py`` with ``fastembed`` stubbed."""
    fastembed = types.ModuleType("fastembed")
    fastembed.TextEmbedding = FakeTextEmbedding
    rerank_module = types.ModuleType("fastembed.rerank")
    cross_encoder = types.ModuleType("fastembed.rerank.cross_encoder")
    cross_encoder.TextCrossEncoder = FakeCrossEncoder
    rerank_module.cross_encoder = cross_encoder

    monkeypatch.setitem(sys.modules, "fastembed", fastembed)
    monkeypatch.setitem(sys.modules, "fastembed.rerank", rerank_module)
    monkeypatch.setitem(sys.modules, "fastembed.rerank.cross_encoder", cross_encoder)
    monkeypatch.setenv("INFERENCE_WORKERS", str(workers))
    monkeypatch.setenv("INFERENCE_QUEUE_DEPTH", str(depth))

    spec = importlib.util.spec_from_file_location("paperbox_embedding_server", SERVER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "paperbox_embedding_server", module)
    spec.loader.exec_module(module)
    return module


def test_queue_runs_one_inference_at_a_time(monkeypatch) -> None:
    server = load_server(monkeypatch, workers=1)
    state = {"running": 0, "peak": 0}
    lock = threading.Lock()

    def inference() -> str:
        with lock:
            state["running"] += 1
            state["peak"] = max(state["peak"], state["running"])
        time.sleep(0.05)
        with lock:
            state["running"] -= 1
        return "done"

    results: list[str] = []
    threads = [
        threading.Thread(target=lambda: results.append(server.QUEUE.submit(inference)))
        for _ in range(4)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results == ["done"] * 4
    assert state["peak"] == 1, "workers=1 must serialize"
    assert server.QUEUE.stats()["completed"] == 4


def test_queue_is_fifo(monkeypatch) -> None:
    server = load_server(monkeypatch, workers=1, depth=8)
    order: list[int] = []
    gate = threading.Event()

    def blocker() -> None:
        gate.wait(2)

    first = threading.Thread(target=lambda: server.QUEUE.submit(blocker))
    first.start()
    while server.QUEUE.stats()["running"] == 0:
        time.sleep(0.01)

    threads = [
        threading.Thread(target=lambda index=index: server.QUEUE.submit(order.append, index))
        for index in range(3)
    ]
    for thread in threads:
        thread.start()
        time.sleep(0.01)  # keep the submission order deterministic
    gate.set()
    first.join()
    for thread in threads:
        thread.join()

    assert order == [0, 1, 2]


def test_a_full_queue_is_rejected_instead_of_waiting(monkeypatch) -> None:
    server = load_server(monkeypatch, workers=1, depth=1)
    gate = threading.Event()
    released = threading.Event()

    def blocker() -> None:
        gate.wait(2)
        released.set()

    running = threading.Thread(target=lambda: server.QUEUE.submit(blocker))
    running.start()
    while server.QUEUE.stats()["running"] == 0:
        time.sleep(0.01)

    queued = threading.Thread(target=lambda: server.QUEUE.submit(lambda: None))
    queued.start()
    while server.QUEUE.stats()["waiting"] == 0:
        time.sleep(0.01)

    with pytest.raises(server.QueueFull):
        server.QUEUE.submit(lambda: None)
    assert server.QUEUE.stats()["rejected"] == 1

    gate.set()
    running.join()
    queued.join()
    assert released.is_set()


def test_errors_from_inference_reach_the_caller(monkeypatch) -> None:
    server = load_server(monkeypatch)

    def boom() -> None:
        raise RuntimeError("onnx exploded")

    with pytest.raises(RuntimeError, match="onnx exploded"):
        server.QUEUE.submit(boom)
    # a failed call still frees the worker
    assert server.QUEUE.submit(lambda: 7) == 7


def test_embed_and_rerank_share_the_queue(monkeypatch) -> None:
    server = load_server(monkeypatch)
    client = TestClient(server.app)

    embedded = client.post("/embed", json={"texts": ["hello"]}).json()
    assert embedded["count"] == 1
    assert embedded["embeddings"] == [[0.0, 1.0]]

    ranked = client.post("/rerank", json={"query": "q", "documents": ["a", "b"]}).json()
    assert [item["index"] for item in ranked["results"]] == [0, 1]

    openai = client.post("/v1/embeddings", json={"input": "hello"}).json()
    assert openai["object"] == "list" and len(openai["data"]) == 1

    assert server.QUEUE.stats()["completed"] == 3


def test_info_reports_the_queue(monkeypatch) -> None:
    server = load_server(monkeypatch, workers=2, depth=5)
    client = TestClient(server.app)

    body = client.get("/info").json()
    assert body["inference"]["workers"] == 2
    assert body["inference"]["queue_depth"] == 5
    assert body["inference"]["waiting"] == 0
    assert body["inference"]["rejected"] == 0

    health = client.get("/health").json()
    assert health["queue"]["workers"] == 2
