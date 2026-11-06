"""Bounded, read-only application log queries over the configured rotation family."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat

from app.core.config import REPO_ROOT, settings
from app.schemas.observability import LogEntryOut, LogFilters, LogsOut
from app.services.health_service import redact

_TEXT = re.compile(r"^(?P<ts>\d{4}-\d\d-\d\dT\S+)\s+(?P<level>DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+(?P<logger>\S+)\s+\[req=(?P<request_id>[^\]]*)\]\s+(?:\[key=(?P<key_prefix>[^\]]*)\]\s+)?(?P<message>.*)$")
_BEARER = re.compile(r"(?i)(bearer\s+)[\w.+/~=-]+")
_SECRET = re.compile(r'''(?i)((?:["']?)(?:api[_-]?key|password|secret|access_token|token)(?:["']?)\s*[:=]\s*)(?:"[^"]*"|'[^']*'|[^\s,;}]+)''')


def _redact(value: str) -> str:
    cleaned = redact(value)
    secrets = [getattr(settings, name) for name in type(settings).model_fields
        if any(word in name for word in ("password", "secret", "api_key"))]
    secrets.extend(settings.bootstrap_keys.values())
    for secret in sorted((item for item in secrets if isinstance(item, str) and item), key=len, reverse=True):
        cleaned = cleaned.replace(secret, "***")
    return _SECRET.sub(r"\1***", _BEARER.sub(r"\1***", cleaned))


def _record(line: str) -> dict | None:
    match = _TEXT.match(line)
    if match:
        return match.groupdict()
    if line.startswith("{"):
        try:
            value = json.loads(line)
            if not isinstance(value, dict) or not {"ts", "level", "message"} <= value.keys():
                return None
            extra = {key: item for key, item in value.items() if key not in {"ts", "level", "logger", "message", "request_id", "key_prefix"}}
            return {**value, "message": str(value["message"]) + (" " + json.dumps(extra, ensure_ascii=False) if extra else "")}
        except ValueError:
            pass
    return None


def _entries(data: bytes, name: str, start: int) -> list[LogEntryOut]:
    records = []
    current = None
    for index, line in enumerate(data.decode("utf-8", errors="replace").splitlines()):
        parsed = _record(line)
        if parsed:
            try:
                stamp = datetime.fromisoformat(str(parsed["ts"]))
                if stamp.tzinfo is None:
                    continue
                current = LogEntryOut(id=f"{name}:{start}:{index}", timestamp=stamp,
                    level=str(parsed["level"]).strip().upper(), logger=str(parsed.get("logger") or ""),
                    request_id=parsed.get("request_id"), key_prefix=parsed.get("key_prefix"),
                    message=str(parsed["message"]))
                records.append(current)
            except (ValueError, TypeError):
                current = None
        elif current is not None:
            current.message += "\n" + line
    for record in records:
        record.message = _redact(record.message)
        record.logger = _redact(record.logger)
        record.request_id = _redact(record.request_id) if record.request_id else None
        record.key_prefix = _redact(record.key_prefix) if record.key_prefix else None
    return records


def query(filters: LogFilters, *, export: bool = False) -> LogsOut:
    result = LogsOut(offset=filters.offset, limit=filters.limit,
        scan_limit_bytes=settings.system_log_scan_bytes, measured_at=datetime.now(timezone.utc))
    if not settings.log_file:
        return result.model_copy(update={"available": False, "reason": "未配置应用文件日志（LOG_FILE），暂无可读取日志"})
    path = REPO_ROOT / settings.log_file
    root = REPO_ROOT / "logs" / "app"
    if path.parent.resolve() != root.absolute() or path.is_symlink():
        return result.model_copy(update={"available": False, "reason": "日志文件不在允许的应用日志目录内"})
    remaining, records = settings.system_log_scan_bytes, []
    readable = False
    for number in range(settings.log_backup_count + 1):
        candidate = path if number == 0 else path.with_name(f"{path.name}.{number}")
        try:
            fd = os.open(candidate, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
            with os.fdopen(fd, "rb") as handle:
                info = os.fstat(handle.fileno())
                if not stat.S_ISREG(info.st_mode):
                    result.truncated = True
                    continue
                readable = True
                take = min(remaining, info.st_size)
                if take < info.st_size:
                    result.truncated = True
                if not take:
                    continue
                start = info.st_size - take
                handle.seek(start)
                data = handle.read(take)
                remaining -= len(data)
                result.scanned_bytes += len(data)
                if start:
                    # A suffix can start in the middle of a UTF-8 character or record.
                    data = data.partition(b"\n")[2]
                records.extend(_entries(data, candidate.name, start))
        except FileNotFoundError:
            continue
        except OSError:
            result.truncated = True
            result.reason = "部分轮转文件无法读取，结果可能不完整"
    if not readable:
        result.available = False
        result.reason = "日志文件不存在或不可读取"
    selected = []
    for entry in records:
        if filters.level and entry.level != filters.level:
            continue
        if filters.logger.casefold() not in entry.logger.casefold():
            continue
        if filters.request_id and filters.request_id != entry.request_id:
            continue
        if filters.since and entry.timestamp < filters.since:
            continue
        if filters.until and entry.timestamp > filters.until:
            continue
        text = f"{entry.message} {entry.logger} {entry.request_id or ''} {entry.key_prefix or ''}"
        if filters.q.casefold() not in text.casefold():
            continue
        selected.append(entry)
    selected.sort(key=lambda item: item.timestamp, reverse=True)
    result.total = len(selected)
    result.entries = selected if export else selected[filters.offset:filters.offset + filters.limit]
    if export:
        result.offset, result.limit = 0, len(selected)
    return result
