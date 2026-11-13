# Configuration

DocuRAG reads `docurag.yaml` from the current directory, or the file given with `--config`.
CLI flags override it. Data is stored in `~/.docurag/docurag.db` unless `DOCURAG_HOME` or
`data_dir` says otherwise.

```yaml
# data_dir: ~/.docurag        # SQLite database location (or set DOCURAG_HOME)
concurrency: 16              # concurrent fetches across all domains
# extract_processes: null    # HTML -> Markdown worker processes; null = min(4, CPUs - 1)

libraries:
  - name: fastapi
    start_urls: [https://fastapi.tiangolo.com/]
    include: ["/tutorial/", "/advanced/"]
    exclude: ["/zh/", "/ja/"]
    render_js: false          # false | auto | true (auto/true need the `js` extra)
    rate_limit: 2             # requests per second for this library's domains
    # max_pages: 500
    # max_depth: 20
    # version: latest          # a fixed label for every page turns detection off
    # version_detection: auto  # auto | off
    # version_pattern: '^/docs/(?P<version>[^/]+)/'
    # discover_sitemaps: true
    # trust_sitemap_lastmod: true
    # scope_prefixes: ["/docs/"]   # default: derived from start_urls

embedder:
  provider: local             # local | openai | hash
  model: BAAI/bge-small-en-v1.5
  # batch_size: 32
  # cpu_mem_arena: false      # true trades ~2 GB of RAM for no measurable speed-up
  # provider: openai
  # model: text-embedding-3-small
  # base_url: https://api.openai.com/v1   # any OpenAI-compatible endpoint
  # api_key_env: OPENAI_API_KEY

store:
  type: sqlite                # no server needed
  # type: qdrant
  # url: http://localhost:6333   # omit to run Qdrant embedded under data_dir

chunking: {min_tokens: 300, max_tokens: 512, overlap: 0.12}

search:
  mode: hybrid                # vector | bm25 | hybrid
  candidates: 50              # per retriever, before fusion
  rrf_k: 60
  rerank: false
  reranker_model: Xenova/ms-marco-MiniLM-L-6-v2
  rerank_candidates: 20
```

## Notes

- **Changing the embedder** re-indexes pages on the next `index` / `update`. Vectors from
  different models are never mixed.
- **Qdrant** needs the extra: `uv sync --extra qdrant`.
- **`trust_sitemap_lastmod`**: turn it off for sites whose sitemaps carry stale dates, or
  run `docurag update NAME --full` once.
- **`extract_processes: 0`** extracts in a thread of the crawler process instead of separate
  processes. Useful where process pools are unavailable.

## Environment variables

| Variable | Used for |
| --- | --- |
| `DOCURAG_HOME` | Data directory (default `~/.docurag`) |
| `DOCURAG_API_KEY` | Require `Authorization: Bearer <key>` on the REST API and `/mcp` |
| `OPENAI_API_KEY` | The `openai` embedder (name set by `api_key_env`) |
| `QDRANT_API_KEY` | A Qdrant server with authentication (name set by `store.api_key_env`) |
