"""极简 Embedding Server (OpenAI 兼容 + batch 扩展)。

模型: 由 EMBEDDING_MODEL 指定, 默认 BAAI/bge-m3 (1024 维, 多语言)。
后端: fastembed (ONNX Runtime, 无 torch, CPU 友好)。

Rerank 模型: 由 RERANK_MODEL 指定, 默认 Xenova/ms-marco-MiniLM-L-6-v2。
             不在 fastembed 内置清单里的模型，用 RERANK_MODEL_FILE 指出仓库内的
             ONNX 文件路径（默认 onnx/model.onnx），启动时按 add_custom_model 注册：
             换任何 ONNX 导出都只是换环境变量，不动代码。
(交叉编码器精排, 供 app 侧两阶段检索使用)。

API:
  GET  /health                     -> {"status":"ok","model":...,"dimension":...,
                                       "rerank_model":...,"queue":{...}}
  GET  /info                       -> {"model":...,"dimension":...,"max_batch":...,
                                       "rerank_model":...,"inference":{...}}

并发: 所有推理（embed / rerank）都排进一个 FIFO 队列，由 INFERENCE_WORKERS 个
  工作线程串行执行；队列积压超过 INFERENCE_QUEUE_DEPTH 直接 503 + Retry-After，
  而不是无限排队。见 InferenceQueue 的注释。
  POST /rerank                     -> {"results":[{"index":int,"score":float}],
                                       "model":...,"took_ms":int}
       {"query": "...", "documents": ["..."], "top_n": int|null}
  POST /embed                      -> {"embeddings":[[...]], "model":..., "dimension":...}
       {"texts": ["...", ...]}
  POST /v1/embeddings (OpenAI 兼容) -> OpenAI 风格响应
"""
import os
import queue
import threading
import time
from concurrent.futures import Future

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, ConfigDict
from fastembed import TextEmbedding
from fastembed.common.model_description import ModelSource
from fastembed.rerank.cross_encoder import TextCrossEncoder

MODEL = os.environ.get("EMBEDDING_MODEL", "BAAI/bge-m3")
RERANK_MODEL = os.environ.get("RERANK_MODEL", "Xenova/ms-marco-MiniLM-L-6-v2")
MAX_BATCH = int(os.environ.get("MAX_BATCH", "64"))
# Reranking gets its own ceiling: a cross-encoder's activation memory grows with
# (tokens x batch), so the multilingual 1.1GB model needs batch 4 on this box
# (measured: 16 docs ~ 5.1GB peak vs 4 docs ~ 2.4GB). Embedding stays on
# MAX_BATCH -- sharing one knob would either OOM rerank or 422 normal ingests.
RERANK_MAX_BATCH = int(os.environ.get("RERANK_MAX_BATCH", str(MAX_BATCH)))
# 自定义（不在 fastembed 清单里的）交叉编码器：仓库内 ONNX 文件路径。
# 默认是多数 HF 导出的布局；量化导出常常把文件放在仓库根（例如
# temsa/mmarco-mMiniLMv2-L12-H384-v1-onnx-cpu-qint8 的 model.onnx），
# 那时把 RERANK_MODEL_FILE 设成 model.onnx 即可 —— 不必改代码。
RERANK_MODEL_FILE = os.environ.get("RERANK_MODEL_FILE", "onnx/model.onnx")
THREADS = int(os.environ.get("ORT_THREADS", "2"))
# 一个容器里同时跑几次 ONNX 推理。ONNX Runtime 自己就有 ORT_THREADS 条 lane，
# 再叠上 N 个并发请求，CPU 只会互相抢（本机 6 vCPU；精排模型单次峰值已 2.4GB）。
INFERENCE_WORKERS = max(1, int(os.environ.get("INFERENCE_WORKERS", "1")))
# 允许排队等待的请求数上限（不含正在跑的）。满了就 503，让客户端退避重试——
# 无界排队只会把等待时间推到客户端超时之后，那时两边都拿不到可用的错误。
INFERENCE_QUEUE_DEPTH = max(1, int(os.environ.get("INFERENCE_QUEUE_DEPTH", "32")))


class QueueFull(RuntimeError):
    """队列已满：调用方应回 503 + Retry-After，而不是继续等。"""


class InferenceQueue:
    """FIFO 队列 + 固定工作线程，所有推理调用都必须经过它。

    目的不是吞吐，而是“不抢”：embed 与 rerank 共用同一个 ONNX 会话，多个请求
    并发时 CPU/内存互相挤（历史上 uvicorn 被 oom-killer 杀掉就是这条路）。
    排队后每个请求仍然能拿到正确结果，只是排在前面的人后面。

    权衡：延迟变成“排队时间 + 推理时间”。客户端超时必须大于最坏等待时间，
    应用侧 EMBEDDING_TIMEOUT 已按此上调。
    """

    def __init__(self, workers: int, depth: int) -> None:
        self.workers = workers
        self.depth = depth
        self._queue: queue.Queue = queue.Queue(maxsize=depth)
        self._lock = threading.Lock()
        self._running = 0
        self._completed = 0
        self._rejected = 0
        self._started = False

    def start(self) -> None:
        """启动工作线程（幂等；导入时启动会让健康检查也依赖线程）。"""
        with self._lock:
            if self._started:
                return
            self._started = True
            for index in range(self.workers):
                threading.Thread(
                    target=self._run, name=f"inference-{index}", daemon=True
                ).start()

    def _run(self) -> None:
        while True:
            fn, args, kwargs, future = self._queue.get()
            with self._lock:
                self._running += 1
            try:
                if not future.set_running_or_notify_cancel():
                    continue
                try:
                    future.set_result(fn(*args, **kwargs))
                except BaseException as exc:  # noqa: BLE001 - 交给提交方
                    future.set_exception(exc)
            finally:
                with self._lock:
                    self._running -= 1
                    self._completed += 1
                self._queue.task_done()

    def submit(self, fn, *args, **kwargs):
        """按 FIFO 执行 ``fn``；队列满时抛 :class:`QueueFull`。"""
        self.start()
        future: Future = Future()
        try:
            self._queue.put_nowait((fn, args, kwargs, future))
        except queue.Full as exc:
            with self._lock:
                self._rejected += 1
            raise QueueFull(
                f"inference queue full (depth={self.depth}, workers={self.workers})"
            ) from exc
        return future.result()

    def stats(self) -> dict:
        with self._lock:
            return {
                "workers": self.workers,
                "queue_depth": self.depth,
                "waiting": self._queue.qsize(),
                "running": self._running,
                "completed": self._completed,
                "rejected": self._rejected,
            }


QUEUE = InferenceQueue(INFERENCE_WORKERS, INFERENCE_QUEUE_DEPTH)


def _busy(exc: QueueFull) -> HTTPException:
    """队列满 -> 503（与 rerank 不可用同一口径），带 Retry-After。"""
    return HTTPException(status_code=503, detail=str(exc), headers={"Retry-After": "5"})

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


def register_custom_reranker(model: str, model_file: str) -> bool:
    """把不在 fastembed 清单里的交叉编码器教给它；返回是否发生了注册。

    fastembed 只认它内置的 6 个精排模型，其余一律要 ``add_custom_model`` ——
    但那只影响"注册"这一步：加载与推理仍然是同一条 ONNX Runtime 路径，
    所以换模型（含动态 int8 量化导出）只是换环境变量，不是换架构。
    幂等：已在清单里就什么都不做，部署时无需知道当前是哪个模型。
    """
    if model in {item["model"] for item in TextCrossEncoder.list_supported_models()}:
        return False
    TextCrossEncoder.add_custom_model(
        model=model,
        sources=ModelSource(hf=model),
        model_file=model_file,
        description=f"{model} (custom cross-encoder, file={model_file})",
        license="see model card",
        size_in_gb=0.0,
    )
    return True


def get_reranker() -> TextCrossEncoder:
    """Lazily build the cross-encoder (first call downloads and caches it)."""
    global _reranker
    if _reranker is None:
        registered = register_custom_reranker(RERANK_MODEL, RERANK_MODEL_FILE)
        if registered:
            print(
                f"[rerank] registered custom cross-encoder {RERANK_MODEL} "
                f"(file={RERANK_MODEL_FILE})",
                flush=True,
            )
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
        "rerank_model_file": RERANK_MODEL_FILE,
        "rerank_loaded": _reranker is not None,
        "queue": QUEUE.stats(),
    }


@app.get("/info")
def info():
    return {
        "model": MODEL,
        "dimension": 1024,
        "max_batch": MAX_BATCH,
        "rerank_model": RERANK_MODEL,
        "rerank_model_file": RERANK_MODEL_FILE,
        "rerank_max_batch": RERANK_MAX_BATCH,
        "inference": QUEUE.stats(),
    }


@app.post("/embed")
def embed(req: EmbedRequest):
    t0 = time.time()
    try:
        vecs = QUEUE.submit(lambda: list(get_model().embed(req.texts)))
    except QueueFull as e:
        raise _busy(e) from e
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

    def run_rerank() -> list[float]:
        reranker = get_reranker()
        produced_scores: list[float] = []
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
            produced_scores.extend(float(score) for score in produced)
        return produced_scores

    try:
        scores = QUEUE.submit(run_rerank)
    except QueueFull as e:
        raise _busy(e) from e
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
    try:
        vecs = QUEUE.submit(lambda: list(get_model().embed(texts)))
    except QueueFull as e:
        raise _busy(e) from e
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