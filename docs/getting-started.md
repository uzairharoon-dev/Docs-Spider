# Getting started

## Install

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/uzairharoon-dev/Docs-Spider.git
cd Docs-Spider
uv sync
```

## Crawl, index and search

```bash
# Crawl and index the FastAPI tutorial
uv run docurag add https://fastapi.tiangolo.com/ --name fastapi --include /tutorial/

# Search it
uv run docurag search "dependency injection" --lib fastapi

# See what was indexed
uv run docurag libraries
uv run docurag page https://fastapi.tiangolo.com/tutorial/first-steps/

# Later: re-crawl and re-index. Unchanged pages are skipped.
uv run docurag update fastapi
```

The first `add` downloads the default embedding model (`BAAI/bge-small-en-v1.5`, ~70 MB)
into `~/.docurag/models`. Everything runs locally on CPU: no API key, no server.

!!! note "Windows / Git Bash"
    Git Bash turns arguments like `/tutorial/` into file paths
    (`C:/Program Files/Git/tutorial/`). Run `export MSYS_NO_PATHCONV=1` first, or use PowerShell.

## Or with Docker

```bash
docker compose up -d                       # API + MCP on http://localhost:8000
curl -X POST localhost:8000/crawl -H 'content-type: application/json' \
     -d '{"name": "fastapi", "url": "https://fastapi.tiangolo.com/", "include": ["/tutorial/"]}'
curl localhost:8000/jobs/1                 # wait for "status": "completed", "index": "done"
```

Data and model files live in the `docurag-data` volume. `docurag.yaml` is mounted read-only.
`docker compose --profile qdrant up -d` also starts a Qdrant server.

## JavaScript-rendered sites

Sites that build their pages in the browser need Chromium:

```bash
uv sync --extra js && uv run playwright install chromium
uv run docurag add https://example.com/docs/ --name example --render-js auto
```

`auto` renders only pages whose static HTML is an empty app shell. For Docker, build with
`DOCURAG_WITH_JS=true docker compose build` (about 3 GB instead of 1 GB).

Next: [connect your assistant](assistants.md).
