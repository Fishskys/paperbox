"""Isolated, cancellable native extraction; parent owns database and storage."""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading

from app.core.logging import get_logger
from app.db.session import SessionLocal
from app.db.models import FigureExtractionRun, IngestionJob
from app.services import figure_service as figures, object_storage

logger = get_logger(__name__)
_lock = threading.Lock()
_processes: dict[str, subprocess.Popen] = {}
_stopping = False


def start():
    global _stopping
    with _lock:
        _stopping = False


def _kill(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def cancel(run_id):
    with _lock:
        process = _processes.get(run_id)
        if process is not None:
            _kill(process)


def cancel_all():
    global _stopping
    with _lock:
        _stopping = True
        for process in _processes.values():
            _kill(process)


def extract(run_id, source, output, options):
    config = output.parent / "options.json"
    config.write_text(json.dumps(options))
    with tempfile.TemporaryFile() as log:
        with _lock:
            if _stopping:
                raise RuntimeError("figure worker is stopping")
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "app.parsing.figures",
                    str(source),
                    str(output),
                    str(config),
                ],
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
            _processes[run_id] = process
        try:
            process.wait(timeout=options["timeout"])
            if process.returncode:
                raise RuntimeError("figure extractor exited unsuccessfully")
        finally:
            _kill(process)
            process.wait()
            with _lock:
                _processes.pop(run_id, None)
    return json.loads((output / "manifest.json").read_text())


def run(job_id):
    session = SessionLocal()
    run_id = None
    try:
        job = session.get(IngestionJob, job_id)
        if job is None:
            return
        run_id = (job.payload or {}).get("run_id")
        run = session.get(FigureExtractionRun, run_id)
        if run is None or run.status != "queued":
            return
        record, source = figures.check_run(session, run_id)
        run.status, run.started_at = "running", figures.now()
        job.stage, job.progress, job.started_at = "PARSING", 10, figures.now()
        session.commit()
        with tempfile.TemporaryDirectory(prefix="paperbox-figures-") as temporary:
            root = Path(temporary)
            local_source, output = root / "source.pdf", root / "output"
            output.mkdir()
            size = 0
            with (
                object_storage.open_stream(
                    source.object_key, bucket=source.bucket
                ) as remote,
                local_source.open("wb") as local,
            ):
                while block := remote.read(1024 * 1024):
                    size += len(block)
                    if size > run.options["max_temp_bytes"]:
                        raise ValueError("source PDF exceeds temporary byte budget")
                    local.write(block)
            figures.check_run(session, run_id)
            session.rollback()
            result = extract(run_id, local_source, output, run.options)
            figures.publish(session, run_id, result, output)
    except Exception as exc:
        session.rollback()
        logger.exception(
            "figure extraction failed",
            extra={"extra_fields": {"job_id": job_id, "run_id": run_id}},
        )
        if run_id:
            if isinstance(exc, figures.FigureError):
                code, message = exc.code, str(exc)
            elif isinstance(exc, subprocess.TimeoutExpired):
                code, message = (
                    "FIGURE_EXTRACTION_TIMEOUT",
                    "figure extraction exceeded its processing deadline",
                )
            elif isinstance(exc, object_storage.ObjectStorageError):
                code, message = (
                    "FIGURE_STORAGE_FAILED",
                    "figure storage operation failed",
                )
            else:
                code, message = (
                    "FIGURE_EXTRACTION_FAILED",
                    "figure extraction failed; see server logs for details",
                )
            figures.fail(session, run_id, code, message)
    finally:
        if run_id:
            run = session.get(FigureExtractionRun, run_id)
            if (
                run is not None
                and run.status == "cancelled"
                and run.finished_at is None
            ):
                run.finished_at = figures.now()
                session.commit()
            figures.cleanup(session, run_ids=[run_id])
        session.close()
