#!/usr/bin/env python3
"""Prove the embedding container serializes inference instead of thrashing (T7.3).

    uv run python scripts/probe_embedding_queue.py [--url http://127.0.0.1:8090]
    uv run python scripts/probe_embedding_queue.py --mode rejection --url http://127.0.0.1:8092

Two modes.

``serialization`` (default) runs against the *running* container (no database, no
artifacts):

1. reads ``/info`` for the queue configuration,
2. warms the model up and measures one batch's latency ``L``,
3. fires ``--clients`` batches at once and measures the wall time ``W``,
4. reports ``W`` against ``L`` and ``clients * L``: with ``INFERENCE_WORKERS=1``
   the batch is expected to take about ``clients * L`` (serialized), not ``L``
   (parallel). It also samples ``/info`` while the burst runs to show the peak
   number of inferences in flight, which must stay at ``workers``.

``rejection`` checks the bounded part of the queue: fire ``--clients`` requests at
once and expect a mix of ``200`` and ``503``+``Retry-After`` once the backlog
exceeds ``INFERENCE_QUEUE_DEPTH``. Point it at a throwaway container that sets a
small depth (see docs/progress/project.md), never at the live one.

Nothing is written anywhere: the script only reads HTTP, so there is nothing to
clean up afterwards.
"""
from __future__ import annotations

import argparse
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

#: Enough text that one batch takes real CPU time (e5 truncates at 512 tokens,
#: so more text would not make the measurement more honest).
FILLER = (
    "The comparator offsets are cancelled by chopping, which lets the amplifier "
    "operate at a lower supply voltage without losing input range. "
) * 12


def info(client: httpx.Client, url: str) -> dict:
    return client.get(f"{url}/info", timeout=30).json()


def one_batch(client: httpx.Client, url: str, batch: int) -> float:
    started = time.perf_counter()
    response = client.post(
        f"{url}/embed", json={"texts": [FILLER] * batch}, timeout=600
    )
    response.raise_for_status()
    body = response.json()
    assert body["count"] == batch, body.get("count")
    return time.perf_counter() - started


def sample_peak_running(client: httpx.Client, url: str, stop: list[bool]) -> int:
    """Poll ``/info`` until ``stop`` is set; return the peak ``running``."""
    peak = 0
    while not stop[0]:
        try:
            peak = max(peak, int(info(client, url)["inference"]["running"]))
        except Exception:  # noqa: BLE001 - sampling must never fail the probe
            pass
        time.sleep(0.05)
    return peak


def rejection_check(url: str, clients: int, batch: int) -> int:
    """A full queue must answer 503 + Retry-After, never an endless wait."""
    with httpx.Client() as client:
        before = info(client, url)
        queue = before["inference"]
        print(f"url          : {url}")
        print(
            "queue        : workers={workers} depth={queue_depth}".format(**queue)
        )

        def fire(index: int) -> tuple[int, str | None]:
            response = client.post(
                f"{url}/embed", json={"texts": [f"probe {index}"] * batch}, timeout=600
            )
            return response.status_code, response.headers.get("Retry-After")

        with ThreadPoolExecutor(max_workers=clients) as pool:
            results = list(pool.map(fire, range(clients)))

        codes = [status for status, _ in results]
        retry_after = [value for status, value in results if status == 503]
        print(f"statuses     : {codes}")
        print(f"retry-after  : {retry_after}")
        after = info(client, url)
        print("counters     : " + ", ".join(f"{k}={v}" for k, v in after["inference"].items()))

        accepted = codes.count(200)
        rejected = codes.count(503)
        ok_rejected = rejected > 0 and all(value for value in retry_after)
        ok_accepted = accepted >= 1
        ok_counted = after["inference"]["rejected"] >= rejected
        print()
        print(f"[{'OK' if ok_accepted else 'FAIL'}] {accepted} request(s) accepted")
        verdict = "OK" if ok_rejected else "FAIL"
        print(f"[{verdict}] {rejected} request(s) got 503 + Retry-After")
        print(f"[{'OK' if ok_counted else 'FAIL'}] server counted the rejections")
        return 0 if (ok_accepted and ok_rejected and ok_counted) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8090")
    parser.add_argument(
        "--mode", choices=("serialization", "rejection"), default="serialization"
    )
    parser.add_argument("--clients", type=int, default=4)
    parser.add_argument("--batch", type=int, default=16)
    args = parser.parse_args()

    if args.mode == "rejection":
        return rejection_check(args.url, args.clients, args.batch)

    with httpx.Client() as client:
        before = info(client, args.url)
        queue = before["inference"]
        print(f"url          : {args.url}")
        print(f"model        : {before['model']} (max_batch={before['max_batch']})")
        print(
            "queue        : workers={workers} depth={queue_depth} "
            "completed={completed} rejected={rejected}".format(**queue)
        )

        warm = one_batch(client, args.url, args.batch)
        print(f"warm-up      : {warm:.2f}s (model load included)")

        single = statistics.median(
            [one_batch(client, args.url, args.batch) for _ in range(3)]
        )
        print(f"single batch : {single:.2f}s (median of 3, batch={args.batch})")

        stop = [False]
        with ThreadPoolExecutor(max_workers=1) as sampler:
            peak_future = sampler.submit(sample_peak_running, client, args.url, stop)
            started = time.perf_counter()
            with ThreadPoolExecutor(max_workers=args.clients) as pool:
                latencies = list(
                    pool.map(
                        lambda _: one_batch(client, args.url, args.batch),
                        range(args.clients),
                    )
                )
            wall = time.perf_counter() - started
            stop[0] = True
            peak_running = peak_future.result()

        after = info(client, args.url)
        expected_serial = args.clients * single
        print()
        print(f"concurrent  : {args.clients} batches fired at once")
        print(f"  wall      : {wall:.2f}s")
        print(f"  latencies : {', '.join(f'{value:.2f}' for value in latencies)}")
        print(f"  peak in-flight inference : {peak_running} (workers={queue['workers']})")
        print(f"  serialized? wall vs clients*single = {wall:.2f}s vs {expected_serial:.2f}s")
        print(
            "  counters  : completed={completed} rejected={rejected} waiting={waiting} "
            "running={running}".format(**after["inference"])
        )

        ok = peak_running <= queue["workers"]
        serial = wall > expected_serial * 0.6
        print()
        peak_verdict = "OK" if ok else "FAIL"
        serial_verdict = "OK" if serial else "FAIL"
        print(
            f"[{peak_verdict}] peak in-flight {peak_running} <= "
            f"workers {queue['workers']}"
        )
        print(
            f"[{serial_verdict}] queueing visible "
            f"(wall >> one batch {single:.2f}s)"
        )
        return 0 if (ok and serial) else 1


if __name__ == "__main__":
    sys.exit(main())
