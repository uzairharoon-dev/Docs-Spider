# Docs-Spider (DocuRAG)

[![CI](https://github.com/uzairharoon-dev/Docs-Spider/actions/workflows/ci.yml/badge.svg)](https://github.com/uzairharoon-dev/Docs-Spider/actions/workflows/ci.yml)
[![Docs](https://img.shields.io/badge/docs-site-indigo)](https://uzairharoon-dev.github.io/Docs-Spider/)
![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)

**Up-to-date, versioned library docs for AI coding assistants. Self-hosted, and it works on private docs too.**

AI coding assistants stop learning at their training cutoff. As a result they suggest
deprecated APIs, mix up library versions, and know nothing about your team's internal docs.

DocuRAG fixes that. It:

1. **crawls** any docs site, such as FastAPI, Next.js or your company wiki,
2. **indexes** the pages by version, and
3. **serves** them to Claude Code, Cursor and other assistants through an MCP server.

The assistant then answers from current docs and cites them.

![DocuRAG demo: listing libraries, a paraphrased question finding the CORS page, and an exact API name search](docs/assets/demo.gif)

**Full documentation: <https://uzairharoon-dev.github.io/Docs-Spider/>**

---

## Why not just use Context7?

[Context7](https://context7.com) solves this well for public libraries. DocuRAG adds three things:

| | Context7 | DocuRAG |
| --- | --- | --- |
| Hosting | Hosted service | **Self-hosted**: runs on your laptop or your servers |
| Private / internal docs | No | **Yes**: point it at any URL your machine can reach |
| Retrieval quality | Not published | **Measured**: Recall@5 = 0.94 on a public 84-question eval set ([details](#results)) |

---

## Quickstart

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/uzairharoon-dev/Docs-Spider.git
cd Docs-Spider
uv sync

uv run docurag add https://fastapi.tiangolo.com/ --name fastapi --include /tutorial/
uv run docurag search "dependency injection" --lib fastapi
uv run docurag update fastapi        # later: unchanged pages are skipped
```

The first `add` downloads the embedding model (`BAAI/bge-small-en-v1.5`, ~70 MB). Everything
runs locally on CPU: no API key, no server.

Or with Docker: `docker compose up -d` starts the REST API and MCP server on
`http://localhost:8000` ([details](https://uzairharoon-dev.github.io/Docs-Spider/getting-started/#or-with-docker)).

> **Windows / Git Bash:** Git Bash turns arguments like `/tutorial/` into file paths.
> Run `export MSYS_NO_PATHCONV=1` first, or use PowerShell.

## Connect your assistant

```bash
# Claude Code, local over stdio
claude mcp add docurag -- uv run --directory /path/to/Docs-Spider docurag serve --mcp

# Claude Code, against a running server or the Docker container
claude mcp add --transport http docurag http://localhost:8000/mcp
```

The MCP server offers `search_docs`, `list_libraries` and `get_page`. Setup for Cursor and
other clients: [Use it from your assistant](https://uzairharoon-dev.github.io/Docs-Spider/assistants/).

---

## Features

- **Crawler:** async and polite (robots.txt per RFC 9309, 2 req/s per domain, retries with
  backoff), resumable after a crash, round-robin across domains. Seeds from a URL, a
  `sitemap.xml` or an `llms.txt`.
- **Cheap re-crawls:** sitemap `<lastmod>` and conditional GETs. Re-crawling the 23-page
  httpx docs fetched zero pages. Removed pages leave the index.
- **Versions:** read from URLs (`/0.115/`, `/v2/`, `/en/stable/`); `latest` resolves to the
  newest indexed release.
- **JavaScript sites:** optional headless Chromium, only for pages that need it.
- **Clean Markdown:** main-content detection for MkDocs, Sphinx and Docusaurus, chrome
  stripped, code and tables kept, SHA-256 + SimHash dedup, `page#section` anchors.
- **Index:** heading-aware chunks, local embeddings (or any OpenAI-compatible API),
  SQLite or Qdrant.
- **Search:** hybrid vector + BM25 with reciprocal rank fusion, optional cross-encoder
  reranker, every hit cited.
- **Serving:** MCP (stdio and streamable HTTP), REST API with an optional API key, CLI,
  Docker Compose.

How each part works: [How it works](https://uzairharoon-dev.github.io/Docs-Spider/how-it-works/).

---

## Results

### Retrieval quality

84 developer-style questions over FastAPI, httpx and Typer docs (167 pages, 1,008 chunks):

| Config | Recall@1 | Recall@5 | Recall@10 | MRR@10 |
| --- | ---: | ---: | ---: | ---: |
| vector | 0.762 | 0.917 | 0.952 | 0.832 |
| bm25 | 0.643 | 0.881 | 0.940 | 0.736 |
| **hybrid** (default) | **0.774** | **0.940** | **0.976** | **0.847** |
| hybrid + rerank | 0.798 | 0.952 | 0.976 | 0.855 |

Hybrid beats either retriever alone on every metric. Method, labels and how to reproduce:
[Retrieval evaluation](https://uzairharoon-dev.github.io/Docs-Spider/evaluation/).

### Performance

Measured on a laptop with an 8th-gen Intel Core (4 cores), against the spec's targets:

| Target | Result |
| --- | --- |
| Crawl ≥ 20 pages/s across domains | **23.8 pages/s** at 2 req/s per domain; 41.2 unthrottled |
| Search p95 < 300 ms (no reranker) | **26 ms** hybrid, 17 ms vector, 7 ms BM25 |
| Runs on a laptop | **358 MB** RAM while serving; 7.9 KB of disk per chunk |

Details and how to re-run them: [Benchmarks](https://uzairharoon-dev.github.io/Docs-Spider/benchmarks/).

---

## Documentation

- [Getting started](https://uzairharoon-dev.github.io/Docs-Spider/getting-started/): install, Docker, JS sites
- [CLI](https://uzairharoon-dev.github.io/Docs-Spider/cli/) and [configuration](https://uzairharoon-dev.github.io/Docs-Spider/configuration/)
- [REST API](https://uzairharoon-dev.github.io/Docs-Spider/rest-api/)
- [How it works](https://uzairharoon-dev.github.io/Docs-Spider/how-it-works/): architecture and data model
- [Limitations](https://uzairharoon-dev.github.io/Docs-Spider/limitations/)

The site is built from [`docs/`](docs/) with MkDocs Material.

## Development

```bash
uv sync                 # install with dev dependencies
uv run pytest           # tests: a fake docs site, no network, no model download
uv run ruff check .     # lint
```

CI runs lint and tests on Ubuntu and Windows and builds the Docker image. More in
[Development](https://uzairharoon-dev.github.io/Docs-Spider/development/).

