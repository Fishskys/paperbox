"""Conservative cleanup of application log files and owned object prefixes."""

from datetime import timedelta
import os
from pathlib import Path
import re
import stat
from uuid import UUID

from sqlalchemy import select

from app.core.config import REPO_ROOT, settings
from app.db.models import Paper
from app.services import cleanup_policy as policy, object_storage

LOG_ROOT = REPO_ROOT / "logs" / "app"


def file_identity(value):
    return [value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns]


def open_log(value):
    """Unknown ownership is protected; never unlink a currently open log."""
    proc = Path("/proc")
    if not proc.is_dir():
        return True
    for process in proc.iterdir():
        if not process.name.isdigit():
            continue
        try:
            if process.stat().st_uid != os.getuid():
                continue
            for descriptor in (process / "fd").iterdir():
                try:
                    found = descriptor.stat()
                    if (found.st_dev, found.st_ino) == (value.st_dev, value.st_ino):
                        return True
                except FileNotFoundError:
                    continue
        except FileNotFoundError:
            continue
        except PermissionError:
            return True
    return False


def log_snapshot(name):
    if Path(name).name != name or not re.fullmatch(r".+\.log(?:\.\d+)?(?:\.gz)?", name):
        raise ValueError("not a registered application log name")
    if LOG_ROOT.resolve() != LOG_ROOT.absolute():
        raise ValueError("log directory must not contain symlinks")
    path = LOG_ROOT / name
    value = path.lstat()
    if not stat.S_ISREG(value.st_mode):
        raise ValueError("not a regular log file")
    return value


def scan_logs(moment):
    if not LOG_ROOT.is_dir():
        return
    cutoff = (moment - timedelta(days=policy.policy()["days"]["app_logs"])).timestamp()
    for path in sorted(LOG_ROOT.iterdir()):
        if not re.fullmatch(r".+\.log(?:\.\d+)?(?:\.gz)?", path.name):
            continue
        try:
            value = log_snapshot(path.name)
            if value.st_mtime >= cutoff:
                continue
            reason = "log file is open or ownership cannot be established" if open_log(value) else None
            yield {"category": "app_logs", "resource_key": path.name, "revision": policy.digest(file_identity(value)),
                "detail": {"modified_at": value.st_mtime}, "size_bytes": value.st_size,
                "state": "protected" if reason else "ready", "error": reason}
        except (ValueError, OSError):
            yield {"category": "app_logs", "resource_key": path.name, "revision": "unknown", "detail": {},
                "size_bytes": None, "state": "protected", "error": "file identity cannot be verified"}


def remove_log(candidate):
    value = log_snapshot(candidate.resource_key)
    if policy.digest(file_identity(value)) != candidate.revision or open_log(value):
        raise ValueError("log changed or is open")
    directory = os.open(LOG_ROOT, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        current = os.stat(candidate.resource_key, dir_fd=directory, follow_symlinks=False)
        if file_identity(current) != file_identity(value):
            raise ValueError("log changed before deletion")
        os.unlink(candidate.resource_key, dir_fd=directory)
    finally:
        os.close(directory)


def object_info(item):
    return {"key": item.object_name, "etag": getattr(item, "etag", None), "size": getattr(item, "size", None),
        "modified": str(getattr(item, "last_modified", None))}


def versioning(store, bucket):
    if store is object_storage:
        return store.get_client().get_bucket_versioning(bucket).status
    return store.versioning(bucket)


def scan_objects(session, store, category, moment):
    bucket = settings.minio_bucket
    versioned = versioning(store, bucket) in ("Enabled", "Suspended")
    cutoff = moment - timedelta(days=policy.policy()["days"][category])
    groups = {}
    live = {}
    scanned = 0
    for item in store.list_objects(prefix="papers/", bucket=bucket):
        scanned += 1
        if scanned > settings.cleanup_scan_limit:
            raise ScanLimit("object scan reached its configured bound")
        parts = item.object_name.split("/")
        try:
            paper_id = str(UUID(parts[1]))
        except (ValueError, IndexError):
            continue
        if category == "parse_cache":
            if parts[2:4] != ["extracted", "parsed"]:
                continue
            key = paper_id
        else:
            if paper_id not in live:
                live[paper_id] = session.get(Paper, paper_id) is not None
            if live[paper_id]:
                continue
            key = item.object_name
        modified = getattr(item, "last_modified", None)
        info = object_info(item)
        entry = groups.setdefault(key, {"objects": [], "paper_id": paper_id, "fresh": False})
        entry["objects"].append(info)
        if modified is None or policy.aware(modified) >= cutoff:
            entry["fresh"] = True
    for key, entry in groups.items():
        if entry["fresh"]:
            continue
        objects = sorted(entry["objects"], key=lambda value: value["key"])
        reason = "versioned bucket requires separate version cleanup" if versioned else None
        yield {"category": category, "resource_key": key, "revision": policy.digest(objects),
            "detail": {"objects": objects, "paper_id": entry["paper_id"], "bucket": bucket},
            "size_bytes": sum(obj["size"] for obj in objects) if all(obj["size"] is not None for obj in objects) else None,
            "state": "protected" if reason else "ready", "error": reason}


ScanLimit = policy.ScanLimit


def remove_objects(session, store, candidate):
    detail = dict(candidate.detail)
    bucket, objects = detail["bucket"], detail["objects"]
    if versioning(store, bucket) in ("Enabled", "Suspended"):
        raise ValueError("bucket versioning changed")
    if candidate.category == "orphan_objects" and session.get(Paper, detail["paper_id"]) is not None:
        raise ValueError("object prefix now belongs to a paper")
    expected = {item["key"]: item for item in objects}
    if candidate.category == "parse_cache":
        actual = [object_info(item) for item in store.list_objects(prefix=f'papers/{detail["paper_id"]}/extracted/parsed/', bucket=bucket)]
    else:
        actual = [object_info(item) for item in store.list_objects(prefix=candidate.resource_key, bucket=bucket) if item.object_name == candidate.resource_key]
    if any(item != expected.get(item["key"]) for item in actual):
        raise ValueError("stored object changed after preview")
    if not detail.get("started") and {item["key"] for item in actual} != set(expected):
        raise ValueError("stored objects changed after preview")
    detail["started"] = True
    candidate.detail = detail
    session.flush()
    for item in actual:
        store.delete_object(item["key"], bucket=bucket)
