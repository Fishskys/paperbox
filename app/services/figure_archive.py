"""Build a bounded ZIP snapshot of a paper's currently visible figures."""

from dataclasses import dataclass
import hashlib
import json
import tempfile
from typing import BinaryIO
import zipfile

from sqlalchemy import select

from app.core.config import settings
from app.db.models import FigureExtractionRun as Run, PaperFigure as Figure
from app.services import figure_service as figures, object_storage


@dataclass
class Archive:
    content: BinaryIO
    size_bytes: int
    filename: str
    generation_id: str


def _snapshot(session, paper_id, variant, generation_id):
    record = figures.paper(session, paper_id)
    current = session.scalar(
        select(Run).where(Run.paper_id == record.id, Run.is_current.is_(True))
    )
    if generation_id and (current is None or current.id != generation_id):
        raise figures.FigureError(
            "FIGURE_GENERATION_CHANGED", "the figure generation has changed"
        )
    if current is None:
        previous = session.scalar(select(Run).where(Run.paper_id == record.id).limit(1))
        code = "FIGURE_ARCHIVE_NOT_READY" if previous else "FIGURES_NOT_EXTRACTED"
        raise figures.FigureError(code, "extract figures before downloading an archive")
    rows = list(
        session.scalars(
            select(Figure)
            .where(Figure.run_id == current.id, Figure.deleted_at.is_(None))
            .order_by(Figure.ordinal)
        )
    )
    assets = figures.assets_by_figure(session, [row.id for row in rows])
    manifest = {
        "schema_version": 1,
        "paper_id": record.id,
        "title": record.title,
        "generation_id": current.id,
        "source_sha256": current.source_sha256,
        "variant": variant,
        "status": current.status,
        "warnings": list(current.warnings),
        "figures": [],
    }
    files, captions = [], []
    for row in rows:
        public = figures.serialize_figure(row, assets[row.id])
        directory = public["display_name"] + "-" + row.id
        public["archive_files"] = []
        captions.append((directory + "/caption.txt", row.caption_text.encode("utf-8")))
        for asset in sorted(
            assets[row.id], key=lambda item: (item.role, item.part_index)
        ):
            if variant != "all" and asset.role != variant:
                continue
            filename = (
                "figure.pdf"
                if asset.role == "native"
                else f"part-{asset.part_index:03}-{asset.role}.png"
            )
            path = directory + "/" + filename
            public["archive_files"].append(
                {
                    "path": path,
                    "role": asset.role,
                    "part": asset.part_index,
                    "size_bytes": asset.size_bytes,
                    "sha256": asset.sha256,
                }
            )
            files.append(
                {
                    "path": path,
                    "key": asset.object_key,
                    "bucket": current.bucket,
                    "size_bytes": asset.size_bytes,
                    "sha256": asset.sha256,
                }
            )
        if not public["archive_files"]:
            manifest["warnings"].append(
                {
                    "code": "FIGURE_ARCHIVE_VARIANT_UNAVAILABLE",
                    "figure_id": row.id,
                    "variant": variant,
                }
            )
        manifest["figures"].append(public)
    snapshot = {
        "paper_id": record.id,
        "generation_id": current.id,
        "revision": record.figure_revision,
        "manifest": manifest,
        "files": files,
        "captions": captions,
    }
    # Release the database snapshot before object storage I/O; recheck visibility
    # after packaging so deletion or replacement cannot publish an old archive.
    session.rollback()
    return snapshot


def build(
    session, paper_id, *, variant="native", generation_id=None, store=object_storage
):
    if variant not in ("native", "preview", "thumbnail", "all"):
        raise figures.FigureError(
            "FIGURE_ARCHIVE_VARIANT_INVALID", "unsupported archive variant", 422
        )
    snapshot = _snapshot(session, paper_id, variant, generation_id)
    budget = settings.figure_max_temp_bytes
    metadata = [
        (
            "manifest.json",
            json.dumps(snapshot["manifest"], ensure_ascii=False, indent=2).encode(
                "utf-8"
            ),
        ),
        *snapshot["captions"],
    ]
    if (
        sum(len(data) for _, data in metadata)
        + sum(item["size_bytes"] for item in snapshot["files"])
        > budget
    ):
        raise figures.FigureError(
            "FIGURE_ARCHIVE_TOO_LARGE", "figure archive exceeds its byte budget", 413
        )
    spool = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b")
    try:
        with zipfile.ZipFile(spool, "w", compression=zipfile.ZIP_STORED) as archive:
            for path, data in metadata:
                archive.writestr(path, data)
            for item in snapshot["files"]:
                digest, size = hashlib.sha256(), 0
                with (
                    store.open_stream(item["key"], bucket=item["bucket"]) as source,
                    archive.open(item["path"], "w", force_zip64=True) as target,
                ):
                    while block := source.read(64 * 1024):
                        size += len(block)
                        if (
                            size > item["size_bytes"]
                            or spool.tell() + len(block) > budget
                        ):
                            raise figures.FigureError(
                                "FIGURE_ARCHIVE_TOO_LARGE",
                                "figure archive exceeds its byte budget",
                                413,
                            )
                        digest.update(block)
                        target.write(block)
                if size != item["size_bytes"] or digest.hexdigest() != item["sha256"]:
                    raise figures.FigureError(
                        "FIGURE_ASSET_CORRUPT",
                        "a figure asset failed integrity validation",
                    )
        size = spool.tell()
        if size > budget:
            raise figures.FigureError(
                "FIGURE_ARCHIVE_TOO_LARGE",
                "figure archive exceeds its byte budget",
                413,
            )
        record = figures.paper(session, snapshot["paper_id"])
        current = session.scalar(
            select(Run.id).where(Run.paper_id == record.id, Run.is_current.is_(True))
        )
        if (
            record.figure_revision != snapshot["revision"]
            or current != snapshot["generation_id"]
        ):
            raise figures.FigureError(
                "FIGURE_GENERATION_CHANGED",
                "figures changed while preparing the archive; download again",
            )
        session.rollback()
        spool.seek(0)
        return Archive(
            spool, size, f"paper-{snapshot['paper_id']}-figures-{variant}.zip", current
        )
    except object_storage.ObjectNotFound as exc:
        spool.close()
        raise figures.FigureError(
            "FIGURE_ASSET_NOT_FOUND", "a figure asset is unavailable", 404
        ) from exc
    except object_storage.ObjectStorageError as exc:
        spool.close()
        raise figures.FigureError(
            "FIGURE_STORAGE_UNAVAILABLE", "figure storage is unavailable", 503
        ) from exc
    except BaseException:
        spool.close()
        raise
