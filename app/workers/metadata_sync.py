"""Retry metadata-only index updates independently of ingestion and embeddings."""

import asyncio
from contextlib import suppress

from app.core.config import settings
from app.core.logging import get_logger
from app.db.session import SessionLocal
from app.services import metadata_index_sync

logger = get_logger(__name__)
_task: asyncio.Task | None = None


def _pass() -> None:
    with SessionLocal() as session:
        metadata_index_sync.flush_pending(session, limit=settings.metadata_sync_batch_size)


async def _loop() -> None:
    while True:
        try:
            await asyncio.to_thread(_pass)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("metadata synchronization pass failed")
        await asyncio.sleep(settings.metadata_sync_interval_s)


def start() -> None:
    global _task
    if _task is None:
        _task = asyncio.create_task(_loop(), name="metadata-index-sync")


async def stop() -> None:
    global _task
    if _task is not None:
        _task.cancel()
        with suppress(asyncio.CancelledError):
            await _task
        _task = None
