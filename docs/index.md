# DocuRAG

**Up-to-date, versioned library docs for AI coding assistants. Self-hosted, and it works on
private docs too.**

AI coding assistants stop learning at their training cutoff. As a result they suggest
deprecated APIs, mix up library versions, and know nothing about your team's internal docs.

DocuRAG fixes that. It:

1. **crawls** any docs site, such as FastAPI, Next.js or your company wiki,
2. **indexes** the pages by version, and
3. **serves** them to Claude Code, Cursor and other assistants through an MCP server.

The assistant then answers from current docs and cites them.

```
You:        How do I declare cookie parameters in FastAPI?
Assistant:  (calls search_docs("cookie parameters", library="fastapi"))
            Use Annotated[str | None, Cookie()] ...
            Source: https://fastapi.tiangolo.com/tutorial/cookie-params/ > Cookie Parameters
```

![DocuRAG demo](assets/demo.gif)

## Why not just use Context7?

[Context7](https://context7.com) solves this well for public libraries. DocuRAG adds three things:

| | Context7 | DocuRAG |
| --- | --- | --- |
| Hosting | Hosted service | **Self-hosted**: runs on your laptop or your servers |
| Private / internal docs | No | **Yes**: point it at any URL your machine can reach |
| Retrieval quality | Not published | **Measured**: Recall@5 = 0.94 on a public 84-question eval set ([details](evaluation.md)) |

## At a glance

- **Crawler:** async, polite (robots.txt, 2 req/s per domain), resumable, incremental
  re-crawls with sitemap `<lastmod>` and conditional GETs, optional Chromium for JS sites.
- **Index:** heading-aware chunks, local embeddings (no API key), SQLite or Qdrant.
- **Search:** hybrid vector + BM25 with rank fusion, optional cross-encoder reranker,
  results cited down to the section.
- **Serving:** MCP (stdio and HTTP), REST API, CLI, Docker Compose.
- **Measured:** an [84-question retrieval eval](evaluation.md) and
  [crawl and search benchmarks](benchmarks.md).

Next: [Getting started](getting-started.md).
