"""REST API (F17), with the MCP server mounted at ``/mcp`` (streamable HTTP).

    POST /search        hybrid search with citations
    POST /crawl         start a background crawl + index; returns a job id
    GET  /libraries     indexed libraries
    GET  /jobs/{id}     crawl job status and stats
    GET  /pages?url=    one page as Markdown
    GET  /health        liveness
    *    /mcp           MCP over streamable HTTP

Set ``DOCURAG_API_KEY`` to require ``Authorization: Bearer <key>`` on every route but /health.
"""

from __future__ import annotations

import hmac
import logging
import os
import threading
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from docurag import __version__
from docurag.config import Settings
from docurag.index.search import UnknownLibrary
from docurag.serve.service import CrawlInProgress, DocuRAGService

log = logging.getLogger(__name__)
API_KEY_ENV = "DOCURAG_API_KEY"


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    library: str | None = None
    version: str | None = None
    k: int = Field(default=5, ge=1, le=50)
    mode: Literal["vector", "bm25", "hybrid"] | None = None
    rerank: bool | None = None


class CrawlRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    url: str | None = Field(default=None, description="Start URL; optional for known libraries")
    include: list[str] | None = None
    exclude: list[str] | None = None
    version: str | None = None
    max_pages: int | None = Field(default=None, gt=0)
    max_depth: int | None = Field(default=None, ge=0)
    rate_limit: float | None = Field(default=None, gt=0, le=20)
    fresh: bool = False
    index: bool = True


def create_app(
    settings: Settings,
    *,
    service: DocuRAGService | None = None,
    mount_mcp: bool = True,
    host: str = "127.0.0.1",
    warm_up: bool = True,
) -> FastAPI:
    service = service or DocuRAGService(settings)
    api_key = os.environ.get(API_KEY_ENV) or None
    mcp_app = None
    mcp = None
    if mount_mcp:
        from docurag.serve.mcp_server import create_mcp_server

        mcp = create_mcp_server(service)
        mcp_app = mcp.streamable_http_app(streamable_http_path="/mcp", host=host)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if warm_up:
            threading.Thread(target=service.warm_up, daemon=True).start()
        async with AsyncExitStack() as stack:
            if mcp is not None:
                await stack.enter_async_context(mcp.session_manager.run())
            yield
            await service.aclose()

    app = FastAPI(
        title="DocuRAG",
        version=__version__,
        summary="Up-to-date, versioned library docs for AI coding assistants.",
        lifespan=lifespan,
    )
    app.state.service = service

    @app.middleware("http")
    async def require_api_key(request: Request, call_next):
        if api_key and request.url.path != "/health":
            header = request.headers.get("authorization", "")
            token = header[7:] if header.lower().startswith("bearer ") else ""
            if not hmac.compare_digest(token.encode(), api_key.encode()):
                return JSONResponse({"detail": "missing or invalid API key"}, status_code=401)
        return await call_next(request)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.post("/search")
    def search(req: SearchRequest) -> dict[str, Any]:
        try:
            hits, took, label = service.search(
                req.query, req.library, req.version, req.k, req.mode, req.rerank
            )
        except UnknownLibrary as exc:
            raise HTTPException(404, f"unknown library {req.library!r}") from exc
        return {
            "query": req.query,
            "mode": label,
            "took_ms": round(took, 1),
            "results": [h.as_dict() for h in hits],
        }

    @app.get("/libraries")
    def libraries() -> dict[str, Any]:
        return {"libraries": service.list_libraries()}

    @app.get("/pages")
    def page(url: str = Query(min_length=1), library: str | None = None) -> dict[str, Any]:
        found = service.get_page(url.split("#")[0], library)
        if found is None:
            raise HTTPException(404, "page not indexed")
        return found

    @app.get("/jobs/{job_id}")
    def job(job_id: int) -> dict[str, Any]:
        found = service.get_job(job_id)
        if found is None:
            raise HTTPException(404, "no such job")
        return found

    @app.post("/crawl", status_code=202)
    async def crawl(req: CrawlRequest) -> dict[str, Any]:
        overrides = req.model_dump(include={
            "include", "exclude", "version", "max_pages", "max_depth", "rate_limit"
        })
        try:
            lib = service.resolve_library(req.name, req.url, overrides)
            job_id = service.start_crawl(lib, fresh=req.fresh, index=req.index)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except CrawlInProgress as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"job_id": job_id, "library": lib.name, "status_url": f"/jobs/{job_id}"}

    if mcp_app is not None:
        app.mount("/", mcp_app)  # last: API routes match first; MCP owns /mcp
    return app
