#!/usr/bin/env python3
"""Health check for paperbox and every dependency it talks to.

    uv run python scripts/healthcheck.py

Exits non-zero when a required dependency is unreachable. Prints one line per
service plus a summary line.
"""
from __future__ import annotations

import re
import socket
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
ENV = ROOT / ".env"


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def host_port(url: str) -> tuple[str, int]:
    match = re.match(r"^(?:https?://)?\[?([^\]/:]+)\]?(?::(\d+))?", url)
    if match is None:
        raise ValueError(f"cannot parse host from {url!r}")
    host = match.group(1)
    port = int(match.group(2) or 80)
    return host, port


def check_tcp(name: str, host: str, port: int, timeout: float = 5.0) -> bool:
    started = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            pass
    except OSError as exc:
        print(f"  FAIL {name:<12} {host}:{port}  {exc}")
        return False
    took = (time.perf_counter() - started) * 1000
    print(f"  ok   {name:<12} {host}:{port}  ({took:.0f} ms)")
    return True


def check_http(name: str, url: str, path: str = "", timeout: float = 10.0) -> bool:
    target = f"{url.rstrip('/')}{path}"
    started = time.perf_counter()
    try:
        response = httpx.get(target, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - report and continue
        print(f"  FAIL {name:<12} {target}  {exc}")
        return False
    took = (time.perf_counter() - started) * 1000
    if response.status_code >= 400:
        print(f"  FAIL {name:<12} {target}  HTTP {response.status_code}")
        return False
    print(f"  ok   {name:<12} {target}  HTTP {response.status_code} ({took:.0f} ms)")
    return True


def main() -> int:
    env = load_env(ENV)
    if not env:
        print(f"WARN: {ENV} not found - falling back to localhost defaults")

    pg_host, pg_port = host_port(env.get("POSTGRES_DSN", "postgres://...@localhost:5432/paperbox").split("@")[-1])
    os_url = env.get("OPENSEARCH_URL", "http://localhost:9200")
    minio = env.get("MINIO_ENDPOINT", "localhost:9000")
    minio_host, minio_port = host_port(minio if "://" in minio else f"http://{minio}")
    emb_url = env.get("EMBEDDING_URL", "http://localhost:8090")
    app_port = env.get("PAPER_API_PORT", "8077")
    index = env.get("OPENSEARCH_ALIAS", "paper_chunks_current")

    print("paperbox healthcheck")
    print(f"  config {ENV}")
    ok = True
    ok &= check_tcp("postgres", pg_host, pg_port)
    ok &= check_http("opensearch", os_url, "/_cluster/health")
    ok &= check_http("minio", f"http://{minio_host}:{minio_port}", "/minio/health/live")
    ok &= check_http("embedding", emb_url, "/health")
    ok &= check_http("api", f"http://127.0.0.1:{app_port}", "/health")

    try:
        count = httpx.get(f"{os_url.rstrip('/')}/{index}/_count", timeout=10).json().get("count")
        print(f"  info {index} documents: {count}")
    except Exception as exc:  # noqa: BLE001
        print(f"  warn could not count {index}: {exc}")

    # Deployment-state objects: the native hybrid backend needs ``paperbox-rrf60``
    # in the cluster, and a drifted body would silently change how a hybrid query
    # is fused. Warn-only: the default ``python`` backend does not read them, so a
    # missing pipeline must not fail a healthcheck of a working deployment.
    try:
        sys.path.insert(0, str(ROOT))
        from app.search.native import ensure_pipelines

        report = ensure_pipelines(dry_run=True)
        drifted = [item["name"] for item in report if item["changed"]]
        if drifted:
            print(
                f"  warn search pipelines drifted: {', '.join(drifted)} "
                "(run scripts/ensure_search_pipelines.py)"
            )
        else:
            print(f"  info search pipelines: {len(report)} present, in sync")
    except Exception as exc:  # noqa: BLE001
        print(f"  warn could not check search pipelines: {exc}")

    print("RESULT:", "all dependencies reachable" if ok else "one or more dependencies failed")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
