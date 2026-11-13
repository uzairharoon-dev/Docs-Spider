# Development

```bash
uv sync                 # install with dev dependencies
uv run pytest           # run the test suite
uv run ruff check .     # lint
```

The tests serve a fake docs site through `httpx.MockTransport`, so they never touch the
network. They cover robots rules, redirects, duplicates, conditional re-crawls, the `include`
filter, `max_pages`, resuming after an interruption, round-robin scheduling across hosts,
chunk boundaries, both vector stores, incremental indexing, BM25/FTS5 sync and migration,
rank fusion, reranking, the eval metrics, the REST API, the MCP tools (in memory and as a real
stdio subprocess) and the CLI. They use the `hash` embedder, so no model download is needed.

CI runs lint and tests on Ubuntu and Windows, and builds the Docker image
(`.github/workflows/ci.yml`).

## Benchmarks

```bash
uv run --group bench python benchmarks/bench_crawl.py --json benchmarks/results/crawl.json
DOCURAG_HOME=~/.docurag-eval uv run --group bench python benchmarks/bench_search.py \
    -c docurag/eval/corpus.yaml --json benchmarks/results/search.json
```

See [Benchmarks](benchmarks.md) for what they measure.

## This site

```bash
uv run --group docs mkdocs serve        # http://127.0.0.1:8000
uv run --group docs mkdocs build --strict
```

Pushes to `main` that touch `docs/` or `mkdocs.yml` publish it to GitHub Pages
(`.github/workflows/docs.yml`).

## Demo GIF

`demo/demo.tape` is a [VHS](https://github.com/charmbracelet/vhs) script. `demo/README.md`
explains how to re-record `docs/assets/demo.gif`.
