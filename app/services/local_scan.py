"""Server-side directory scanning for ``POST /api/papers/ingest/dir`` (2026-09-19).

When the PDFs are already on the machine that runs paperbox -- or on a volume it
has mounted -- the fastest import is the one that transfers nothing: the server
walks the directory, hashes each candidate and hands the pipeline a path.

That makes the endpoint a **new filesystem access surface**, so the rules are
strict and non-negotiable:

* **opt-in** -- with ``INGEST_LOCAL_ROOTS`` empty the endpoint does not exist
  (the API answers ``404``);
* **contained** -- ``root`` is ``realpath``-normalized and must land inside one
  of the whitelisted roots, so ``..`` and symlinked directories cannot escape;
* **read-only, no symlinks** -- the walk never follows a symlinked directory and
  every symlinked *file* is skipped, because a link inside a whitelisted root is
  the obvious way out of it;
* **quiet** -- hidden files/directories and editor/partial-download leftovers are
  skipped instead of being hashed;
* **bounded** -- files above ``INGEST_MAX_FILE_MB`` are rejected before hashing,
  and the walk stops after ``limit`` candidates.

Nothing here talks to the database or the API: :func:`scan` returns plain
descriptors, which keeps the security rules unit-testable on their own.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath

from app.core.logging import get_logger
from app.services import paper_service

logger = get_logger(__name__)

DEFAULT_GLOB = "**/*.pdf"
#: Suffixes of files that are being written right now (editor swaps, partial
#: downloads, our own temp files). They are never a finished PDF.
TEMP_SUFFIXES: tuple[str, ...] = (
    ".tmp",
    ".temp",
    ".part",
    ".partial",
    ".crdownload",
    ".download",
    ".swp",
    ".bak",
    "~",
)
#: Directory names that are never worth walking into.
SKIP_DIRS: frozenset[str] = frozenset(
    {"__pycache__", "node_modules", ".git", ".svn", "$recycle.bin"}
)


class LocalScanError(RuntimeError):
    """Base class for directory-import problems (message is safe for the API)."""


class ScanUnavailable(LocalScanError):
    """The endpoint is disabled on this deployment (no whitelisted roots)."""


class RootNotAllowed(LocalScanError):
    """The requested root is outside ``INGEST_LOCAL_ROOTS``."""


class RootMissing(LocalScanError):
    """The requested root does not exist or is not a directory."""


@dataclass(frozen=True)
class ScannedFile:
    """One candidate file found by :func:`scan`."""

    path: Path
    relative: str
    size_bytes: int
    #: SHA256 computed during the scan (``None`` when the file was rejected
    #: before hashing).
    sha256: str | None = None
    #: Set when the file must be rejected (too large / unreadable); ``None`` when
    #: the file is a valid candidate.
    reason: str | None = None
    error_code: str | None = None


@dataclass
class ScanResult:
    """Outcome of one directory walk."""

    root: Path
    glob: str
    recursive: bool
    files: list[ScannedFile] = field(default_factory=list)
    #: Candidates the glob matched but that were never processed (``limit``).
    skipped: int = 0

    @property
    def matched(self) -> int:
        """Every candidate the glob matched, capped or not."""
        return len(self.files) + self.skipped


# --------------------------------------------------------------------------- #
# containment
# --------------------------------------------------------------------------- #
def _norm(path: str | os.PathLike[str]) -> str:
    """``realpath`` + ``normcase``: the form every comparison uses."""
    return os.path.normcase(os.path.realpath(str(path)))


def is_within(path: str | os.PathLike[str], root: str | os.PathLike[str]) -> bool:
    """Whether ``path`` is ``root`` or lives under it (symlinks resolved)."""
    candidate = _norm(path)
    base = _norm(root)
    if candidate == base:
        return True
    return candidate.startswith(base.rstrip("\\/") + os.sep)


def ensure_allowed(root: str | os.PathLike[str], roots: list[Path]) -> Path:
    """Validate ``root`` against the whitelist and return its real path.

    Raises :class:`ScanUnavailable` when no root is whitelisted (the endpoint is
    disabled), :class:`RootNotAllowed` when the path resolves outside every
    whitelisted root, and :class:`RootMissing` when it is not a directory.
    """
    if not roots:
        raise ScanUnavailable(
            "directory import is disabled: INGEST_LOCAL_ROOTS is empty"
        )
    raw = str(root or "").strip()
    if not raw:
        raise RootNotAllowed("root is required")
    resolved = Path(os.path.realpath(os.path.expanduser(raw)))
    if not any(is_within(resolved, allowed) for allowed in roots):
        raise RootNotAllowed(
            f"root is outside the whitelist (INGEST_LOCAL_ROOTS): {raw}"
        )
    if not resolved.is_dir():
        raise RootMissing(f"root is not a directory: {raw}")
    return resolved


# --------------------------------------------------------------------------- #
# matching
# --------------------------------------------------------------------------- #
def _match_parts(relative: tuple[str, ...], pattern: tuple[str, ...]) -> bool:
    """Glob match where ``**`` stands for zero or more path segments."""
    if not pattern:
        return not relative
    head, rest = pattern[0], pattern[1:]
    if head == "**":
        if _match_parts(relative, rest):
            return True
        return bool(relative) and _match_parts(relative[1:], pattern)
    if not relative:
        return False
    # 大小写不敏感是**契约**，不是平台巧合：``fnmatch`` 只在 Windows 上经
    # ``os.path.normcase`` 折叠大小写，Linux 上退化成区分大小写，于是同一份
    # ``**/*.pdf`` 在 Linux 上匹配不到 ``d.PDF``（2026-10-10 修）。
    if not fnmatchcase(relative[0].casefold(), head.casefold()):
        return False
    return _match_parts(relative[1:], rest)


def matches_glob(relative: str, pattern: str) -> bool:
    """Whether a POSIX-style relative path matches ``pattern``.

    Matching is case-insensitive on every platform on purpose: ``*.pdf`` must
    match ``D.PDF``. That used to ride on ``fnmatch`` + ``os.path.normcase``,
    which only folds case on Windows -- on Linux the same pattern silently
    stopped matching, so the comparison is now explicit (``fnmatchcase`` on
    casefolded strings). ``scripts/bulk_ingest_dir.py`` keeps a standalone copy
    of this matcher; the two are asserted to agree in
    ``tests/test_bulk_ingest_dir.py``.
    """
    return _match_parts(
        PurePosixPath(relative).parts, PurePosixPath(pattern).parts
    )


def is_skippable(name: str) -> bool:
    """Hidden files, editor leftovers and partial downloads are not candidates."""
    if not name or name.startswith("."):
        return True
    lowered = name.lower()
    return any(lowered.endswith(suffix) for suffix in TEMP_SUFFIXES)


def is_link(path: str | os.PathLike[str]) -> bool:
    """Whether a path is a symlink -- or a Windows directory junction.

    ``os.walk(followlinks=False)`` still descends into junctions (they are not
    reported as symlinks by ``lstat``), and a junction is the one reparse point
    an unprivileged user can create on Windows. Both are therefore treated as
    links and never walked into or read through.
    """
    candidate = Path(path)
    if candidate.is_symlink():
        return True
    isjunction = getattr(os.path, "isjunction", None)
    return bool(isjunction is not None and isjunction(candidate))


def _walk(root: Path, recursive: bool):
    """Yield ``(path, relative)`` for candidate files, never following links."""
    root = Path(root)
    if not recursive:
        try:
            entries = sorted(os.scandir(root), key=lambda item: item.name)
        except OSError:  # pragma: no cover - the root was checked already
            return
        for entry in entries:
            if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                continue
            if is_skippable(entry.name):
                continue
            yield Path(entry.path), entry.name
        return

    for current, dirnames, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        # ``os.walk`` lists linked directories in ``dirnames``; dropping them
        # here is what keeps the walk inside the whitelisted root.
        dirnames[:] = sorted(
            name
            for name in dirnames
            if not name.startswith(".")
            and name.lower() not in SKIP_DIRS
            and not is_link(current_path / name)
        )
        for name in sorted(filenames):
            if is_skippable(name):
                continue
            path = current_path / name
            if is_link(path):
                continue
            relative = path.relative_to(root).as_posix()
            yield path, relative


def scan(
    root: Path,
    *,
    glob: str = DEFAULT_GLOB,
    recursive: bool = True,
    limit: int = 2000,
    max_bytes: int | None = None,
    hash_file=None,
) -> ScanResult:
    """Walk ``root`` and return the candidates that match ``glob``.

    Files are hashed here (streaming, one pass) because the caller needs the
    digest to drop duplicates before creating any job; the ``hash_file`` hook
    exists so tests can simulate an unreadable file. Sizes are checked *before*
    hashing, so an oversized file costs nothing.
    """
    hasher = hash_file or paper_service.compute_sha256_file
    result = ScanResult(root=Path(root), glob=glob, recursive=recursive)
    for path, relative in _walk(Path(root), recursive):
        if not matches_glob(relative, glob):
            continue
        if len(result.files) >= max(1, int(limit)):
            result.skipped += 1
            continue
        try:
            size = path.stat().st_size
        except OSError as exc:
            result.files.append(
                ScannedFile(
                    path=path,
                    relative=relative,
                    size_bytes=0,
                    reason=f"could not stat the file: {exc}",
                    error_code="INTERNAL",
                )
            )
            continue
        if max_bytes is not None and size > max_bytes:
            result.files.append(
                ScannedFile(
                    path=path,
                    relative=relative,
                    size_bytes=size,
                    reason=(
                        f"file too large: {size} bytes exceeds {max_bytes} bytes"
                    ),
                    error_code="OVERSIZED",
                )
            )
            continue
        if size == 0:
            result.files.append(
                ScannedFile(
                    path=path,
                    relative=relative,
                    size_bytes=0,
                    reason="local file is empty",
                    error_code="UNSUPPORTED_TYPE",
                )
            )
            continue
        try:
            digest = hasher(path)
        except OSError as exc:
            result.files.append(
                ScannedFile(
                    path=path,
                    relative=relative,
                    size_bytes=size,
                    reason=f"could not read the file: {exc}",
                    error_code="INTERNAL",
                )
            )
            continue
        result.files.append(
            ScannedFile(
                path=path,
                relative=relative,
                size_bytes=size,
                sha256=digest,
            )
        )
    logger.info(
        "directory scan finished",
        extra={
            "extra_fields": {
                "root": str(root),
                "glob": glob,
                "matched": result.matched,
                "scanned": len(result.files),
                "skipped": result.skipped,
            }
        },
    )
    return result


def digest_for(path: Path) -> str:
    """SHA256 of a scanned file (streaming, never buffered)."""
    return paper_service.compute_sha256_file(path)


__all__ = [
    "DEFAULT_GLOB",
    "LocalScanError",
    "RootMissing",
    "RootNotAllowed",
    "SKIP_DIRS",
    "ScanResult",
    "ScanUnavailable",
    "ScannedFile",
    "TEMP_SUFFIXES",
    "digest_for",
    "ensure_allowed",
    "is_link",
    "is_skippable",
    "is_within",
    "matches_glob",
    "scan",
]
