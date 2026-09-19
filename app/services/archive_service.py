"""``POST /api/papers/ingest/compressed``: a ZIP archive as the upload unit (2026-09-19).

The client uploads one archive; the server unpacks it, keeps the PDFs and queues
them as ``local_path`` jobs with ``cleanup_after=true`` (the extracted file
disappears once its bytes are in object storage, and the extraction directory
with it).

Only **zip** is supported: it is the one format the standard library can unpack
safely, so it costs no new dependency. A 7z/rar/tar upload is refused with
``415`` and a message that says why.

Unpacking a file the client controls is the most dangerous thing these endpoints
do, so the guards are explicit and all of them are tested:

* **zip-slip** -- absolute paths, drive-qualified paths, ``..`` segments, symlinks
  and device files are never written; the resolved target must stay inside the
  extraction directory;
* **zip bomb** -- three ceilings at once: entry count, total uncompressed bytes
  and compression ratio, all checked from the central directory *before* a byte
  is written, plus a per-entry cap enforced while writing;
* **nested archives** -- a zip inside the zip is not unpacked recursively; it is
  counted as ignored;
* **only PDFs** -- entries are matched by extension and then by ``%PDF`` magic.
"""

from __future__ import annotations

import os
import stat
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from app.core.logging import get_logger
from app.services.local_scan import is_within

logger = get_logger(__name__)

#: Local file header, end-of-central-directory and spanned-archive signatures.
ZIP_MAGICS: tuple[bytes, ...] = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
#: Everything the standard library cannot unpack (refused with 415).
UNSUPPORTED_SUFFIXES: tuple[str, ...] = (
    ".7z",
    ".rar",
    ".tar",
    ".gz",
    ".tgz",
    ".bz2",
    ".xz",
    ".zst",
    ".cab",
)
NESTED_SUFFIXES: tuple[str, ...] = (".zip",) + UNSUPPORTED_SUFFIXES
READ_CHUNK = 1024 * 1024
#: ``%PDF`` must appear within this many bytes (the spec allows leading junk).
PDF_MAGIC_WINDOW = 1024
PDF_MAGIC = b"%PDF"


class ArchiveError(RuntimeError):
    """Base class for archive problems (message is safe for the API)."""


class NotAnArchive(ArchiveError):
    """The upload is not a zip archive (maps to HTTP 415)."""


class ArchiveTooLarge(ArchiveError):
    """The archive itself exceeds ``INGEST_ARCHIVE_MAX_MB`` (HTTP 422)."""


class ArchiveUnsafe(ArchiveError):
    """The archive would unpack beyond its limits (HTTP 422)."""


@dataclass(frozen=True)
class ArchiveLimits:
    """The three zip-bomb ceilings plus the per-file cap."""

    max_files: int
    max_uncompressed_bytes: int
    max_ratio: int
    max_file_bytes: int

    @classmethod
    def from_settings(cls, settings) -> "ArchiveLimits":
        return cls(
            max_files=int(settings.ingest_archive_max_files),
            max_uncompressed_bytes=int(
                settings.ingest_archive_max_uncompressed_mb
            )
            * 1024
            * 1024,
            max_ratio=int(settings.ingest_archive_max_ratio),
            max_file_bytes=int(settings.ingest_max_file_mb) * 1024 * 1024,
        )


@dataclass(frozen=True)
class ExtractedEntry:
    """One PDF unpacked from the archive."""

    path: Path
    name: str
    size_bytes: int


@dataclass
class ExtractResult:
    """Outcome of one unpacking run."""

    root: Path
    entries: list[ExtractedEntry] = field(default_factory=list)
    #: Entries deliberately not unpacked: not a PDF, or a nested archive.
    ignored: int = 0
    #: Entries refused because they were unsafe or too large.
    rejected: int = 0
    reasons: list[str] = field(default_factory=list)
    total_entries: int = 0
    total_uncompressed: int = 0


# --------------------------------------------------------------------------- #
# paths
# --------------------------------------------------------------------------- #
def tmp_base(tmp_dir: str | os.PathLike[str] | None = None) -> Path:
    """Directory that holds every extraction directory (system temp default)."""
    return Path(tmp_dir) if tmp_dir else Path(tempfile.gettempdir())


def extraction_root(request_id: str, tmp_dir=None) -> Path:
    """``<tmp>/paperbox-<request_id>/`` -- where one archive is unpacked."""
    safe = "".join(ch for ch in str(request_id) if ch.isalnum() or ch in "-_")[:64]
    return tmp_base(tmp_dir) / f"paperbox-{safe or 'request'}"


def archive_path(request_id: str, tmp_dir=None) -> Path:
    """``<tmp>/paperbox-<request_id>.zip`` -- the uploaded archive itself."""
    return tmp_base(tmp_dir) / f"{extraction_root(request_id, tmp_dir).name}.zip"


def iter_extraction_dirs(tmp_dir=None) -> list[Path]:
    """Every ``paperbox-*`` extraction directory under ``tmp_dir``."""
    base = tmp_base(tmp_dir)
    try:
        return sorted(
            (entry for entry in base.iterdir() if entry.is_dir() and entry.name.startswith("paperbox-")),
            key=lambda path: path.name,
        )
    except OSError:  # pragma: no cover - unreadable temp directory
        return []


# --------------------------------------------------------------------------- #
# recognition
# --------------------------------------------------------------------------- #
def looks_like_zip(head: bytes) -> bool:
    """Whether ``head`` starts with a zip signature (empty zip included)."""
    return any(head.startswith(magic) for magic in ZIP_MAGICS)


def is_pdf_entry(name: str) -> bool:
    """Entry names are only *candidates* by extension; the magic is checked later."""
    return name.lower().endswith(".pdf")


def is_nested_archive(name: str) -> bool:
    """A zip (or any archive) inside the archive: never unpacked recursively."""
    return name.lower().endswith(NESTED_SUFFIXES)


def has_pdf_magic(path: Path) -> bool:
    """Whether a file really is a PDF (``%PDF`` within the first KiB)."""
    try:
        with Path(path).open("rb") as handle:
            head = handle.read(PDF_MAGIC_WINDOW)
    except OSError:
        return False
    return PDF_MAGIC in head


# --------------------------------------------------------------------------- #
# upload
# --------------------------------------------------------------------------- #
def save_stream(fileobj, dest: Path, *, limit: int) -> int:
    """Stream an uploaded archive to ``dest``, refusing anything over ``limit``."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    try:
        with dest.open("wb") as handle:
            while True:
                chunk = fileobj.read(READ_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise ArchiveTooLarge(
                        f"archive too large: exceeds {limit} bytes "
                        "(INGEST_ARCHIVE_MAX_MB)"
                    )
                handle.write(chunk)
    except BaseException:
        # A half-written archive is never worth keeping.
        remove_file(dest)
        raise
    if total == 0:
        remove_file(dest)
        raise NotAnArchive("uploaded archive is empty")
    return total


# --------------------------------------------------------------------------- #
# safety
# --------------------------------------------------------------------------- #
def unsafe_reason(info: zipfile.ZipInfo) -> str | None:
    """Why an entry must not be written to disk (``None`` when it is fine)."""
    name = (info.filename or "").strip()
    if not name:
        return "empty entry name"
    if name.startswith(("/", "\\")):
        return "absolute path"
    if len(name) > 1 and name[1] == ":":
        return "drive-qualified path"
    parts = PurePosixPath(name.replace("\\", "/")).parts
    if any(part == ".." for part in parts):
        return "parent traversal"
    mode = info.external_attr >> 16
    if stat.S_ISLNK(mode):
        return "symbolic link"
    if stat.S_ISCHR(mode) or stat.S_ISBLK(mode) or stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode):
        return "device file"
    return None


def safe_relative(name: str) -> Path:
    """Turn a validated entry name into a relative path inside the extraction dir."""
    parts = [
        part
        for part in PurePosixPath(name.replace("\\", "/")).parts
        if part not in ("", ".", "..")
    ]
    return Path(*parts)


# --------------------------------------------------------------------------- #
# extraction
# --------------------------------------------------------------------------- #
def extract_archive(
    source: Path,
    dest: Path,
    *,
    limits: ArchiveLimits,
) -> ExtractResult:
    """Unpack the PDFs of ``source`` into ``dest`` under every guard.

    Raises :class:`NotAnArchive` when the file is not a zip and
    :class:`ArchiveUnsafe` when the central directory alone shows the archive
    would blow past the entry-count, total-size or ratio ceiling (so a zip bomb
    is refused before anything is written).
    """
    source = Path(source)
    dest = Path(dest)
    if not zipfile.is_zipfile(source):
        raise NotAnArchive("only zip archives are supported (7z/rar/tar are not)")
    dest.mkdir(parents=True, exist_ok=True)
    result = ExtractResult(root=dest)

    with zipfile.ZipFile(source) as archive:
        infos = [info for info in archive.infolist() if not info.is_dir()]
        result.total_entries = len(infos)
        total_uncompressed = sum(int(info.file_size) for info in infos)
        total_compressed = sum(int(info.compress_size) for info in infos)

        if len(infos) > limits.max_files:
            raise ArchiveUnsafe(
                f"archive has {len(infos)} entries, "
                f"INGEST_ARCHIVE_MAX_FILES={limits.max_files}"
            )
        if total_uncompressed > limits.max_uncompressed_bytes:
            raise ArchiveUnsafe(
                f"archive unpacks to {total_uncompressed} bytes, "
                f"INGEST_ARCHIVE_MAX_UNCOMPRESSED_MB limits it to "
                f"{limits.max_uncompressed_bytes}"
            )
        if limits.max_ratio > 0 and total_compressed > 0:
            ratio = total_uncompressed / total_compressed
            if ratio > limits.max_ratio:
                raise ArchiveUnsafe(
                    f"compression ratio {ratio:.1f} exceeds "
                    f"INGEST_ARCHIVE_MAX_RATIO={limits.max_ratio}"
                )

        for info in infos:
            reason = unsafe_reason(info)
            if reason is not None:
                result.rejected += 1
                result.reasons.append(f"{info.filename}: {reason}")
                logger.warning(
                    "archive entry refused (%s): %s", reason, info.filename
                )
                continue
            if is_nested_archive(info.filename) or not is_pdf_entry(info.filename):
                result.ignored += 1
                continue
            if int(info.file_size) > limits.max_file_bytes:
                result.rejected += 1
                result.reasons.append(
                    f"{info.filename}: entry too large "
                    f"({info.file_size} > {limits.max_file_bytes})"
                )
                continue

            relative = safe_relative(info.filename)
            target = dest / relative
            if not is_within(target, dest) or target.resolve() == dest.resolve():
                result.rejected += 1
                result.reasons.append(f"{info.filename}: escapes the extraction dir")
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            written = _write_entry(archive, info, target, limits.max_file_bytes)
            if written is None:
                result.rejected += 1
                result.reasons.append(f"{info.filename}: entry too large")
                continue
            result.entries.append(
                ExtractedEntry(path=target, name=info.filename, size_bytes=written)
            )
            result.total_uncompressed += written

    logger.info(
        "archive extracted",
        extra={
            "extra_fields": {
                "entries": len(result.entries),
                "ignored": result.ignored,
                "rejected": result.rejected,
                "bytes": result.total_uncompressed,
            }
        },
    )
    return result


def _write_entry(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    target: Path,
    max_bytes: int,
) -> int | None:
    """Copy one entry out, returning the size or ``None`` when it overran."""
    written = 0
    try:
        with archive.open(info) as source, target.open("wb") as handle:
            while True:
                chunk = source.read(READ_CHUNK)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_bytes:
                    raise ArchiveUnsafe(f"{info.filename} exceeds the per-file cap")
                handle.write(chunk)
    except ArchiveUnsafe:
        remove_file(target)
        return None
    except OSError:
        remove_file(target)
        return None
    return written


# --------------------------------------------------------------------------- #
# cleanup
# --------------------------------------------------------------------------- #
def remove_file(path: str | os.PathLike[str]) -> bool:
    """Delete a file (missing is fine); returns whether it is gone."""
    candidate = Path(path)
    try:
        candidate.unlink(missing_ok=True)
    except OSError:
        logger.warning("could not delete %s", candidate)
        return False
    return True


def prune_dir(path: str | os.PathLike[str]) -> bool:
    """Remove a directory when it is empty; returns whether it is gone."""
    candidate = Path(path)
    try:
        if candidate.is_dir() and not any(candidate.iterdir()):
            candidate.rmdir()
            return True
    except OSError:
        logger.debug("could not prune %s", candidate)
    return False


def prune_tree(path: str | os.PathLike[str]) -> bool:
    """Remove empty directories bottom-up, ``path`` itself included.

    An archive whose entries were all refused, ignored or duplicates leaves an
    empty skeleton behind (``paperbox-<id>/``, maybe with empty subdirectories).
    Cleaning it up here keeps the temp directory from filling with husks the GC
    would only collect an hour later.
    """
    candidate = Path(path)
    if not candidate.is_dir():
        return False
    for current, _dirnames, _filenames in os.walk(candidate, topdown=False):
        node = Path(current)
        try:
            if not any(node.iterdir()):
                node.rmdir()
        except OSError:
            logger.debug("could not prune %s", node)
    return not candidate.exists()


def cleanup_dir(path: str | os.PathLike[str]) -> None:
    """Remove an extraction directory and everything left in it (best effort)."""
    import shutil

    candidate = Path(path)
    if not candidate.exists():
        return
    try:
        shutil.rmtree(candidate)
        logger.info("removed extraction dir %s", candidate)
    except OSError:
        logger.warning("could not remove extraction dir %s", candidate)


__all__ = [
    "ArchiveError",
    "ArchiveLimits",
    "ArchiveTooLarge",
    "ArchiveUnsafe",
    "ExtractedEntry",
    "ExtractResult",
    "NESTED_SUFFIXES",
    "NotAnArchive",
    "PDF_MAGIC_WINDOW",
    "UNSUPPORTED_SUFFIXES",
    "ZIP_MAGICS",
    "archive_path",
    "cleanup_dir",
    "extract_archive",
    "extraction_root",
    "has_pdf_magic",
    "is_nested_archive",
    "is_pdf_entry",
    "iter_extraction_dirs",
    "looks_like_zip",
    "prune_dir",
    "prune_tree",
    "remove_file",
    "safe_relative",
    "save_stream",
    "tmp_base",
    "unsafe_reason",
]
