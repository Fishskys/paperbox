"""Real-machine acceptance for the rerank model swap (plan T-C2/T-C3).

Measures the gates a cross-encoder swap has to clear, against the *live*
embedding container -- memory, latency, stability, and the runtime facts that make
the swap reversible. Quality is not measured here: that comes from
``scripts/eval.py`` against the same query set for both models.

    uv run python scripts/acceptance_rerank_model.py --label jina
    uv run python scripts/acceptance_rerank_model.py --label mmarco-int8

What it does, per round:

1. reads ``/health`` + ``/info`` (the model *and* the ONNX file it actually loaded),
2. assembles the app's real candidate width (``top_k x RERANK_CANDIDATES``, 50 by
   default) out of **real indexed chunk text** -- a cross-encoder's cost is a
   function of ``tokens x candidates``, so synthetic filler would measure the
   wrong thing,
3. reports both peaks that matter, instead of presenting one as the other:

   * the container's **lifetime high-water mark** (``memory.peak``) -- this is what
     decides OOM risk, labelled with the container's start time because it cannot be
     reset (the cgroup is mounted ``ro`` inside the container, and this kernel
     ignores a host-side write of 0), so a warm container carries history;
   * the **per-call** maximum, sampled from the host side every 20ms while
     ``POST /rerank`` is in flight. ``docker stats`` is far too coarse for a 1-2s
     activation peak, and a sample taken after the call only sees what is left.
     A warm ONNX arena can make this look flat at the steady-state footprint, which
     is a real result -- not a substitute for the lifetime number.

It prints a table and writes ``logs/eval/rerank-model-<label>.json``. The script is
read-only against the library: it never reindexes, uploads, or writes to PG.
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONTAINER = "paperbox-embedding"
CGROUP_CURRENT = "/sys/fs/cgroup/memory.current"
CGROUP_PEAK = "/sys/fs/cgroup/memory.peak"
#: Host-side path of the container's cgroup. Inside the container the cgroup is
#: mounted read-only, so the memory numbers are read (and sampled) via WSL root.
HOST_CGROUP_GLOB = "/sys/fs/cgroup/system.slice/docker-{cid}.scope"


def _env_value(key: str, default: str = "") -> str:
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip()
    return default


def _wsl(args: list[str], timeout: float = 60.0) -> subprocess.CompletedProcess:
    return subprocess.run(["wsl", *args], capture_output=True, text=True, timeout=timeout)


def _cgroup_bytes(container: str, path: str) -> int | None:
    try:
        return int(_wsl(["docker", "exec", container, "cat", path]).stdout.strip())
    except Exception:
        return None


def _host_cgroup_dir(container: str) -> str | None:
    """The container's cgroup directory as seen from the WSL host."""
    try:
        cid = _wsl(["docker", "inspect", "-f", "{{.Id}}", container]).stdout.strip()
    except Exception:
        return None
    if not cid:
        return None
    candidates = [
        HOST_CGROUP_GLOB.format(cid=cid),
        f"/sys/fs/cgroup/docker/{cid}",
        f"/sys/fs/cgroup/docker.slice/docker-{cid}.scope",
    ]
    for path in candidates:
        if _wsl(["-u", "root", "test", "-f", f"{path}/memory.current"]).returncode == 0:
            return path
    return None


def _start_memory_probe(cgroup_dir: str, interval: float = 0.02) -> subprocess.Popen:
    """Stream ``memory.current`` *and* anonymous memory into a pipe.

    ``memory.current`` counts page cache too (a just-downloaded 400MB model shows up
    there and is reclaimable), so the anonymous number from ``memory.stat`` is what
    actually says how much the model costs. Both lines look like
    ``<current> <anon>``; the caller kills the probe after the call.
    """
    script = (
        f"while :; do echo $(cat {cgroup_dir}/memory.current) "
        f"$(grep ^anon {cgroup_dir}/memory.stat | cut -d' ' -f2); sleep {interval}; done"
    )
    return subprocess.Popen(
        ["wsl", "-u", "root", "bash", "-lc", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )


def _peaks_from_probe(proc: subprocess.Popen) -> tuple[int | None, int | None]:
    """``(total_peak, anon_peak)`` in bytes from the probe's paired samples."""
    proc.terminate()
    try:
        out, _ = proc.communicate(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate(timeout=15)
    totals: list[int] = []
    anon: list[int] = []
    for line in (out or "").splitlines():
        parts = line.split()
        if len(parts) == 2 and all(part.isdigit() for part in parts):
            totals.append(int(parts[0]))
            anon.append(int(parts[1]))
    return (max(totals) if totals else None, max(anon) if anon else None)


def _api_key() -> str:
    return _env_value("PAPER_API_KEY")


def _post(url: str, payload: dict, api_key: str, timeout: float) -> dict:
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _search_evidence(base_url: str, api_key: str, query: str, want: int) -> list[str]:
    """Text the search API itself would hand to the cross-encoder."""
    body = {"query": query, "top_k": max(1, want // 5), "rerank": False}
    payload = _post(f"{base_url}/api/search", body, api_key, timeout=180)
    texts: list[str] = []
    for result in payload.get("results", []):
        for evidence in result.get("evidence") or []:
            text = evidence.get("text") or ""
            if text:
                texts.append(text)
    return texts


def _indexed_chunks(want: int) -> list[str]:
    """Top up with real chunk text straight from the index (same corpus)."""
    base = _env_value("OPENSEARCH_URL", "http://127.0.0.1:9200").rstrip("/")
    source = _env_value("OPENSEARCH_ALIAS") or _env_value("OPENSEARCH_INDEX", "paper_chunks_current")
    body = {"size": max(1, want), "query": {"match_all": {}}, "_source": ["text"]}
    req = urllib.request.Request(
        f"{base}/{source}/_search",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        payload = json.loads(resp.read())
    return [
        hit["_source"]["text"]
        for hit in payload.get("hits", {}).get("hits", [])
        if hit.get("_source", {}).get("text")
    ]


def _candidates(base_url: str, api_key: str, query: str, want: int) -> list[str]:
    texts = _search_evidence(base_url, api_key, query, want)
    if len(texts) < want:
        seen = set(texts)
        for text in _indexed_chunks(want - len(texts)):
            if text not in seen:
                texts.append(text)
                seen.add(text)
    return texts[:want]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", required=True, help="tag for the report file, e.g. jina / mmarco-int8")
    parser.add_argument("--base-url", default="http://127.0.0.1:8077")
    parser.add_argument("--embedding-url", default="http://127.0.0.1:8090")
    parser.add_argument("--container", default=DEFAULT_CONTAINER)
    parser.add_argument("--candidates", type=int, default=50, help="candidates per call (top_k x RERANK_CANDIDATES)")
    parser.add_argument("--rounds", type=int, default=3, help="each round is one /rerank call at full width")
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument("--query", default="低功耗 SRAM 设计与近似计算")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    api_key = _api_key()
    started = time.time()

    with urllib.request.urlopen(f"{args.embedding_url}/health", timeout=60) as resp:
        health = json.loads(resp.read())
    with urllib.request.urlopen(f"{args.embedding_url}/info", timeout=60) as resp:
        info = json.loads(resp.read())

    docs = _candidates(args.base_url, api_key, args.query, args.candidates)
    if not docs:
        raise SystemExit("no candidate text available -- is the app running with a corpus?")

    peak_before = _cgroup_bytes(args.container, CGROUP_PEAK)
    container_started = _wsl(
        ["docker", "inspect", "-f", "{{.State.StartedAt}}", args.container]
    ).stdout.strip()
    cgroup_dir = _host_cgroup_dir(args.container)
    if cgroup_dir is None:
        raise SystemExit("cannot locate the container's cgroup on the WSL host -- memory gate unavailable")
    print(f"sampling {cgroup_dir}/memory.current every 20ms from the host", flush=True)
    latencies: list[float] = []
    peaks: list[int | None] = []
    anon_peaks: list[int | None] = []
    currents: list[int | None] = []
    errors: list[str] = []
    scores_seen = 0
    for round_index in range(args.rounds):
        before = _cgroup_bytes(args.container, CGROUP_CURRENT)
        probe = _start_memory_probe(cgroup_dir)
        round_seconds: float | None = None
        try:
            body = _post(
                f"{args.embedding_url}/rerank",
                {"query": args.query, "documents": docs},
                api_key,
                timeout=args.timeout,
            )
            results = body.get("results") or []
            scores_seen += len(results)
            round_seconds = float(body.get("took_ms", 0.0)) / 1000.0
            latencies.append(round_seconds)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            errors.append(f"round {round_index + 1}: {type(exc).__name__}: {exc}")
        total_peak, anon_peak = _peaks_from_probe(probe)
        peaks.append(total_peak)
        anon_peaks.append(anon_peak)
        currents.append(_cgroup_bytes(args.container, CGROUP_CURRENT))
        took = "failed" if round_seconds is None else f"{round_seconds:6.2f}s"
        peak_text = "n/a" if total_peak is None else f"{total_peak / 1048576:7.0f} MiB"
        anon_text = "n/a" if anon_peak is None else f"{anon_peak / 1048576:7.0f} MiB"
        before_text = "n/a" if before is None else f"{before / 1048576:6.0f}"
        print(
            f"  round {round_index + 1}/{args.rounds}: {took}  peak {peak_text} "
            f"(anon {anon_text}, before {before_text} MiB)",
            flush=True,
        )

    peak = max([p for p in peaks if p is not None], default=None)
    anon_peak = max([p for p in anon_peaks if p is not None], default=None)
    peak_after = _cgroup_bytes(args.container, CGROUP_PEAK)
    report = {
        "label": args.label,
        "rerank_model": health.get("rerank_model"),
        "rerank_model_file": health.get("rerank_model_file"),
        "rerank_loaded_after": health.get("rerank_loaded"),
        "rerank_max_batch": info.get("rerank_max_batch"),
        "inference_workers": info.get("inference", {}).get("workers"),
        "candidates_per_call": len(docs),
        "rounds": args.rounds,
        "container": args.container,
        "call_peak_mib": None if peak is None else round(peak / 1048576, 1),
        "call_peak_anon_mib": None if anon_peak is None else round(anon_peak / 1048576, 1),
        "call_peak_source": "host-side memory.current/memory.stat @20ms, max during the call",
        "lifetime_peak_before_mib": None if peak_before is None else round(peak_before / 1048576, 1),
        "lifetime_peak_after_mib": None if peak_after is None else round(peak_after / 1048576, 1),
        "lifetime_peak_source": "container memory.peak (lifetime high-water; not resettable on this kernel)",
        "container_started_at": container_started,
        "used_after_mib": None if not currents or currents[-1] is None else round(currents[-1] / 1048576, 1),
        "seconds_per_call": latencies,
        "seconds_per_candidate": [round(x / len(docs), 4) for x in latencies],
        "seconds_per_call_median": statistics.median(latencies) if latencies else None,
        "scores_returned": scores_seen,
        "errors": errors,
        "took_s": round(time.time() - started, 1),
    }

    out = Path(args.out) if args.out else ROOT / "logs" / "eval" / f"rerank-model-{args.label}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(
        f"\nmodel={report['rerank_model']} file={report['rerank_model_file']} "
        f"batch={report['rerank_max_batch']} workers={report['inference_workers']}"
    )
    print(f"candidates={len(docs)} rounds={args.rounds} errors={len(errors)}")
    if latencies:
        print(
            f"latency: median {report['seconds_per_call_median']:.2f}s/call "
            f"({report['seconds_per_candidate'][0]:.3f}s/candidate)"
        )
    print(
        f"memory: call peak {report['call_peak_mib']} MiB total / "
        f"{report['call_peak_anon_mib']} MiB anon ({report['call_peak_source']}); "
        f"container lifetime peak {report['lifetime_peak_before_mib']} -> "
        f"{report['lifetime_peak_after_mib']} MiB (started {report['container_started_at']})"
    )
    print(f"report -> {out}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
