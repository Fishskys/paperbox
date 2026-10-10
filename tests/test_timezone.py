"""全栈时区口径：Asia/Shanghai（2026-10-10）。

这条口径有三个消费者，任何一处退回 UTC 都会重新出现"时间不对"的排查：
应用日志（``%z`` 偏移）、``.env`` 里不带偏移的时间戳、以及**依赖容器的 psql 直连**。
前两个由 ``app/core/config.py`` 导入期的 ``TZ`` + ``time.tzset()`` 保证，
第三个由 ``infra/docker-compose.yml`` 每个服务的 ``TZ``（Postgres 另加 ``PGTZ``）保证 ——
所以这个文件既测进程，也静态守卫那份 compose。
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta
from pathlib import Path

import app.core.config  # noqa: F401  -- 导入期设定进程时区，测的就是这个副作用

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE = REPO_ROOT / "infra" / "docker-compose.yml"
NAS_COMPOSE = REPO_ROOT / "infra" / "docling" / "fnos" / "docker-compose.yml"


def test_the_process_timezone_defaults_to_shanghai() -> None:
    assert os.environ.get("TZ") == "Asia/Shanghai"


def test_local_time_is_utc_plus_eight() -> None:
    """`tzset()` 真的生效了：本地渲染与 UTC 相差 8 小时。"""
    assert datetime.now().astimezone().utcoffset() == timedelta(hours=8)
    if hasattr(time, "timezone"):  # POSIX：时区西偏秒数
        assert time.timezone == -8 * 3600


def test_log_timestamps_carry_the_offset() -> None:
    """日志里必须能看出偏移 —— 没有偏移的时间戳是"时间不对"的源头。"""
    stamp = datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")

    assert stamp.endswith("+0800"), stamp


def test_every_compose_service_declares_the_timezone() -> None:
    """compose 里五个服务一个都不能漏（漏的那个就是下次的排查对象）。"""
    import yaml

    services = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]
    assert set(services) >= {"postgres", "opensearch", "minio", "embedding", "docling"}

    for name, service in services.items():
        env = service.get("environment") or []
        if isinstance(env, list):
            blob = "\n".join(env)
        else:
            blob = "\n".join(f"{k}={v}" for k, v in env.items())
        assert "TZ=" in blob or "TZ:" in blob or "TZ" in [e.split("=")[0] for e in env], (
            f"{name} 没有设 TZ"
        )

    # Postgres 额外要 PGTZ：容器内的 libpq 客户端（psql）别又退回 UTC
    pg = services["postgres"]["environment"]
    assert "PGTZ" in pg, "postgres 少了 PGTZ，docker exec psql 会看到 UTC"


def test_the_nas_compose_declares_it_too() -> None:
    """NAS 上那份 docling compose 别被漏掉（同一套口径）。"""
    text = NAS_COMPOSE.read_text(encoding="utf-8")
    assert "TZ" in text
