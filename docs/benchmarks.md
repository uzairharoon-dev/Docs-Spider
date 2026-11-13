# Benchmarks

Measured 2026-11-13 on a laptop: 8th-gen Intel Core (4 cores / 8 threads), 17 GB RAM,
Windows 11, Python 3.12. It is a slow CPU for ML work. Raw results live in
`benchmarks/results/`.

## Summary

| Target | Result | |
| --- | --- | --- |
| Crawl ≥ 20 pages/s across domains | **23.8 pages/s** polite (2 req/s per domain), 41.2 unthrottled | ✅ |
| Search p95 < 300 ms without reranker | **26 ms** hybrid, 17 ms vector, 7 ms BM25 | ✅ |
| Runs on a laptop | **358 MB** RSS serving searches; 7.9 KB of disk per chunk | ✅ |

## Crawl throughput

`benchmarks/bench_crawl.py` serves 12 synthetic docs "domains" (one local port each,
60 pages of ~1,000 words, code blocks and tables) from a separate process, and crawls them all
as one library. Nothing on the internet is touched.

| Mode | Pages | Seconds | Pages/s |
| --- | ---: | ---: | ---: |
| Polite: 2 req/s per domain (the default) | 732 | 30.8 | **23.8** |
| Unthrottled: the crawler's own ceiling | 732 | 17.8 | **41.2** |

Politeness caps one domain at 2 requests/second, so the 20 pages/s target is about crawling
many domains at once. Two changes got there:

- **Round-robin across hosts.** A page's links all point at its own host. Handed out in
  order, they made most workers queue on that host's rate limiter while the other hosts
  sat idle: **5.6 pages/s**. The frontier now interleaves hosts within each depth level.
- **Extraction in worker processes.** HTML-to-Markdown is CPU-bound and was the ceiling once
  fetching stopped waiting. A pool of `min(4, CPUs − 1)` processes does it now.

```bash
uv run --group bench python benchmarks/bench_crawl.py --domains 12 --pages 60
```

## Search latency

`benchmarks/bench_search.py` runs the 84 eval questions three times per mode in one warm
process (as the MCP server and REST API do), query embedding included. Corpus: the eval set's
167 pages, 1,008 chunks.

| Mode | n | p50 ms | p95 ms | p99 ms |
| --- | ---: | ---: | ---: | ---: |
| bm25 | 252 | 3.8 | 7.4 | 9.6 |
| vector | 252 | 12.2 | 17.2 | 28.3 |
| **hybrid** (default) | 252 | 18.5 | **26.4** | 36.5 |
| hybrid + rerank | 20 | 2,890 | 4,091 | 5,366 |

The reranker scores 20 candidates of up to 512 tokens with a cross-encoder on the CPU, which
takes seconds here. It stays off by default.

## Memory and disk

| | |
| --- | ---: |
| RSS, Python + DocuRAG | 60 MB |
| RSS with the embedding model loaded | 227 MB |
| RSS after 756 searches and 20 reranks | **358 MB** |
| Database (pages, chunks, FTS index, vectors) | 7.9 MB, 7.9 KB per chunk |
| Model files (embedder + reranker) | 159 MB |
| Embedding throughput, indexing | 2.3 chunks/s |

The first run of this benchmark ended at **2,340 MB**. Repeated queries did not leak; the
growth came from ONNX Runtime's CPU memory arena, which keeps the largest buffer each input
shape has needed. Reranking and batch embedding feed it many shapes. With the arena off
(`embedder.cpu_mem_arena: false`, now the default) query and rerank speed did not change
measurably and RSS stays flat.

```bash
DOCURAG_HOME=~/.docurag-eval uv run --group bench python benchmarks/bench_search.py \
    -c docurag/eval/corpus.yaml --repeat 3
```
