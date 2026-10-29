"""Disposable PostgreSQL/MinIO acceptance; never uses the developer's services."""

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import time
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def command(args, **kwargs):
    return subprocess.check_output(args, text=True, **kwargs).strip()


def supervise(pdf):
    names = [
        "paperbox-figure-pg-" + uuid.uuid4().hex[:8],
        "paperbox-figure-minio-" + uuid.uuid4().hex[:8],
    ]
    password = secrets.token_urlsafe(24)
    environment = dict(
        os.environ,
        POSTGRES_PASSWORD=password,
        MINIO_ROOT_USER="figuretest",
        MINIO_ROOT_PASSWORD=password,
    )
    try:
        command(
            [
                "podman",
                "run",
                "-d",
                "--name",
                names[0],
                "-p",
                "127.0.0.1::5432",
                "-e",
                "POSTGRES_PASSWORD",
                "docker.io/library/postgres:15.2-alpine",
            ],
            env=environment,
        )
        command(
            [
                "podman",
                "run",
                "-d",
                "--name",
                names[1],
                "-p",
                "127.0.0.1::9000",
                "-e",
                "MINIO_ROOT_USER",
                "-e",
                "MINIO_ROOT_PASSWORD",
                "quay.io/minio/minio:RELEASE.2025-04-22T22-12-26Z",
                "server",
                "/data",
            ],
            env=environment,
        )
        pg_port = command(["podman", "port", names[0], "5432"]).rsplit(":", 1)[1]
        minio_port = command(["podman", "port", names[1], "9000"]).rsplit(":", 1)[1]
        environment.update(
            POSTGRES_DSN=f"postgresql+psycopg://postgres:{password}@127.0.0.1:{pg_port}/postgres",
            MINIO_ENDPOINT=f"127.0.0.1:{minio_port}",
            MINIO_ACCESS_KEY="figuretest",
            MINIO_SECRET_KEY=password,
            MINIO_SECURE="false",
            MINIO_BUCKET="figure-acceptance",
            AUTH_ENABLED="false",
            MCP_ENABLED="false",
            FIGURE_AUTO_EXTRACT="false",
            HEALTH_PROBE_ON_STARTUP="false",
            PDFFIGURES2_JAVA=os.environ.get(
                "PDFFIGURES2_JAVA", shutil.which("java") or "java"
            ),
        )
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            ready = subprocess.run(
                ["podman", "exec", names[0], "pg_isready", "-U", "postgres"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if ready.returncode == 0:
                break
            time.sleep(0.2)
        else:
            subprocess.run(["podman", "logs", names[0]], check=False)
            raise RuntimeError("isolated PostgreSQL failed to start")
        subprocess.run(
            [sys.executable, __file__, "--inside", "--pdf", str(pdf)],
            env=environment,
            cwd=ROOT,
            check=True,
            timeout=240,
        )
    finally:
        for name in reversed(names):
            subprocess.run(
                ["podman", "rm", "-f", name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )


def inside(pdf):
    from alembic import command as alembic
    from alembic.config import Config
    from fastapi.testclient import TestClient
    from sqlalchemy import inspect
    from app.db.session import get_engine, SessionLocal
    from app.db.models import Paper, PaperFile
    from app.services import (
        object_storage,
        embedding_service,
        api_key_service,
        health_service,
    )
    from app.workers import housekeeping
    from app.main import app

    engine = get_engine()
    config = Config(str(ROOT / "alembic.ini"))
    alembic.upgrade(config, "head")
    assert "paper_figures" in inspect(engine).get_table_names()
    alembic.downgrade(config, "c6e1a4b92f70")
    assert "paper_figures" not in inspect(engine).get_table_names()
    alembic.upgrade(config, "head")
    data, paper_id = pdf.read_bytes(), str(uuid.uuid4())
    digest = hashlib.sha256(data).hexdigest()
    stored = object_storage.upload_original_pdf(paper_id, data)
    with SessionLocal() as session:
        session.add(
            Paper(
                id=paper_id,
                fingerprint="sha256:" + digest,
                title="Disposable figure acceptance",
                status="INDEXED",
            )
        )
        session.flush()
        session.add(
            PaperFile(
                paper_id=paper_id,
                kind="original",
                is_primary=True,
                bucket=stored.bucket,
                object_key=stored.object_key,
                sha256=digest,
                size_bytes=len(data),
            )
        )
        session.commit()
    # Only unrelated startup dependencies are substituted. The app, queue,
    # PostgreSQL, MinIO, JVM and native exporter are real.
    embedding_service.check_dimension_consistency = lambda: None
    api_key_service.startup_bootstrap = lambda: None
    housekeeping.start = lambda: None

    async def stop_housekeeping():
        pass

    housekeeping.stop = stop_housekeeping
    health_service.health_specs = lambda: (
        health_service.postgres_spec(),
        health_service.minio_spec(),
    )
    base = f"/api/papers/{paper_id}/figures"
    timings = []
    try:
        with TestClient(app) as client:
            assert client.get("/openapi.json").status_code == 200
            health = client.get("/health")
            assert health.status_code == 200 and not health.json()["degraded"], (
                health.text
            )
            assert client.get(base).json()["status"] == "not_extracted"

            def extract(force=False):
                started = time.monotonic()
                response = client.post(base + "/extract", json={"force": force})
                assert response.status_code == 202, response.text
                assert time.monotonic() - started < 5
                job_id = response.json()["job_id"]
                deadline = started + 60
                while time.monotonic() < deadline:
                    job = client.get("/api/jobs/" + job_id).json()
                    if job["stage"] in ("COMPLETED", "FAILED"):
                        assert job["stage"] == "COMPLETED", job
                        timings.append(round(time.monotonic() - started, 3))
                        return client.get(base).json()
                    time.sleep(0.1)
                raise RuntimeError(
                    "figure job did not terminate within acceptance deadline"
                )

            first = extract()
            assert first["total"] == 5, first
            assert {row["figure_number"] for row in first["figures"]} == {
                "1",
                "2",
                "3",
                "4",
                "5",
            }
            figure = first["figures"][0]
            assert figure["caption_text"] and figure["match_status"] == "needs_review"
            archive = client.get(base + "/download", params={"variant": "all"})
            assert archive.status_code == 200, archive.text
            with zipfile.ZipFile(io.BytesIO(archive.content)) as packed:
                manifest = json.loads(packed.read("manifest.json"))
                assert manifest["generation_id"] == first["generation_id"]
                assert len(manifest["figures"]) == 5
                assert (
                    len([name for name in packed.namelist() if name.endswith(".pdf")])
                    == 5
                )
                assert (
                    len([name for name in packed.namelist() if name.endswith(".png")])
                    == 10
                )
                assert packed.testzip() is None
            for url in (
                figure["native_url"],
                figure["parts"][0]["preview_url"],
                figure["parts"][0]["thumbnail_url"],
            ):
                response = client.get(url)
                assert response.status_code == 200 and len(response.content) > 100
                assert (
                    client.get(
                        url, headers={"If-None-Match": response.headers["etag"]}
                    ).status_code
                    == 304
                )
            assert client.post(base + "/extract").status_code == 200
            second = extract(True)
            assert second["generation_id"] != first["generation_id"]
            assert (
                client.get(
                    base + "/download", params={"generation_id": first["generation_id"]}
                ).status_code
                == 409
            )
            assert (
                client.get(
                    base, params={"generation_id": first["generation_id"]}
                ).status_code
                == 409
            )
            identifier = second["figures"][0]["figure_id"]
            assert client.delete(base + "/" + identifier).status_code == 200
            assert client.get(base).json()["total"] == 4
            assert client.get(second["figures"][0]["native_url"]).status_code == 404
            assert client.delete(base).status_code == 200
            assert client.get(base).json()["total"] == 0
            assert client.get(base + "/download").status_code == 409
            third = extract()
            assert third["total"] == 5
            assert (
                object_storage.download_bytes(stored.object_key, stored.bucket) == data
            )
            with SessionLocal() as session:
                paper = session.get(Paper, paper_id)
                assert (
                    paper.title == "Disposable figure acceptance"
                    and paper.status == "INDEXED"
                )
            assert client.delete(base).status_code == 200
            assert [
                item.object_name
                for item in object_storage.list_objects(prefix=f"papers/{paper_id}/")
            ] == [stored.object_key]
            print(
                json.dumps(
                    {
                        "migration_roundtrip": True,
                        "cold_start": True,
                        "figures": third["total"],
                        "extraction_seconds": timings,
                        "original_unchanged": True,
                        "cleanup": True,
                        "zip_download": True,
                    }
                )
            )
    finally:
        object_storage.delete_prefix(paper_id)
        engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pdf", type=Path, default=ROOT / "tests/fixtures/smoke_sample.pdf"
    )
    parser.add_argument("--inside", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    (inside if args.inside else supervise)(args.pdf)
