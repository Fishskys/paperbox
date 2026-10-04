"""Signed download of the stored original (``GET /api/downloads/{paper_id}``).

This route is deliberately **not** behind :func:`app.core.security.require_api_key`:
the whole point of :mod:`app.services.download_signing` is that an agent can fetch
the PDF from a short-lived link that carries no long-lived credential. The
signature and its expiry *are* the credential, and the object storage is still
reached through the app (the bucket stays private).

Only the original file of a live paper is reachable, and only while the signature
is valid: a tampered ``sig`` is a 403, an expired link is a 403 that says so.
"""

from __future__ import annotations

import re
import time
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.db.session import get_db
from app.services import download_signing, object_storage
from app.services import paper_service as papers

logger = get_logger(__name__)

router = APIRouter(prefix="/api/downloads", tags=["downloads"])

#: Read size when streaming from the object store.
STREAM_CHUNK_BYTES = 64 * 1024

#: Characters allowed in the ASCII fallback of a download filename.
_ASCII_SAFE = re.compile(r"[^A-Za-z0-9._-]")


def disposition(filename: str) -> str:
    """RFC 6266 ``Content-Disposition`` for an arbitrary (possibly CJK) name.

    Starlette encodes headers as latin-1, so an unquoted ``filename=<中文名>.pdf``
    raised ``UnicodeEncodeError`` and the download answered 500 -- a stored file
    with a non-ASCII name could not be fetched at all (found on the live corpus,
    2026-10-04). The fix is the standard two-part header: an ASCII fallback plus a
    percent-encoded UTF-8 ``filename*`` that every real client prefers.
    """
    name = filename or object_storage.ORIGINAL_FILENAME
    if name.isascii():
        return f'attachment; filename="{name}"'
    fallback = _ASCII_SAFE.sub("_", name) or object_storage.ORIGINAL_FILENAME
    return (
        f'attachment; filename="{fallback}"; '
        f"filename*=UTF-8''{quote(name, safe='')}"
    )


def stream_original(record) -> StreamingResponse:
    """Stream one stored file with an attachment disposition.

    Shared with ``GET /api/papers/{paper_id}/file`` so both surfaces stream the
    same bytes the same way (and neither buffers a PDF in memory).
    """
    try:
        stream = object_storage.open_stream(record.object_key, bucket=record.bucket)
    except object_storage.ObjectNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="paper file not found"
        ) from exc
    except object_storage.ObjectStorageError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="object storage unavailable",
        ) from exc

    filename = record.filename or object_storage.ORIGINAL_FILENAME
    media_type = record.content_type or object_storage.DEFAULT_PDF_CONTENT_TYPE
    headers = {"Content-Disposition": disposition(filename)}
    if record.size_bytes is not None:
        headers["Content-Length"] = str(record.size_bytes)

    def _iterate():
        with stream as body:
            while True:
                chunk = body.read(STREAM_CHUNK_BYTES)
                if not chunk:
                    break
                yield chunk

    return StreamingResponse(_iterate(), media_type=media_type, headers=headers)


@router.get("/{paper_id}")
def download_paper(
    paper_id: str,
    exp: int = Query(description="Unix timestamp the link stops working"),
    sig: str = Query(description="HMAC over paper_id + exp"),
    session: Session = Depends(get_db),
):
    """Stream a paper's original PDF when the signature is valid and unexpired."""
    if not download_signing.verify(paper_id, exp, sig):
        logger.warning("download refused: bad signature for %s", paper_id)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="invalid download signature"
        )
    if exp < int(time.time()):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="download link expired; ask the tool for a fresh one",
        )

    paper = papers.get_paper(session, paper_id)
    if paper is None or paper.deleted_at is not None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="paper not found"
        )
    record = papers.original_file(paper)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="paper file not found"
        )
    return stream_original(record)


__all__ = ["router", "stream_original"]
