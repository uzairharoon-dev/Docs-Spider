# Retrieval evaluation

Retrieval quality is measured, not assumed. The bundled eval set has **84 questions** over
three docs sites: FastAPI (40), httpx (20) and Typer (24). The questions are phrased the way a
developer asks an assistant ("My React app on localhost:3000 gets blocked when calling the
API"), not copied from page headings, so keyword matching gets no free ride. Each question lists
every page that answers it.

## Results

Crawled 2026-11-13; 167 pages, 1,008 chunks; `bge-small-en-v1.5`; k = 10.

| Config | Recall@1 | Recall@3 | Recall@5 | Recall@10 | MRR@10 | p50 ms | p95 ms |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| vector | 0.762 | 0.881 | 0.917 | 0.952 | 0.832 | 34 | 114 |
| bm25 | 0.643 | 0.786 | 0.881 | 0.940 | 0.736 | 7 | 15 |
| **hybrid** (default) | **0.774** | **0.917** | **0.940** | **0.976** | **0.847** | 49 | 105 |
| hybrid + rerank | 0.798 | 0.905 | 0.952 | 0.976 | 0.855 | 3241 | 4819 |

- **Hybrid beats either retriever alone** on every metric. BM25 and vectors miss different
  questions, so fusing them recovers most of each one's misses.
- **Targets met:** Recall@5 0.940 against a target of ≥ 0.85; p95 105 ms against < 300 ms.
- **The reranker** adds a little at the top (Recall@1 +0.024, MRR +0.008) but costs seconds per
  query on the CPU used here, which is a slow one. It stays opt-in.
- Latency here is one CLI-process search call, query embedding included. The
  [benchmarks](benchmarks.md) measure a warm, long-running process instead.
- Raw per-question results: `docurag/eval/results/2026-11-13.json`.

## How the metrics work

A hit is relevant when its page URL is one of the question's expected pages. Recall@k is the
share of questions with a relevant hit in the top k. MRR@10 is the mean of 1 / rank of the
first relevant hit.

One label was corrected after the first run. Typer's "Nested SubCommands" page also answers
`ty-17` and had been left out of its expected pages. No other labels were changed after
seeing results.

## Reproduce it

About 10 minutes of crawling and embedding on a laptop:

```bash
export DOCURAG_HOME=~/.docurag-eval        # keep the corpus apart from your own libraries
for lib in fastapi httpx typer; do uv run docurag -c docurag/eval/corpus.yaml update $lib; done
uv run docurag -c docurag/eval/corpus.yaml eval --out results.json
uv run docurag -c docurag/eval/corpus.yaml eval --configs hybrid   # faster: skip the reranker
```

Docs sites change, so expect small drifts from the table above.
