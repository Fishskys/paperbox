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
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

import httpx
from minio.error import S3Error
from sqlalchemy import text

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
#: A cached verdict is being reported after its TTL passed and the fresh probe
#: failed; the reported status is the last known one.
STALE = "stale"

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
    timeout_s: float = 3.0
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
        reason=reason,
        latency_ms=latency_ms,
        checked_at=_now_iso(),
        cached=cached,
        required=spec.required,
        detail=spec.detail,
        meta=dict(meta or {}),
    )


def _profile_http(spec: ProbeSpec) -> tuple[str, dict[str, str]]:
    """Full URL and headers for one HTTP probe, or raise for a missing token."""
    headers: dict[str, str] = {}
    if spec.auth == AUTH_BEARER:
        token = spec.token().strip()
        if not token:
            raise _MissingToken(spec.name)
        headers["Authorization"] = f"Bearer {token}"
    return spec.target().rstrip("/") + spec.path, headers


class _MissingToken(RuntimeError):
    """A probe needs a credential that is not configured."""


def _snippet(text_body: str) -> str:
    collapsed = " ".join(text_body.split())
    return collapsed[:REASON_BODY_CHARS]


async def _probe_http(spec: ProbeSpec) -> ProbeResult:
    try:
        url, headers = _profile_http(spec)
    except _MissingToken:
        # Never send a request that can only come back 401: that is exactly the
        # mistake the old "< 500 means ok" rule made.
        return _result(spec, ERROR, reason="missing token: nothing to authenticate with")

    started = time.perf_counter()
    reason = ""
    for attempt in range(max(0, spec.retries) + 1):
        try:
            async with httpx.AsyncClient(
                timeout=spec.timeout_s, transport=spec.transport
            ) as client:
                response = await client.request(
                    spec.method,
                    url,
                    json=spec.json_body,
                    headers=headers or None,
                )
        except Exception as exc:  # noqa: BLE001 - a probe must never raise
            reason = f"{type(exc).__name__}: {exc}"
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
            reason = f"HTTP {response.status_code}: {_snippet(response.text)}"
            logger.warning("probe %s failed: %s", spec.name, reason)
            return _result(spec, ERROR, reason=reason, latency_ms=latency)

        if spec.ok_when is not None:
            try:
                body = response.json()
            except ValueError as exc:
                reason = f"non-JSON body: {exc}"
                logger.warning("probe %s failed: %s", spec.name, reason)
                return _result(spec, ERROR, reason=reason, latency_ms=latency)
            if not spec.ok_when(body):
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


async def _probe_postgres(spec: ProbeSpec) -> ProbeResult:
    """``SELECT 1`` through the application's own session factory."""
    from app.db.session import SessionLocal

    started = time.perf_counter()

    def _check() -> None:
        session = SessionLocal()
        try:
            session.execute(text("SELECT 1"))
        finally:
            session.close()

    try:
        await asyncio.to_thread(_check)
    except Exception as exc:  # noqa: BLE001 - a probe must never raise
        reason = f"{type(exc).__name__}: {exc}"
        logger.warning("probe %s failed: %s", spec.name, reason)
        return _result(spec, ERROR, reason=reason, latency_ms=_elapsed_ms(started))
    return _result(spec, OK, latency_ms=_elapsed_ms(started))


async def _probe_s3(spec: ProbeSpec) -> ProbeResult:
    """``bucket_exists`` through the object-storage client.

    A missing bucket is not a failure (nothing has been uploaded yet); a refused
    credential is, and it must not be swallowed.
    """
    from app.services import object_storage

    started = time.perf_counter()

    def _check() -> None:
        client = object_storage.get_client()
        client.bucket_exists(spec.target())

    try:
        await asyncio.to_thread(_check)
    except S3Error as exc:
        if exc.code in {"NoSuchBucket", "NoSuchKey"}:
            return
        raise
    except Exception as exc:  # noqa: BLE001 - a probe must never raise
        reason = f"{type(exc).__name__}: {exc}"
        logger.warning("probe %s failed: %s", spec.name, reason)
        return _result(spec, ERROR, reason=reason, latency_ms=_elapsed_ms(started))
    return _result(spec, OK, latency_ms=_elapsed_ms(started))


async def run_probe(spec: ProbeSpec) -> ProbeResult:
    """Run one probe and answer with a verdict -- never raises."""
    target = spec.target().strip()
    if spec.kind == KIND_HTTP and not target:
        return _result(spec, DISABLED, reason="not configured")
    if spec.kind == KIND_POSTGRES:
        return await _probe_postgres(spec)
    if spec.kind == KIND_S3:
        return await _probe_s3(spec)
    return await _probe_http(spec)


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
    "STALE",
    "UNKNOWN",
    "run_probe",
]
