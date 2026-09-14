"""极简 Embedding Server (OpenAI 兼容 + batch 扩展)。

模型: 由 EMBEDDING_MODEL 指定, 默认 BAAI/bge-m3 (1024 维, 多语言)。
后端: fastembed (ONNX Runtime, 无 torch, CPU 友好)。

Rerank 模型: 由 RERANK_MODEL 指定, 默认 Xenova/ms-marco-MiniLM-L-6-v2
(交叉编码器精排, 供 app 侧两阶段检索使用)。

API:
  GET  /health                     -> {"status":"ok","model":...,"dimension":...,
                                       "rerank_model":...}
  GET  /info                       -> {"model":...,"dimension":...,"max_batch":...,
                                       "rerank_model":...}
  POST /rerank                     -> {"results":[{"index":int,"score":float}],
                                       "model":...,"took_ms":int}
       {"query": "...", "documents": ["..."], "top_n": int|null}
  POST /embed                      -> {"embeddings":[[...]], "model":..., "dimension":...}
       {"texts": ["...", ...]}
  POST /v1/embeddings (OpenAI 兼容) -> OpenAI 风格响应
"""
import os
import time

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, ConfigDict
from fastembed import TextEmbedding
from fastembed.rerank.cross_encoder import TextCrossEncoder

MODEL = os.environ.get("EMBEDDING_MODEL", "BAAI/bge-m3")
RERANK_MODEL = os.environ.get("RERANK_MODEL", "Xenova/ms-marco-MiniLM-L-6-v2")
MAX_BATCH = int(os.environ.get("MAX_BATCH", "64"))
# Reranking gets its own ceiling: a cross-encoder's activation memory grows with
# (tokens x batch), so the multilingual 1.1GB model needs batch 4 on this box
# (measured: 16 docs ~ 5.1GB peak vs 4 docs ~ 2.4GB). Embedding stays on
# MAX_BATCH -- sharing one knob would either OOM rerank or 422 normal ingests.
RERANK_MAX_BATCH = int(os.environ.get("RERANK_MAX_BATCH", str(MAX_BATCH)))
THREADS = int(os.environ.get("ORT_THREADS", "2"))

app = FastAPI(title="paperbox embedding server", version="0.1.0")

_model: TextEmbedding | None = None
_reranker: TextCrossEncoder | None = None


def get_model() -> TextEmbedding:
    global _model
    if _model is None:
        _model = TextEmbedding(
            model_name=MODEL,
            cache_dir=os.environ.get("FASTEMBED_CACHE_PATH", "~/.cache/fastembed"),
            threads=THREADS,
        )
    return _model


def get_reranker() -> TextCrossEncoder:
    """Lazily build the cross-encoder (first call downloads and caches it)."""
    global _reranker
    if _reranker is None:
        _reranker = TextCrossEncoder(
            model_name=RERANK_MODEL,
            cache_dir=os.environ.get("FASTEMBED_CACHE_PATH", "~/.cache/fastembed"),
        )
    return _reranker


class EmbedRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    texts: list[str] = Field(min_length=1, max_length=MAX_BATCH)
    model: str | None = None


class OpenAIEmbedRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    model: str = MODEL
    input: list[str] | str
    encoding_format: str | None = None


class RerankRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    query: str
    documents: list[str] = Field(default_factory=list)
    top_n: int | None = None


@app.get("/health")
def health():
    return {
        "status": "ok",
        "model": MODEL,
        "dimension": 1024,
        "loaded": _model is not None,
        "rerank_model": RERANK_MODEL,
        "rerank_loaded": _reranker is not None,
    }


@app.get("/info")
def info():
    return {
        "model": MODEL,
        "dimension": 1024,
        "max_batch": MAX_BATCH,
        "rerank_model": RERANK_MODEL,
        "rerank_max_batch": RERANK_MAX_BATCH,
    }


@app.post("/embed")
def embed(req: EmbedRequest):
    t0 = time.time()
    try:
        vecs = list(get_model().embed(req.texts))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"embedding failed: {e}") from e
    dim = len(vecs[0]) if vecs else 0
    return {
        "embeddings": [[float(x) for x in v] for v in vecs],
        "model": MODEL,
        "dimension": dim,
        "latency_ms": round((time.time() - t0) * 1000, 1),
        "count": len(vecs),
    }


@app.post("/rerank")
def rerank(req: RerankRequest):
    """Score every document against the query, best first (cross-encoder)."""
    t0 = time.time()
    documents = [doc for doc in req.documents if isinstance(doc, str)]
    if not documents:
        return {"results": [], "model": RERANK_MODEL, "took_ms": 0}

    try:
        reranker = get_reranker()
        scores: list[float] = []
        # RERANK_MAX_BATCH bounds one inference call, so long candidate lists are
        # split and the original document indices are restored afterwards.
        for start in range(0, len(documents), RERANK_MAX_BATCH):
            batch = documents[start : start + RERANK_MAX_BATCH]
            produced = list(reranker.rerank(req.query, batch))
            if len(produced) != len(batch):
                raise RuntimeError(
                    f"reranker returned {len(produced)} scores "
                    f"for {len(batch)} documents"
                )
            scores.extend(float(score) for score in produced)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001 - the app side degrades on 503
        raise HTTPException(status_code=503, detail=f"rerank unavailable: {e}") from e

    ranked = sorted(
        ({"index": index, "score": score} for index, score in enumerate(scores)),
        key=lambda item: item["score"],
        reverse=True,
    )
    if req.top_n is not None:
        ranked = ranked[: max(0, req.top_n)]
    return {
        "results": ranked,
        "model": RERANK_MODEL,
        "took_ms": int(round((time.time() - t0) * 1000)),
    }


@app.post("/v1/embeddings")
def openai_compat(req: OpenAIEmbedRequest):
    texts = req.input if isinstance(req.input, list) else [req.input]
    t0 = time.time()
    vecs = list(get_model().embed(texts))
    data = [
        {"object": "embedding", "index": i, "embedding": [float(x) for x in v]}
        for i, v in enumerate(vecs)
    ]
    return {
        "object": "list",
        "model": req.model,
        "data": data,
        "usage": {"prompt_tokens": 0, "total_tokens": 0},
        "latency_ms": round((time.time() - t0) * 1000, 1),
    }