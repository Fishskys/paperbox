"""Best-effort local Linux process measurements; no engine socket or privilege escalation."""

from datetime import datetime, timezone
import ipaddress
import json
import os
from pathlib import Path
import shutil
import subprocess
from urllib.parse import urlsplit

from app.core.config import settings
from app.schemas.observability import MemoryOut, RuntimeOut

_PROC = Path("/proc")


def _local(target: str) -> bool:
    try:
        host = urlsplit(target if "://" in target else f"http://{target}").hostname or ""
        if host in {"localhost", "host.docker.internal", "host.containers.internal", "::1"}:
            return True
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _run(arguments: list[str]) -> str:
    result = subprocess.run(arguments, capture_output=True, text=True,
        timeout=settings.system_metrics_timeout_s, check=True)
    if len(result.stdout) > 1024 * 1024:
        raise ValueError("container listing exceeds measurement bound")
    return result.stdout


def _containers() -> dict[str, int]:
    choice = settings.system_container_runtime
    runtimes = ("podman", "docker") if choice == "auto" else (choice,)
    for runtime in runtimes:
        if runtime == "disabled" or not shutil.which(runtime):
            continue
        try:
            if runtime == "podman" and (os.environ.get("CONTAINER_HOST") or os.environ.get("CONTAINER_CONNECTION")):
                continue
            if runtime == "docker":
                host = os.environ.get("DOCKER_HOST") or json.loads(
                    _run([runtime, "context", "inspect", "--format", "{{json .Endpoints.docker.Host}}"])
                )
                if not host.startswith("unix://"):
                    continue
            output = _run([runtime, "ps", "--format", "json"]).strip()
            rows = json.loads(output) if output.startswith("[") else [json.loads(line) for line in output.splitlines()]
            found = {}
            wanted = set(settings.system_container_names.values())
            for row in rows:
                names = row.get("Names", [])
                if isinstance(names, str):
                    names = names.split(",")
                for name in names:
                    if name not in wanted:
                        continue
                    pid = row.get("Pid")
                    if pid is None and runtime == "docker":
                        pid = json.loads(_run([runtime, "inspect", "--format", "{{json .State.Pid}}", row["ID"]]))
                    if int(pid or 0) > 0:
                        found[name] = int(pid)
            if found:
                return found
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
            continue
    return {}


def _process_tree() -> dict[int, list[int]]:
    children: dict[int, list[int]] = {}
    for path in _PROC.iterdir():
        if not path.name.isdecimal():
            continue
        try:
            # comm may contain spaces and closing parentheses; PPID follows the last ')'.
            fields = (path / "stat").read_text().rsplit(")", 1)[1].split()
            children.setdefault(int(fields[1]), []).append(int(path.name))
        except (OSError, ValueError, IndexError):
            continue
    return children


def _memory(pid: int, tree: dict[int, list[int]], *, source: str, container: str | None = None) -> MemoryOut:
    pending, seen = [pid], set()
    rss, pss, measured, pss_complete = 0, 0, 0, True
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        pending.extend(tree.get(current, []))
        try:
            values = dict(line.split(":", 1) for line in (_PROC / str(current) / "status").read_text().splitlines() if ":" in line)
            rss += int(values["VmRSS"].split()[0]) * 1024
            measured += 1
        except (OSError, KeyError, ValueError):
            pss_complete = False
            continue
        try:
            fields = dict(line.split(":", 1) for line in (_PROC / str(current) / "smaps_rollup").read_text().splitlines() if ":" in line)
            pss += int(fields["Pss"].split()[0]) * 1024
        except (OSError, KeyError, ValueError):
            pss_complete = False
    return MemoryOut(rss_bytes=rss if measured else None,
        pss_bytes=pss if measured and pss_complete else None, processes=measured,
        partial=measured < len(seen), source=source, container=container,
        reason=None if measured else "无权限读取本地进程内存，或进程已退出")


def snapshot() -> RuntimeOut:
    targets = {"postgres": settings.postgres_dsn, "opensearch": settings.opensearch_url,
        "minio": settings.minio_endpoint, "embedding": settings.embedding_url,
        "rerank": settings.rerank_url, "docling": settings.docling_url}
    backend = MemoryOut(reason="当前平台不支持进程内存采集")
    dependencies = {name: MemoryOut(reason="远程或未配置的依赖，未采集本地内存") for name in targets}
    if _PROC.is_dir():
        tree = _process_tree()
        backend = _memory(os.getpid(), {}, source="backend")
        containers = _containers()
        cache = {}
        for name, target in targets.items():
            if not target or not _local(target):
                continue
            container = settings.system_container_names.get(name, "")
            pid = containers.get(container)
            if not pid:
                dependencies[name] = MemoryOut(reason="未找到可采集的本地容器，或运行时不可用")
                continue
            if container not in cache:
                cache[container] = _memory(pid, tree, source="container", container=container)
            dependencies[name] = cache[container]
    return RuntimeOut(measured_at=datetime.now(timezone.utc), backend=backend, dependencies=dependencies)
