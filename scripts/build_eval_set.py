"""Build evals/queries.jsonl + evals/labels.jsonl from the audited spec.

``evals/queries-spec.json`` is the human-reviewed source of truth: every query
carries a ``note`` explaining the grading decision, and every label points at an
``arxiv`` id (or a ``title_like`` prefix for the handful of library entries whose
arxiv id is empty). This script resolves those references against PostgreSQL so
the label file always contains real ``paper_id`` values.

Usage:  uv run python scripts/build_eval_set.py [--spec evals/queries-spec.json]
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _docker_argv(*args: str) -> list[str]:
    """Build ``docker <args>`` for whatever environment we are running in.

    Linux/macOS servers have the docker CLI on PATH. On this Windows dev box the
    daemon lives inside WSL and there is no docker CLI on the Windows PATH, so we
    fall back to ``wsl -e docker ...`` (previously this file hardcoded the wsl
    wrapper, which made the script unusable on Linux).

    Override with ``PAPERBOX_DOCKER_PREFIX`` (e.g. ``wsl -e`` or
    ``docker -H tcp://host:2375``) when neither default fits.
    """
    prefix = os.environ.get("PAPERBOX_DOCKER_PREFIX")
    if prefix:
        base = shlex.split(prefix)
    elif shutil.which("docker"):
        base = ["docker"]
    else:
        base = ["wsl", "-e", "docker"]
    return [*base, *args]


def psql(sql: str) -> list[list[str]]:
    """Run one read-only query through the dockerised PostgreSQL."""
    container = os.environ.get("PAPERBOX_PG_CONTAINER", "paperbox-postgres")
    result = subprocess.run(
        _docker_argv(
            "exec", container, "psql", "-U", "postgres", "-d", "paperbox",
            "-t", "-A", "-F|", "-c", sql,
        ),
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "psql failed")
    return [line.split("|") for line in result.stdout.splitlines() if line.strip()]


def load_corpus() -> tuple[dict[str, list[str]], list[tuple[str, str]]]:
    """(arxiv_id -> [paper_id], [(title, paper_id)]) for every live indexed paper."""
    rows = psql(
        "select coalesce(arxiv_id,''), id, coalesce(title,'') from papers "
        "where deleted_at is null and status='INDEXED' order by created_at"
    )
    by_arxiv: dict[str, list[str]] = {}
    corpus: list[tuple[str, str]] = []
    for arxiv_id, paper_id, title in rows:
        corpus.append((title, paper_id))
        if arxiv_id:
            by_arxiv.setdefault(arxiv_id, []).append(paper_id)
    return by_arxiv, corpus


def resolve(label: dict, by_arxiv: dict[str, list[str]], corpus: list[tuple[str, str]]) -> list[str]:
    if "arxiv" in label:
        return list(by_arxiv.get(label["arxiv"], []))
    # ``title_like`` is written SQL-style ("A Novel 550-fs%"); Python matches a
    # literal prefix, so the trailing wildcard marker is stripped here.
    prefix = label.get("title_like", "").rstrip("%")
    return [paper_id for title, paper_id in corpus if title.startswith(prefix)]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", default=str(REPO / "evals" / "queries-spec.json"))
    args = parser.parse_args()

    spec = json.loads(Path(args.spec).read_text(encoding="utf-8"))
    by_arxiv, corpus = load_corpus()
    print(f"corpus: {len(corpus)} live indexed papers, {len(by_arxiv)} with an arxiv id")

    queries_out: list[str] = []
    labels_out: list[str] = []
    unresolved: list[str] = []

    for entry in spec["queries"]:
        queries_out.append(
            json.dumps(
                {
                    "id": entry["id"],
                    "query": entry["query"],
                    "language": entry.get("language", "en"),
                    "note": entry.get("note", ""),
                },
                ensure_ascii=False,
            )
        )
        for label in entry["labels"]:
            for paper_id in resolve(label, by_arxiv, corpus):
                labels_out.append(
                    json.dumps(
                        {
                            "query_id": entry["id"],
                            "paper_id": paper_id,
                            "grade": label["grade"],
                        },
                        ensure_ascii=False,
                    )
                )
            if not resolve(label, by_arxiv, corpus):
                unresolved.append(f"{entry['id']} -> {label}")

    (REPO / "evals" / "queries.jsonl").write_text("\n".join(queries_out) + "\n", encoding="utf-8")
    (REPO / "evals" / "labels.jsonl").write_text("\n".join(labels_out) + "\n", encoding="utf-8")

    print(f"queries: {len(queries_out)}  labels: {len(labels_out)}")
    if unresolved:
        print("UNRESOLVED (需人工确认):")
        for item in unresolved:
            print("  ", item)
    else:
        print("all labels resolved against the live corpus")


if __name__ == "__main__":
    main()
