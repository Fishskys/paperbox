"""Dependency probes behind ``GET /health`` -- declarative, one spec per dependency.

The endpoint used to hold five hand-written coroutines with implicit success
criteria (``status < 500`` meant "healthy", which reports a 401 or a 404 as
fine). That is wrong the moment a dependency is a **third-party API**: a wrong
key, a renamed model or a rate limit all look healthy.

Here each dependency is one :class:`ProbeSpec` that says out loud how success is
judged (``ok_status`` plus an optional ``ok_when`` on the body), where its
credentials come from (``auth``/``token``), how long it may take
(``timeout_s``/``retries``), whether a failure makes the deployment *degraded*
(``required``) and, for billable probes, how long an answer may be reused
(``cache_ttl_s``).

:func:`run_probe` never raises: a probe answers ``ok`` / ``error`` / ``disabled``
/ ``unknown`` / ``stale`` and carries a human-readable ``reason``.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Callable

import httpx
from minio.error import S3Error
from sqlalchemy import text

from app.core.config import EMBEDDING_HEALTH_OPENAI, settings
from app.core.logging import get_logger

logger = get_logger(__name__)

#: Verdicts a probe can report.
OK = "ok"
ERROR = "error"
#: The dependency is configured but not in use (``DOCLING_URL`` empty on a
#: pypdf-only deployment): nothing is broken, so it must never read as ``error``.
DISABLED = "disabled"
#: The probe could not decide (the service answered something that carries no
#: verdict yet, e.g. an embedding container whose model is not loaded).
UNKNOWN = "unknown"

#: ``auth`` vocabulary.
AUTH_NONE = "none"
AUTH_BEARER = "bearer"

#: ``kind`` vocabulary.
KIND_HTTP = "http"
KIND_POSTGRES = "postgres"
KIND_S3 = "s3"

#: Delay between probe attempts; probes are cheap, this only avoids hammering a
#: service that just refused a connection.
RETRY_DELAY_S = 0.3

#: How much of a non-2xx body is kept in the reason (enough to see the message).
REASON_BODY_CHARS = 200

#: The text a billable probe embeds. Fixed and tiny on purpose: the same input is
#: a single token, cheap, and cacheable on the provider's side.
PROBE_TEXT = "paperbox health probe"


def _no_token() -> str:
    return ""


def _no_target() -> str:
    return ""


@dataclass(frozen=True, slots=True)
class ProbeSpec:
    """One dependency probe.

    ``target`` and ``token`` are callables, not values: tests inject fakes, and a
    configuration change takes effect on the next probe instead of at import.
    """

    #: Key in the ``services``/``dependencies`` maps (``embedding``, ...).
    name: str
    kind: str = KIND_HTTP
    #: Base URL (http), DSN (postgres) or ``host:port`` (s3). Empty = disabled.
    target: Callable[[], str] = _no_target
    path: str = ""
    method: str = "GET"
    json_body: dict[str, Any] | None = None
    auth: str = AUTH_NONE
    token: Callable[[], str] = _no_token
    #: Status codes that count as success. Anything else is ``error`` -- there is
    #: deliberately no "below 500" fallback.
    ok_status: tuple[int, ...] = (200,)
    #: Extra verdict on a successful status (needs the parsed JSON body).
    ok_when: Callable[[Any], bool] | None = None
    #: Checked *before* ``ok_when``: a service that answers but cannot serve yet
    #: (an embedding container whose model is not loaded) is ``unknown``, which
    #: is not the same as ``error`` and not the same as ``ok``.
    unknown_when: Callable[[Any], bool] | None = None
    #: Wall-clock budget for this probe. ``None`` means "take
    #: ``HEALTH_HTTP_TIMEOUT`` at probe time" (the registry passes the resolved
    #: value, so a config change never needs a restart to take effect here).
    timeout_s: float | None = None
    #: Extra attempts after the first failure. Only set this on idempotent probes.
    retries: int = 0
    #: ``False`` for optional dependencies: a failure is reported but does not
    #: make the deployment degraded.
    required: bool = True
    #: Reuse a fresh verdict for this many seconds (billable probes: a container
    #: healthcheck polls every 20 s and must not buy an API call each time).
    cache_ttl_s: float = 0.0
    #: One line for humans: which endpoint/contract this probe speaks.
    detail: str = ""
    #: Test seam: an ``httpx`` transport so unit tests never touch the network.
    transport: httpx.AsyncBaseTransport | None = None
    #: Extra fields carried into ``ProbeResult.meta`` (e.g. measured dimension).
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """What one probe decided."""

    name: str
    status: str
    reason: str = ""
    latency_ms: float | None = None
    checked_at: str = ""
    cached: bool = False
    required: bool = True
    detail: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def healthy(self) -> bool:
        """``True`` when this verdict must not count as a problem."""
        return self.status in (OK, DISABLED)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _result(
    spec: ProbeSpec,
    status: str,
    *,
    reason: str = "",
    latency_ms: float | None = None,
    cached: bool = False,
    meta: dict[str, Any] | None = None,
) -> ProbeResult:
    return ProbeResult(
        name=spec.name,
        status=status,
        # Every reason leaves here through the same redaction: a driver naming
        # the DSN it could not reach must not publish that DSN.
        reason=redact(reason) if reason else "",
        latency_ms=latency_ms,
        checked_at=_now_iso(),
        cached=cached,
        required=spec.required,
        detail=spec.detail,
        meta=dict(meta or {}),
    )


def _profile_http(spec: ProbeSpec) -> tuple[str, dict[str, str], str]:
    """Full URL, headers and the token used, or raise for a missing credential."""
    token = ""
    headers: dict[str, str] = {}
    if spec.auth == AUTH_BEARER:
        token = spec.token().strip()
        if not token:
            raise _MissingToken(spec.name)
        headers["Authorization"] = f"Bearer {token}"
    return spec.target().rstrip("/") + spec.path, headers, token


class _MissingToken(RuntimeError):
    """A probe needs a credential that is not configured."""


def _snippet(text_body: str) -> str:
    collapsed = " ".join(text_body.split())
    return collapsed[:REASON_BODY_CHARS]


#: ``user:password@host`` inside a message we are about to echo back, with or
#: without a scheme in front of it (``postgresql://user:pw@db`` or a service
#: answering ``your key user:pw@host was rejected``).
_CREDENTIALS_IN_TEXT = re.compile(
    r"(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*://)?[^/\s:@]+:[^/\s@]*@"
)


def redact(message: str, *, secret: str = "") -> str:
    """Remove credential-shaped text from anything we report.

    A ``/health`` body is public: a driver that names the DSN it failed on, or a
    service that echoes the key it just rejected, must not leak through it.
    """
    cleaned = _CREDENTIALS_IN_TEXT.sub(r"\g<scheme>***@", message)
    if secret:
        cleaned = cleaned.replace(secret, "***")
    return cleaned


def _effective_timeout(spec: ProbeSpec) -> float:
    """The probe's budget: its own value, or ``HEALTH_HTTP_TIMEOUT``."""
    if spec.timeout_s is not None:
        return spec.timeout_s
    return settings.health_http_timeout


async def _probe_http(spec: ProbeSpec) -> ProbeResult:
    try:
        url, headers, token = _profile_http(spec)
    except _MissingToken:
        # Never send a request that can only come back 401: that is exactly the
        # mistake the old "< 500 means ok" rule made.
        return _result(spec, ERROR, reason="credential missing: request not sent")

    timeout = _effective_timeout(spec)
    started = time.perf_counter()
    reason = ""
    for attempt in range(max(0, spec.retries) + 1):
        try:
            async with httpx.AsyncClient(
                timeout=timeout, transport=spec.transport
            ) as client:
                response = await client.request(
                    spec.method,
                    url,
                    json=spec.json_body,
                    headers=headers or None,
                )
        except Exception as exc:  # noqa: BLE001 - a probe must never raise
            reason = redact(f"{type(exc).__name__}: {exc}", secret=token)
            if attempt < spec.retries:
                logger.warning("probe %s retrying: %s", spec.name, reason)
                await asyncio.sleep(RETRY_DELAY_S)
                continue
            logger.warning("probe %s failed: %s", spec.name, reason)
            return _result(
                spec,
                ERROR,
                reason=reason,
                latency_ms=_elapsed_ms(started),
            )

        latency = _elapsed_ms(started)
        if response.status_code not in spec.ok_status:
            reason = redact(
                f"HTTP {response.status_code}: {_snippet(response.text)}", secret=token
            )
            logger.warning("probe %s failed: %s", spec.name, reason)
            return _result(spec, ERROR, reason=reason, latency_ms=latency)

        if spec.ok_when is not None or spec.unknown_when is not None:
            try:
                body = response.json()
            except ValueError as exc:
                reason = redact(f"non-JSON body: {exc}", secret=token)
                logger.warning("probe %s failed: %s", spec.name, reason)
                return _result(spec, ERROR, reason=reason, latency_ms=latency)
            if spec.unknown_when is not None and spec.unknown_when(body):
                # Answering is not the same as serving: an embedding container
                # whose model is not loaded yet cannot embed anything.
                reason = "the service answered but cannot serve yet"
                logger.warning("probe %s: %s", spec.name, reason)
                return _result(spec, UNKNOWN, reason=reason, latency_ms=latency)
            if spec.ok_when is not None and not spec.ok_when(body):
                reason = "body did not match the success criteria"
                logger.warning("probe %s failed: %s", spec.name, reason)
                return _result(spec, ERROR, reason=reason, latency_ms=latency)
            return _result(
                spec, OK, latency_ms=latency, meta=_meta_from_detail(spec, body)
            )

        return _result(spec, OK, latency_ms=latency)
    return _result(  # pragma: no cover - the loop always returns
        spec, ERROR, reason=reason or "probe did not run"
    )


def _meta_from_detail(spec: ProbeSpec, body: Any) -> dict[str, Any]:
    """Carry the numbers a spec asked for (``meta``: field name -> JSON path)."""
    if not spec.meta or not isinstance(body, dict):
        return {}
    found: dict[str, Any] = {}
    for key, path in spec.meta.items():
        value: Any = body
        for part in str(path).split("."):
            if not isinstance(value, dict) or part not in value:
                value = None
                break
            value = value[part]
        if value is not None:
            found[key] = value
    return found


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)


async def _probe_blocking(spec: ProbeSpec, check: Callable[[], None]) -> ProbeResult:
    """Run a blocking check in a thread, under the probe's own budget.

    ``asyncio.wait_for`` cancels the *await*, not the thread (a driver call in
    flight finishes on its own). The point is that ``/health`` answers within the
    budget instead of waiting forever on a socket that is not answering.
    """
    started = time.perf_counter()
    budget = _effective_timeout(spec)
    try:
        await asyncio.wait_for(asyncio.to_thread(check), timeout=budget)
    except TimeoutError:
        reason = f"timed out after {budget:g}s"
        logger.warning("probe %s failed: %s", spec.name, reason)
        return _result(spec, ERROR, reason=reason, latency_ms=_elapsed_ms(started))
    except Exception as exc:  # noqa: BLE001 - a probe must never raise
        reason = f"{type(exc).__name__}: {exc}"
        logger.warning("probe %s failed: %s", spec.name, reason)
        return _result(spec, ERROR, reason=reason, latency_ms=_elapsed_ms(started))
    return _result(spec, OK, latency_ms=_elapsed_ms(started))


async def _probe_postgres(spec: ProbeSpec) -> ProbeResult:
    """``SELECT 1`` through the application's own session factory."""
    from app.db.session import SessionLocal

    def _check() -> None:
        session = SessionLocal()
        try:
            session.execute(text("SELECT 1"))
        finally:
            session.close()

    return await _probe_blocking(spec, _check)


async def _probe_s3(spec: ProbeSpec) -> ProbeResult:
    """``bucket_exists`` through the object-storage client.

    A missing bucket is not a failure (nothing has been uploaded yet); a refused
    credential is, and it must not be swallowed.
    """
    from app.services import object_storage

    def _check() -> None:
        client = object_storage.get_client()
        try:
            client.bucket_exists(spec.target())
        except S3Error as exc:
            if exc.code in {"NoSuchBucket", "NoSuchKey"}:
                return
            raise

    return await _probe_blocking(spec, _check)


#: Verdict cache for specs that set ``cache_ttl_s``: name -> (verdict, monotonic).
_CACHE: dict[str, "ProbeResult"] = {}
_CACHE_STAMPS: dict[str, float] = {}
#: Clock behind the cache; tests drive it, production uses the monotonic one.
_CLOCK: Callable[[], float] = time.monotonic


async def run_probe(spec: ProbeSpec, *, use_cache: bool = True) -> ProbeResult:
    """Run one probe and answer with a verdict -- never raises.

    With ``cache_ttl_s`` set, the verdict is reused for that many seconds (a
    billable probe must not be bought again just because the container
    healthcheck polled again). ``checked_at`` belongs to the verdict, not to the
    request: a reused verdict keeps the moment it was actually measured, so an
    operator can see how old the answer is.
    """
    if not use_cache or spec.cache_ttl_s <= 0:
        return await _probe_once(spec)
    cached = _cached_verdict(spec)
    if cached is not None:
        return replace(cached, cached=True)
    fresh = await _probe_once(spec)
    _CACHE[spec.name] = fresh
    _CACHE_STAMPS[spec.name] = _CLOCK()
    return fresh


def _cached_verdict(spec: ProbeSpec) -> ProbeResult | None:
    result = _CACHE.get(spec.name)
    stored_at = _CACHE_STAMPS.get(spec.name)
    if result is None or stored_at is None:
        return None
    if _CLOCK() - stored_at >= spec.cache_ttl_s:
        return None
    return result


def clear_probe_cache() -> None:
    """Drop every cached verdict (operator action: force a fresh probe)."""
    _CACHE.clear()
    _CACHE_STAMPS.clear()


async def _probe_once(spec: ProbeSpec) -> ProbeResult:
    """One real probe, no cache in the way."""
    target = spec.target().strip()
    if spec.kind == KIND_HTTP and not target:
        return _result(spec, DISABLED, reason="not configured")
    if spec.kind == KIND_POSTGRES:
        return await _probe_postgres(spec)
    if spec.kind == KIND_S3:
        return await _probe_s3(spec)
    return await _probe_http(spec)


# --------------------------------------------------------------------------- #
# the deployment's own dependencies
# --------------------------------------------------------------------------- #

def _timeout_for(name: str) -> float:
    """Probe budget: an explicit ``HEALTH_TIMEOUT_OVERRIDES`` entry wins.

    Every dependency gets the same default -- ``HEALTH_HTTP_TIMEOUT`` (10 s) --
    because the budget answers "how long may one probe take", not "where does the
    target live". A dependency that needs a different budget gets it from
    ``HEALTH_TIMEOUT_OVERRIDES`` (``docling=30``).
    """
    return settings.health_timeout_map.get(name, settings.health_http_timeout)


def postgres_spec() -> ProbeSpec:
    """The metadata store: the cheapest statement that proves a usable session."""
    return ProbeSpec(
        name="postgres",
        kind=KIND_POSTGRES,
        timeout_s=_timeout_for("postgres"),
        detail="SELECT 1 over POSTGRES_DSN",
    )


def opensearch_spec() -> ProbeSpec:
    """The search cluster: 200 is not enough, a red cluster cannot answer."""
    return ProbeSpec(
        name="opensearch",
        kind=KIND_HTTP,
        target=lambda: settings.opensearch_url,
        path="/_cluster/health",
        ok_when=lambda body: body.get("status") in {"green", "yellow"},
        timeout_s=_timeout_for("opensearch"),
        retries=settings.health_probe_retries,
        detail="GET /_cluster/health",
    )


def minio_spec() -> ProbeSpec:
    """The object store: a missing bucket is fine, a refused key is not."""
    return ProbeSpec(
        name="minio",
        kind=KIND_S3,
        target=lambda: settings.minio_bucket,
        timeout_s=_timeout_for("minio"),
        detail="bucket_exists(MINIO_BUCKET)",
    )


def embedding_spec() -> ProbeSpec:
    """The embedding dependency, judged by the contract the probe is told to speak.

    ``container`` (default) is the deployment's own service; ``openai_compatible``
    is a third-party API, whose answer must also prove the vector width. The
    *client* keeps the container contract either way -- this is health only.
    """
    if settings.embedding_health_mode == EMBEDDING_HEALTH_OPENAI:
        return _openai_embedding_spec()
    return _container_embedding_spec()


def _container_embedding_spec() -> ProbeSpec:
    return ProbeSpec(
        name="embedding",
        kind=KIND_HTTP,
        target=lambda: settings.embedding_url,
        path="/health",
        ok_when=lambda body: body.get("status") == "ok",
        # The container answers /health before the model finishes loading.
        unknown_when=lambda body: body.get("loaded") is False,
        timeout_s=_timeout_for("embedding"),
        retries=settings.health_probe_retries,
        detail="container GET /health",
    )


def _openai_embedding_spec() -> ProbeSpec:
    """A third-party embedding API: ``POST /embeddings`` with a bearer token.

    Success is a 200 whose first vector has the configured width -- a wrong key
    (401), a renamed model (404) or a swapped model (768 dims) all have to read
    as a problem, which is what the old "< 500 means ok" rule could not see. The
    call is billable, so it is never retried and its verdict is cached.
    """
    expected = settings.embedding_dimension

    def _vector_width_matches(body: Any) -> bool:
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, list) or not data:
            return False
        vector = data[0].get("embedding") if isinstance(data[0], dict) else None
        return isinstance(vector, list) and len(vector) == expected

    return ProbeSpec(
        name="embedding",
        kind=KIND_HTTP,
        target=lambda: settings.embedding_url,
        path="/embeddings",
        method="POST",
        json_body={"model": settings.embedding_model, "input": [PROBE_TEXT]},
        auth=AUTH_BEARER,
        token=lambda: settings.embedding_api_key,
        ok_status=(200,),
        ok_when=_vector_width_matches,
        timeout_s=_timeout_for("embedding"),
        retries=0,
        cache_ttl_s=settings.health_probe_cache_ttl,
        detail="OpenAI-compatible /embeddings (vector width checked)",
    )


def docling_spec() -> ProbeSpec:
    """The parsing backend. Empty ``DOCLING_URL`` means "not in use", not "down"."""
    return ProbeSpec(
        name="docling",
        kind=KIND_HTTP,
        target=lambda: settings.docling_url,
        path="/health",
        # Exactly 200: a 404 on that port means something else is listening.
        ok_status=(200,),
        timeout_s=_timeout_for("docling"),
        retries=settings.health_probe_retries,
        detail="GET /health (may live on another host)",
    )


def rerank_spec() -> ProbeSpec:
    """The cross-encoder behind ``RERANK_URL`` (the embedding container, here).

    Optional on purpose: a reranker that cannot answer only means search keeps
    the first-stage order, so it is reported without degrading the deployment.
    """
    return ProbeSpec(
        name="rerank",
        kind=KIND_HTTP,
        target=lambda: settings.rerank_url if settings.rerank_enabled else "",
        path="/health",
        ok_when=lambda body: body.get("status") == "ok" or "rerank_model" in body,
        # Same container contract: it answers /health before loading the model.
        unknown_when=lambda body: body.get("rerank_loaded") is False,
        timeout_s=_timeout_for("rerank"),
        retries=settings.health_probe_retries,
        required=False,
        detail="GET /health (rerank_model present)",
    )


def health_specs() -> tuple[ProbeSpec, ...]:
    """The registry: one spec per dependency, in the order the endpoint reports."""
    return (
        postgres_spec(),
        opensearch_spec(),
        minio_spec(),
        embedding_spec(),
        rerank_spec(),
        docling_spec(),
    )


async def probe_all(
    specs: Sequence[ProbeSpec] | None = None, *, use_cache: bool = True
) -> tuple[ProbeResult, ...]:
    """Probe every spec concurrently, preserving the registry's order."""
    chosen = tuple(specs) if specs is not None else health_specs()
    if not chosen:
        return ()
    return tuple(
        await asyncio.gather(
            *(run_probe(spec, use_cache=use_cache) for spec in chosen)
        )
    )


def is_degraded(results: Sequence[ProbeResult]) -> bool:
    """``True`` when a *required* dependency is not answering as expected.

    ``disabled`` never counts: a dependency that is configured off (docling on a
    pypdf-only deployment) is a decision, not an outage.
    """
    return any(result.required and not result.healthy for result in results)


def dependency_payload(result: ProbeResult) -> dict[str, Any]:
    """One dependency as the endpoint reports it (extra fields only when set)."""
    payload: dict[str, Any] = {
        "status": result.status,
        "required": result.required,
    }
    if result.detail:
        payload["detail"] = result.detail
    if result.latency_ms is not None:
        payload["latency_ms"] = result.latency_ms
    if result.checked_at:
        payload["checked_at"] = result.checked_at
    if result.cached:
        payload["cached"] = True
    if result.reason:
        payload["reason"] = result.reason
    if result.meta:
        payload["meta"] = result.meta
    return payload


__all__ = [
    "AUTH_BEARER",
    "AUTH_NONE",
    "DISABLED",
    "ERROR",
    "KIND_HTTP",
    "KIND_POSTGRES",
    "KIND_S3",
    "OK",
    "ProbeResult",
    "ProbeSpec",
    "UNKNOWN",
    "clear_probe_cache",
    "dependency_payload",
    "docling_spec",
    "embedding_spec",
    "health_specs",
    "is_degraded",
    "minio_spec",
    "opensearch_spec",
    "postgres_spec",
    "probe_all",
    "redact",
    "rerank_spec",
    "run_probe",
]
