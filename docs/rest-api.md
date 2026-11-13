# REST API

`docurag serve` (or the Docker image) starts the API on port 8000. Interactive OpenAPI docs
are at `/docs`.

| Endpoint | Description |
| --- | --- |
| `POST /search` | `{"query", "library"?, "version"?, "k"?, "mode"?, "rerank"?}` → cited results with every retriever's score |
| `POST /crawl` | `{"name", "url"?, "include"?, "exclude"?, "max_pages"?, ...}` → `202 {"job_id"}`; crawls and then indexes in the background |
| `GET /jobs/{id}` | Job status, crawl stats, errors and indexing state |
| `GET /libraries` | Indexed libraries |
| `GET /pages?url=` | One page as Markdown |
| `GET /health` | Liveness (never needs a key) |
| `/mcp` | MCP over streamable HTTP |

## Examples

```bash
# Add a library and wait for it
curl -X POST localhost:8000/crawl -H 'content-type: application/json' \
     -d '{"name": "httpx", "url": "https://www.python-httpx.org/"}'
curl localhost:8000/jobs/1

# Search
curl -s -X POST localhost:8000/search -H 'content-type: application/json' \
     -d '{"query": "set a cookie on the response", "library": "fastapi", "k": 3}'
```

`POST /crawl` for a library that is already crawling returns `409`. A crawl interrupted by a
server restart resumes on the next `POST /crawl` for that library.

!!! warning "Security"
    The server binds to `127.0.0.1` by default. `POST /crawl` makes the server fetch any URL
    you give it, so before exposing the port set `DOCURAG_API_KEY`. Every route except
    `/health`, `/mcp` included, then requires `Authorization: Bearer <key>`.
