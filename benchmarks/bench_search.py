"""Search latency, memory footprint and embedding throughput on a built index.

Run it against the eval corpus (see docurag/eval/corpus.yaml) or any data dir:

    DOCURAG_HOME=~/.docurag-eval uv run --group bench python benchmarks/bench_search.py \
        -c docurag/eval/corpus.yaml --repeat 3

Latency is measured in-process (no CLI start-up), query embedding included, after a
warm-up query per mode. Spec targets: p95 < 300 ms without reranker; runs on a laptop.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import time
from pathlib import Path

import psutil

from docurag.config import load_settings
from docurag.eval.run_eval import load_dataset
from docurag.serve.service import DocuRAGService


def pct(values: list[float], p: int) -> float:
    return statistics.quantiles(values, n=100, method="inclusive")[p - 1]


def mb(n: float) -> float:
    return round(n / 1e6, 1)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("-c", "--config", type=Path)
    ap.add_argument("--modes", default="bm25,vector,hybrid")
    ap.add_argument("--repeat", type=int, default=3, help="passes over the question set")
    ap.add_argument("--rerank-queries", type=int, default=20,
                    help="also time hybrid+rerank on this many questions (0 to skip)")
    ap.add_argument("--json", type=Path)
    args = ap.parse_args()

    proc = psutil.Process()
    settings = load_settings(args.config)
    rss_start = proc.memory_info().rss
    service = DocuRAGService(settings)
    questions = load_dataset()

    t = time.perf_counter()
    embedder, _ = service._get_embedder_and_store()
    model_load_s = time.perf_counter() - t
    rss_model = proc.memory_info().rss

    texts = [r[0] for r in service.db.conn.execute("SELECT text FROM chunks LIMIT 64")]
    t = time.perf_counter()
    embedder.embed_documents(texts)
    embed_rate = len(texts) / (time.perf_counter() - t)

    latency: dict[str, dict] = {}
    for mode in args.modes.split(","):
        service.search(questions[0].question, questions[0].library, k=10, mode=mode)  # warm-up
        times = []
        for _ in range(args.repeat):
            for q in questions:
                t = time.perf_counter()
                service.search(q.question, q.library, k=10, mode=mode, rerank=False)
                times.append((time.perf_counter() - t) * 1000)
        latency[mode] = {"n": len(times), "p50_ms": round(pct(times, 50), 1),
                         "p95_ms": round(pct(times, 95), 1), "p99_ms": round(pct(times, 99), 1)}
    if args.rerank_queries:
        qs = questions[: args.rerank_queries]
        service.search(qs[0].question, qs[0].library, k=10, mode="hybrid", rerank=True)
        times = []
        for q in qs:
            t = time.perf_counter()
            service.search(q.question, q.library, k=10, mode="hybrid", rerank=True)
            times.append((time.perf_counter() - t) * 1000)
        latency["hybrid+rerank"] = {"n": len(times), "p50_ms": round(pct(times, 50), 1),
                                    "p95_ms": round(pct(times, 95), 1),
                                    "p99_ms": round(pct(times, 99), 1)}
    rss_end = proc.memory_info().rss

    db = service.db.conn
    chunks = db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    pages = db.execute("SELECT COUNT(*) FROM pages WHERE gone = 0").fetchone()[0]
    db_bytes = sum(
        p.stat().st_size for p in settings.data_dir.glob("docurag.db*") if p.is_file()
    )
    models_bytes = sum(
        p.stat().st_size for p in (settings.data_dir / "models").rglob("*") if p.is_file()
    )

    result = {
        "machine": {
            "cpu": platform.processor() or platform.machine(),
            "cores": os.cpu_count(),
            "ram_gb": round(psutil.virtual_memory().total / 1e9, 1),
            "os": f"{platform.system()} {platform.release()}",
            "python": platform.python_version(),
        },
        "corpus": {"pages": pages, "chunks": chunks},
        "latency": latency,
        "embedding": {"model": settings.embedder.model, "load_s": round(model_load_s, 1),
                      "chunks_per_s": round(embed_rate, 1)},
        "footprint": {
            "rss_baseline_mb": mb(rss_start),
            "rss_with_model_mb": mb(rss_model),
            "rss_after_queries_mb": mb(rss_end),
            "db_mb": mb(db_bytes),
            "db_kb_per_chunk": round(db_bytes / max(chunks, 1) / 1e3, 1),
            "model_files_mb": mb(models_bytes),
        },
    }
    service.db.close()
    print(json.dumps(result, indent=2))
    if args.json:
        args.json.write_text(json.dumps(result, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
