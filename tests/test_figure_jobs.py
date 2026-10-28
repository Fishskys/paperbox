"""Extraction is bounded and does not consume every ingestion slot."""

import asyncio
import subprocess
import sys
import threading

import pytest

from app.core.config import settings
from app.workers import figure_tasks
from app.workers.queue import IngestQueue


def test_figures_leave_a_worker_available_for_ingestion(monkeypatch):
    monkeypatch.setattr(settings, "figure_extract_concurrency", 1)
    started, release, ingested = threading.Event(), threading.Event(), threading.Event()
    active, peak = 0, 0
    lock = threading.Lock()

    def figure(job_id):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        started.set()
        assert release.wait(3)
        with lock:
            active -= 1

    async def scenario():
        queue = IngestQueue(
            concurrency=2,
            runners={"figures": figure, "ingest": lambda _: ingested.set()},
        )
        queue.start()
        try:
            queue.enqueue("figure-1", "figures")
            assert await asyncio.to_thread(started.wait, 2)
            queue.enqueue("figure-2", "figures")
            queue.enqueue("normal-paper", "ingest")
            assert await asyncio.to_thread(ingested.wait, 2)
            release.set()
            await asyncio.wait_for(queue.join(), 3)
            assert queue.depth() == 0
        finally:
            release.set()
            await queue.stop()

    asyncio.run(scenario())
    assert peak == 1


def test_offline_figure_admission_never_runs_inline():
    called = []
    queue = IngestQueue(runners={"figures": called.append})
    assert not queue.enqueue("offline", "figures", allow_inline=False)
    assert not called


def test_deadline_reaps_the_real_subprocess(tmp_path, monkeypatch):
    figure_tasks.start()
    real_popen = subprocess.Popen
    children = []

    def launch(*args, **kwargs):
        process = real_popen(
            [sys.executable, "-c", "import time; time.sleep(30)"], **kwargs
        )
        children.append(process)
        return process

    monkeypatch.setattr(figure_tasks.subprocess, "Popen", launch)
    output = tmp_path / "output"
    output.mkdir()
    with pytest.raises(subprocess.TimeoutExpired):
        figure_tasks.extract(
            "deadline-test", tmp_path / "source.pdf", output, {"timeout": 0.1}
        )
    assert children[0].poll() is not None


def test_cancel_reaps_the_real_subprocess(tmp_path, monkeypatch):
    figure_tasks.start()
    real_popen = subprocess.Popen
    launched, children, errors = threading.Event(), [], []

    def launch(*args, **kwargs):
        process = real_popen(
            [sys.executable, "-c", "import time; time.sleep(30)"], **kwargs
        )
        children.append(process)
        return process

    # Wait signals after registration rather than racing Popen construction.
    real_wait = subprocess.Popen.wait

    def wait(process, *args, **kwargs):
        launched.set()
        return real_wait(process, *args, **kwargs)

    monkeypatch.setattr(figure_tasks.subprocess, "Popen", launch)
    monkeypatch.setattr(real_popen, "wait", wait)
    output = tmp_path / "output"
    output.mkdir()

    def execute():
        try:
            figure_tasks.extract(
                "cancel-test", tmp_path / "source.pdf", output, {"timeout": 3}
            )
        except RuntimeError as exc:
            errors.append(exc)

    worker = threading.Thread(target=execute)
    worker.start()
    assert launched.wait(2)
    figure_tasks.cancel("cancel-test")
    worker.join(3)
    assert not worker.is_alive() and children[0].poll() is not None and errors
