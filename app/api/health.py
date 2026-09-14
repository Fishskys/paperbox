"""``GET /health`` -- lightweight dependency probes (MVP-SPEC section 2).

Each dependency gets a short probe. Failures are reported as ``"error"`` for
that service while the overall status stays ``"ok"``: this endpoint answers for
the liveness of the API process itself.
"""

from __future__ import annotations

import asyncio

import httpx
from fastapi import APIRouter
from minio.error import S3Error
from sqlalchemy import text

from app import __version__
from app.core.config import settings
from app.core.logging import get_logger
from app.db.session import SessionLocal
from app.services import object_storage

logger = get_logger(__name__)

router = APIRouter(tags=["health"])

PROBE_TIMEOUT = 3.0
OK = "ok"
ERROR = "error"
SLASH = chr(47)


def _check_postgres() -> str:
    session = SessionLocal()
    try:
        session.execute(text("SELECT 1"))
        return OK
    except Exception as exc:  # noqa: BLE001 - a probe must never raise
        logger.warning("postgres probe failed: %s", exc)
        return ERROR
    finally:
        session.close()


def _check_minio() -> str:
    try:
        client = object_storage.get_client()
        client.bucket_exists(settings.minio_bucket)
        return OK
    except S3Error as exc:
        if exc.code in {"NoSuchBucket", "NoSuchKey"}:
            return OK
        logger.warning("minio probe failed: %s", exc)
        return ERROR
    except Exception as exc:  # noqa: BLE001 - a probe must never raise
        logger.warning("minio probe failed: %s", exc)
        return ERROR


async def _check_http(url: str, path: str) -> str:
    target = url.rstrip(SLASH) + path
    try:
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT) as client:
            response = await client.get(target)
        return OK if response.status_code < 500 else ERROR
    except Exception as exc:  # noqa: BLE001 - a probe must never raise
        logger.warning("probe failed for %s: %s", target, exc)
        return ERROR


async def _check_opensearch() -> str:
    return await _check_http(settings.opensearch_url, SLASH)


async def _check_embedding() -> str:
    return await _check_http(settings.embedding_url, SLASH + "health")


@router.get("/health")
async def health() -> dict:
    """Report API liveness plus a quick status for each dependency."""
    postgres, minio, opensearch, embedding = await asyncio.gather(
        asyncio.to_thread(_check_postgres),
        asyncio.to_thread(_check_minio),
        _check_opensearch(),
        _check_embedding(),
    )
    return {
        "status": OK,
        "version": __version__,
        "services": {
            "postgres": postgres,
            "opensearch": opensearch,
            "minio": minio,
            "embedding": embedding,
        },
    }


__all__ = ["router"]
