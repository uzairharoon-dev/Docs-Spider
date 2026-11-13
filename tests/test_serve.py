import sys
import time

import pytest
import yaml
from fastapi.testclient import TestClient
from mcp.client.client import Client
from mcp.client.stdio import StdioServerParameters

from docurag.config import EmbedderConfig, Settings
from docurag.db import Database
from docurag.index.embedder import HashEmbedder
from docurag.serve.api import create_app
from docurag.serve.mcp_server import create_mcp_server
from docurag.serve.service import CrawlInProgress, DocuRAGService, resolve_library
from tests.conftest import default_site
from tests.test_index import add_pages, make_indexer

DIM = 256


@pytest.fixture
def srv_settings(tmp_path) -> Settings:
    s = Settings(data_dir=tmp_path, concurrency=4, max_retries=1,
                 embedder=EmbedderConfig(provider="hash", dim=DIM))
    db = Database(s.db_path)
    lib_id = add_pages(db)
    make_indexer(db, embedder=HashEmbedder(DIM))[0].index_library(lib_id)
    db.close()
    return s


@pytest.fixture
def site():
    return default_site()


@pytest.fixture
def client(srv_settings, site):
    service = DocuRAGService(srv_settings, http_client_factory=site.client)
    with TestClient(create_app(srv_settings, service=service, warm_up=False)) as c:
        yield c


# ---------------------------------------------------------------- service
def test_resolve_library_order(srv_settings):
    from docurag.config import LibraryConfig

    db = Database(srv_settings.db_path)
    srv_settings.libraries = [LibraryConfig(name="cfg", start_urls=["https://a.test/"],
                                            include=["/x/"])]
    lib = resolve_library(srv_settings, db, "cfg", None, {"max_pages": 3, "include": None})
    assert lib.include == ["/x/"] and lib.max_pages == 3
    lib = resolve_library(srv_settings, db, "cfg", "https://b.test/docs/")
    assert lib.start_urls == ["https://b.test/docs/"]
    assert resolve_library(srv_settings, db, "new", "https://c.test/").name == "new"
    with pytest.raises(LookupError):
        resolve_library(srv_settings, db, "nope")
    with pytest.raises(ValueError):
        resolve_library(srv_settings, db, "new", "ftp://c.test/")
    db.close()


async def test_service_rejects_parallel_crawl_of_same_library(srv_settings, site):
    service = DocuRAGService(srv_settings, http_client_factory=site.client)
    lib = service.resolve_library("site", "http://docs.test/", {"exclude": ["/zh/"]})
    job_id = service.start_crawl(lib, index=False)
    with pytest.raises(CrawlInProgress):
        service.start_crawl(lib)
    await service.wait_for_crawls()
    assert service.get_job(job_id)["status"] == "completed"
    await service.aclose()


# ---------------------------------------------------------------- REST
def test_health_and_libraries(client):
    assert client.get("/health").json()["status"] == "ok"
    libs = client.get("/libraries").json()["libraries"]
    assert libs[0]["name"] == "demo" and libs[0]["chunks"] > 0 and libs[0]["crawling"] is False


def test_search_endpoint(client):
    res = client.post("/search", json={"query": "cors middleware", "library": "demo", "k": 2})
    assert res.status_code == 200
    body = res.json()
    assert body["mode"] == "hybrid"
    assert body["results"][0]["url"] == "http://docs.test/cors/"
    assert body["results"][0]["citation"] == "http://docs.test/cors/#cors"
    assert set(body["results"][0]["scores"]) >= {"vector", "bm25", "rrf"}

    bm25 = client.post("/search", json={"query": "cookie", "mode": "bm25"}).json()
    assert bm25["mode"] == "bm25" and bm25["results"]
    assert client.post("/search", json={"query": "x", "library": "nope"}).status_code == 404
    assert client.post("/search", json={"query": ""}).status_code == 422
    assert client.post("/search", json={"query": "x", "mode": "magic"}).status_code == 422


def test_pages_and_jobs_endpoints(client):
    page = client.get("/pages", params={"url": "http://docs.test/cors/#cors"}).json()
    assert page["title"] == "CORS" and page["markdown"].startswith("# CORS")
    assert client.get("/pages", params={"url": "http://docs.test/missing/"}).status_code == 404
    assert client.get("/jobs/999").status_code == 404


def test_crawl_endpoint_runs_crawl_and_index(client):
    res = client.post("/crawl", json={"name": "site", "url": "http://docs.test/",
                                      "exclude": ["/zh/"], "rate_limit": 20})
    assert res.status_code == 202, res.text
    job_id = res.json()["job_id"]
    # same library while running -> 409
    assert client.post("/crawl", json={"name": "site", "url": "http://docs.test/"}).status_code \
        == 409

    deadline = time.time() + 20
    job = {}
    while time.time() < deadline:
        job = client.get(f"/jobs/{job_id}").json()
        if job["status"] != "running" and job["index"] in ("done", None) or (
            str(job["index"]).startswith("failed")
        ):
            break
        time.sleep(0.1)
    assert job["status"] == "completed" and job["index"] == "done", job
    hits = client.post("/search", json={"query": "orphan sitemap", "library": "site",
                                        "mode": "bm25"}).json()["results"]
    assert hits[0]["url"] == "http://docs.test/guide/orphan"


def test_crawl_endpoint_errors(client):
    assert client.post("/crawl", json={"name": "unknown"}).status_code == 404
    assert client.post("/crawl", json={"name": "x", "url": "ftp://a"}).status_code == 422
    assert client.post("/crawl", json={"name": "x", "url": "http://a.test",
                                       "rate_limit": 500}).status_code == 422


def test_api_key(srv_settings, monkeypatch):
    monkeypatch.setenv("DOCURAG_API_KEY", "s3cret")
    with TestClient(create_app(srv_settings, warm_up=False)) as c:
        assert c.get("/health").status_code == 200
        assert c.get("/libraries").status_code == 401
        assert c.get("/libraries", headers={"Authorization": "Bearer wrong"}).status_code == 401
        ok = c.get("/libraries", headers={"Authorization": "Bearer s3cret"})
        assert ok.status_code == 200
        assert c.post("/mcp", json={}).status_code == 401  # MCP is behind the key too


# ---------------------------------------------------------------- MCP
def _text(result) -> str:
    return "".join(getattr(c, "text", "") for c in result.content)


async def test_mcp_tools_in_memory(srv_settings):
    service = DocuRAGService(srv_settings)
    async with Client(create_mcp_server(service)) as mcp:
        names = {t.name for t in (await mcp.list_tools()).tools}
        assert names == {"search_docs", "list_libraries", "get_page"}

        out = _text(await mcp.call_tool("search_docs", {"query": "cookie class",
                                                        "library": "demo", "k": 2}))
        assert "Source: http://docs.test/cookies/#" in out
        assert "## 1. Cookie Parameters" in out

        out = _text(await mcp.call_tool("search_docs", {"query": "x", "library": "nope"}))
        assert "Unknown library 'nope'" in out and "demo" in out

        assert "| demo |" in _text(await mcp.call_tool("list_libraries", {}))

        page = _text(await mcp.call_tool("get_page", {"url": "http://docs.test/cors/#cors"}))
        assert page.startswith("# CORS") and "Cross-origin" in page
        short = _text(await mcp.call_tool("get_page", {"url": "http://docs.test/cors/",
                                                       "max_chars": 10}))
        assert "[truncated" in short
        assert "not indexed" in _text(await mcp.call_tool("get_page", {"url": "http://x.test/"}))
    service.db.close()


async def test_mcp_over_stdio_subprocess(srv_settings, tmp_path):
    """The real `docurag serve --mcp` process: nothing but protocol may reach stdout."""
    cfg = tmp_path / "docurag.yaml"
    cfg.write_text(yaml.safe_dump({
        "data_dir": str(srv_settings.data_dir), "embedder": {"provider": "hash", "dim": DIM},
    }))
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "docurag.serve.cli", "-c", str(cfg), "serve", "--mcp"],
    )
    async with Client(params, read_timeout_seconds=60) as mcp:
        out = _text(await mcp.call_tool("search_docs", {"query": "cors", "library": "demo"}))
        assert "http://docs.test/cors/" in out
