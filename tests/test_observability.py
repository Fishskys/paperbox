"""No real runtimes, host processes, credentials or external dependencies."""

from datetime import datetime, timezone
from ipaddress import IPv4Address
import json
import os
import subprocess

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy.engine import URL

from app.api import observability
from app.core.config import settings
from app.core.security import require_admin
from app.schemas.observability import LogFilters
from app.services import runtime_metrics as metrics, system_logs as logs
from app.services.api_key_service import AuthIdentity


@pytest.fixture()
def logfile(tmp_path, monkeypatch):
    monkeypatch.setattr(logs, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(settings, "log_file", "logs/app/paperbox.log")
    monkeypatch.setattr(settings, "log_backup_count", 2)
    monkeypatch.setattr(settings, "system_log_scan_bytes", 8 * 1024 * 1024)
    path = tmp_path / "logs/app/paperbox.log"
    path.parent.mkdir(parents=True)
    return path


def line(message="hello", *, level="INFO", second=0, request="r1"):
    return f"2026-11-06T10:00:{second:02}+0800 {level:<8} app.test [req={request}] [key=admin] {message}\n"


def test_logs_rotations_multiline_json_filters_and_export(logfile):
    logfile.with_name("paperbox.log.1").write_text(line("old", second=1))
    logfile.write_text(line("Failure", level="ERROR", second=2) + "Traceback:\n  detail\n" + json.dumps({
        "ts": "2026-11-06T10:00:03+0800", "level": "INFO", "logger": "app.json",
        "message": "new", "request_id": "r2", "extra": 5,
    }) + "\n")
    result = logs.query(LogFilters(limit=1))
    assert result.total == 3 and result.entries[0].message == 'new {"extra": 5}'
    assert len(logs.query(LogFilters(limit=1), export=True).entries) == 3
    found = logs.query(LogFilters(level="ERROR", q="detail", logger="APP.TEST", request_id="r1",
        since=datetime(2026, 11, 6, 2, 0, 2, tzinfo=timezone.utc), until=datetime(2026, 11, 6, 2, 0, 2, tzinfo=timezone.utc)))
    assert found.total == 1 and "Traceback:" in found.entries[0].message
    assert logs.query(LogFilters(offset=2)).entries[0].message == "old"
    assert logs.query(LogFilters(request_id="r")).total == 0


def test_logs_budget_reports_partial_scope_and_never_returns_partial_record(logfile, monkeypatch):
    logfile.write_text(line("x" * 2000) + line("tail", second=5))
    logfile.with_name("paperbox.log.1").write_text(line("older"))
    monkeypatch.setattr(settings, "system_log_scan_bytes", 1024)
    result = logs.query(LogFilters())
    assert result.truncated and result.scanned_bytes == 1024
    assert [entry.message for entry in result.entries] == ["tail"]
    assert logs.query(LogFilters(), export=True).truncated


def test_logs_do_not_follow_symlinks_or_read_unconfigured_files(logfile, monkeypatch):
    outside = logfile.parents[2] / "private"
    outside.write_text(line("secret outside"))
    logfile.symlink_to(outside)
    assert not logs.query(LogFilters()).available
    monkeypatch.setattr(settings, "log_file", str(outside))
    assert not logs.query(LogFilters()).available
    monkeypatch.setattr(settings, "log_file", "")
    assert not logs.query(LogFilters()).available


def test_missing_and_empty_log_are_distinct(logfile):
    assert not logs.query(LogFilters()).available
    logfile.touch()
    result = logs.query(LogFilters())
    assert result.available and result.total == 0 and not result.truncated


def test_logs_redact_configured_and_shaped_credentials_before_search(logfile, monkeypatch):
    monkeypatch.setattr(settings, "paper_api_key", "fake-private-credential")
    monkeypatch.setattr(settings, "paper_api_keys", "test:fake-bootstrap-key")
    dsn = URL.create("postgresql", username="test", password="fake-db-password", host="db").render_as_string(hide_password=False)
    logfile.write_text(line('Bearer fake-bearer Authorization API_KEY="fake-key" password=private '
        f'{dsn} fake-private-credential fake-bootstrap-key'))
    text = logs.query(LogFilters()).model_dump_json()
    for value in ("fake-bearer", "fake-key", "fake-db-password", "fake-private-credential", "fake-bootstrap-key", "password=private"):
        assert value not in text
    assert logs.query(LogFilters(q="fake-private-credential")).total == 0


@pytest.fixture()
def api():
    app = FastAPI()
    app.include_router(observability.router)
    return app, TestClient(app)


def test_query_validation_and_export_do_not_truncate_to_page(api, logfile):
    app, client = api
    app.dependency_overrides[require_admin] = lambda: None
    logfile.write_text("".join(line(str(i), second=i) for i in range(4)))
    base = "/api/admin/observability/logs"
    assert client.get(base + "?limit=1").json()["total"] == 4
    result = client.get(base + "/export?offset=2&limit=1")
    assert len(result.json()["entries"]) == 4
    assert result.headers["cache-control"] == "no-store"
    assert "attachment" in result.headers["content-disposition"]
    for query in ("limit=201", "offset=-1", "level=WRONG", "since=2026-11-06T00:00:00",
        "since=2026-11-07T00:00:00Z&until=2026-11-06T00:00:00Z"):
        assert client.get(base + "?" + query).status_code == 422


@pytest.mark.parametrize("path", ["memory", "logs", "logs/export"])
def test_anonymous_cannot_read_operational_data(api, monkeypatch, path):
    _, client = api
    monkeypatch.setattr(settings, "auth_enabled", True)
    response = client.get("/api/admin/observability/" + path)
    assert response.status_code == 401


@pytest.mark.parametrize("role", ["read", "write"])
@pytest.mark.parametrize("path", ["memory", "logs", "logs/export"])
def test_lower_roles_cannot_read_operational_data(api, monkeypatch, role, path):
    app, client = api
    monkeypatch.setattr(settings, "auth_enabled", True)
    @app.middleware("http")
    async def identity(request, call_next):
        request.state.auth_identity = AuthIdentity(prefix="test", role=role, source="db", is_anonymous=False)
        request.state.auth_token_present = True
        return await call_next(request)
    assert client.get("/api/admin/observability/" + path).status_code == 403


def process(root, pid, ppid, rss, pss=None):
    folder = root / str(pid)
    folder.mkdir()
    (folder / "stat").write_text(f"{pid} (process with ) brackets) S {ppid} 0 0")
    (folder / "status").write_text(f"Name:\tprocess\nVmRSS:\t{rss} kB\n")
    if pss is not None:
        (folder / "smaps_rollup").write_text(f"000-fff ---p [rollup]\nPss: {pss} kB\n")


def test_memory_counts_descendants_and_does_not_substitute_virtual_memory(tmp_path, monkeypatch):
    monkeypatch.setattr(metrics, "_PROC", tmp_path)
    process(tmp_path, 100, 1, 100, 80)
    process(tmp_path, 101, 100, 60, 50)
    process(tmp_path, 102, 1, 9999, 9999)
    result = metrics._memory(100, metrics._process_tree(), source="container")
    assert result.rss_bytes == 160 * 1024 and result.pss_bytes == 130 * 1024
    assert result.processes == 2 and not result.partial
    (tmp_path / "101/smaps_rollup").unlink()
    result = metrics._memory(100, metrics._process_tree(), source="container")
    assert result.pss_bytes is None and result.rss_bytes == 160 * 1024


def test_runtime_missing_remote_and_shared_containers(tmp_path, monkeypatch):
    monkeypatch.setattr(metrics, "_PROC", tmp_path)
    process(tmp_path, os.getpid(), 1, 100, 90)
    process(tmp_path, 42, 1, 300, 200)
    monkeypatch.setattr(metrics, "_containers", lambda: {"paperbox-embedding": 42})
    monkeypatch.setattr(settings, "embedding_url", "http://127.0.0.1:8090")
    monkeypatch.setattr(settings, "rerank_url", "http://127.0.0.1:8090")
    monkeypatch.setattr(settings, "opensearch_url", "http://remote.test:9200")
    result = metrics.snapshot()
    assert result.backend.rss_bytes == 100 * 1024
    assert result.dependencies["embedding"] == result.dependencies["rerank"]
    assert result.dependencies["embedding"].rss_bytes == 300 * 1024
    assert result.dependencies["opensearch"].rss_bytes is None
    assert result.dependencies["postgres"].source == "unavailable"


def test_podman_json_process_listing_avoids_broken_inspect(monkeypatch):
    monkeypatch.setattr(settings, "system_container_runtime", "podman")
    monkeypatch.setattr(metrics.shutil, "which", lambda _: "/bin/podman")
    monkeypatch.delenv("CONTAINER_HOST", raising=False)
    monkeypatch.delenv("CONTAINER_CONNECTION", raising=False)
    calls = []
    def run(arguments):
        calls.append(arguments)
        return json.dumps([{"Names": ["paperbox-postgres"], "Pid": 123}])
    monkeypatch.setattr(metrics, "_run", run)
    assert metrics._containers() == {"paperbox-postgres": 123}
    assert calls == [["podman", "ps", "--format", "json"]]
    monkeypatch.setattr(metrics, "_run", lambda _: (_ for _ in ()).throw(subprocess.TimeoutExpired("podman", 3)))
    assert metrics._containers() == {}
    monkeypatch.setenv("CONTAINER_HOST", "ssh://remote.test")
    monkeypatch.setattr(metrics, "_run", lambda _: pytest.fail("must not use remote runtimes"))
    assert metrics._containers() == {}


def test_runtime_follows_changed_container_mapping_and_pid_but_not_arbitrary_network_addresses(tmp_path, monkeypatch):
    monkeypatch.setattr(metrics, "_PROC", tmp_path)
    process(tmp_path, os.getpid(), 1, 100, 90)
    process(tmp_path, 456, 1, 300, 200)
    monkeypatch.setattr(metrics, "_containers", lambda: {"renamed-model-server": 456})
    monkeypatch.setattr(settings, "system_container_names", {"embedding": "renamed-model-server", "rerank": "renamed-model-server"})
    monkeypatch.setattr(settings, "embedding_url", "http://localhost:18090")
    monkeypatch.setattr(settings, "rerank_url", "http://127.0.0.1:18090")
    result = metrics.snapshot()
    assert result.dependencies["embedding"].container == "renamed-model-server"
    assert result.dependencies["embedding"].rss_bytes == 300 * 1024
    assert result.dependencies["rerank"] == result.dependencies["embedding"]
    addresses = [str(IPv4Address(bytes(parts))) for parts in ((192, 0, 2, 10), (172, 18, 0, 9))]
    for target in [*(f"http://{address}:8090" for address in addresses), "http://embedding:8090"]:
        monkeypatch.setattr(settings, "embedding_url", target)
        assert metrics.snapshot().dependencies["embedding"].source == "unavailable"


def test_runtime_without_local_engine_still_measures_backend(tmp_path, monkeypatch):
    monkeypatch.setattr(metrics, "_PROC", tmp_path)
    process(tmp_path, os.getpid(), 1, 100, 90)
    monkeypatch.setattr(metrics.shutil, "which", lambda _: None)
    result = metrics.snapshot()
    assert result.backend.rss_bytes == 100 * 1024
    assert all(value.source == "unavailable" and value.rss_bytes is None for value in result.dependencies.values())
