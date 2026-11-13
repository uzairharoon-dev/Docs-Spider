"""Long-lived application service shared by the REST API and the MCP server.

Owns one SQLite connection for reads, loads models lazily (once), and runs crawls as
background asyncio tasks, each with its own SQLite connection (WAL lets them write
while searches keep reading).
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections.abc import Callable
from typing import Any

import httpx

from docurag.config import LibraryConfig, Settings
from docurag.crawler.crawler import Crawler
from docurag.db import Database
from docurag.index.pipeline import Indexer
from docurag.index.search import MODES, Searcher, SearchHit
from docurag.urls import normalize_url

log = logging.getLogger(__name__)


class CrawlInProgress(RuntimeError):
    pass


def resolve_library(
    settings: Settings,
    db: Database,
    name: str,
    url: str | None = None,
    overrides: dict[str, Any] | None = None,
) -> LibraryConfig:
    """Library config from, in order: docurag.yaml, the DB (a library added earlier), ``url``.

    ``url`` replaces the start URLs; ``overrides`` (None values ignored) replace fields.
    Raises LookupError for an unknown library without a URL, ValueError for bad input.
    """
    base = settings.library(name)
    if base is not None:
        data = base.model_dump()
    elif (row := db.get_library(name)) is not None:
        data = json.loads(row["config_json"])
    elif url:
        data = {"name": name}
    else:
        raise LookupError(f"unknown library {name!r}; pass a start URL to add it")
    if url:
        if normalize_url(url) is None:
            raise ValueError(f"not an http(s) URL: {url}")
        data["start_urls"] = [url]
    data.update({k: v for k, v in (overrides or {}).items() if v is not None})
    return LibraryConfig.model_validate(data)


class DocuRAGService:
    def __init__(
        self,
        settings: Settings,
        *,
        http_client_factory: Callable[[], httpx.AsyncClient] | None = None,
    ):
        self.settings = settings
        self.db = Database(settings.db_path)
        self._lock = threading.RLock()  # serialises use of self.db and model loading
        self._embedder = None
        self._store = None
        self._reranker = None
        self._client_factory = http_client_factory
        self._crawls: dict[str, asyncio.Task] = {}
        self._index_state: dict[int, str] = {}  # job id -> pending | running | done | failed: …

    # ------------------------------------------------------------ models
    def _get_embedder_and_store(self):
        from docurag.index.embedder import make_embedder
        from docurag.index.store import make_store

        with self._lock:
            if self._embedder is None:
                self._embedder = make_embedder(self.settings.embedder, self.settings.data_dir)
                self._store = make_store(
                    self.settings.store, self.db, self.settings.data_dir, self._embedder.name
                )
            return self._embedder, self._store

    def _get_reranker(self):
        from docurag.index.rerank import FastEmbedReranker

        with self._lock:
            if self._reranker is None:
                self._reranker = FastEmbedReranker(
                    self.settings.search.reranker_model,
                    self.settings.embedder.cache_dir or self.settings.data_dir / "models",
                )
            return self._reranker

    def warm_up(self) -> None:
        """Load the embedding model now, so the first query isn't slow. Best effort."""
        try:
            self._get_embedder_and_store()
        except Exception:
            log.exception("could not load the embedding model")

    # ------------------------------------------------------------ queries
    def search(
        self,
        query: str,
        library: str | None = None,
        version: str | None = None,
        k: int = 5,
        mode: str | None = None,
        rerank: bool | None = None,
    ) -> tuple[list[SearchHit], float, str]:
        """Returns (hits, milliseconds, mode label). Raises UnknownLibrary, ValueError."""
        cfg = self.settings.search
        mode = mode or cfg.mode
        if mode not in MODES:
            raise ValueError(f"mode must be one of {', '.join(MODES)}")
        rerank = cfg.rerank if rerank is None else rerank
        embedder = store = reranker = None
        if mode != "bm25":
            embedder, store = self._get_embedder_and_store()
        if rerank:
            reranker = self._get_reranker()
        searcher = Searcher(
            self.db, embedder, store, reranker,
            candidates=cfg.candidates, rrf_k=cfg.rrf_k, rerank_candidates=cfg.rerank_candidates,
        )
        with self._lock:
            started = time.perf_counter()
            hits = searcher.search(query, library, version, k, mode=mode, rerank=rerank)
            took = (time.perf_counter() - started) * 1000
        return hits, took, mode + ("+rerank" if rerank else "")

    def list_libraries(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.db.list_libraries()
        for r in rows:
            r["crawling"] = r["name"] in self._crawls
        return rows

    def get_page(self, url: str, library: str | None = None) -> dict[str, Any] | None:
        with self._lock:
            rows = self.db.find_pages_by_url(normalize_url(url) or url)
        if library is not None:
            rows = [r for r in rows if r["library"] == library]
        if not rows:
            return None
        r = rows[0]
        return {
            "library": r["library"], "version": r["version"], "url": r["url"],
            "title": r["title"], "fetched_at": r["fetched_at"], "markdown": r["markdown"],
        }

    def get_job(self, job_id: int) -> dict[str, Any] | None:
        with self._lock:
            job = self.db.get_job(job_id)
        if job is not None:
            job["index"] = self._index_state.get(job_id)
        return job

    def resolve_library(
        self, name: str, url: str | None = None, overrides: dict[str, Any] | None = None
    ) -> LibraryConfig:
        with self._lock:
            return resolve_library(self.settings, self.db, name, url, overrides)

    def library_names(self) -> list[str]:
        with self._lock:
            return [r["name"] for r in self.db.list_libraries()]

    # ------------------------------------------------------------ crawling
    def start_crawl(self, lib: LibraryConfig, *, fresh: bool = False, index: bool = True) -> int:
        """Start crawling (and then indexing) in the background. Returns the job id.

        Must be called from inside the running event loop.
        """
        if lib.name in self._crawls:
            raise CrawlInProgress(f"{lib.name} is already being crawled")
        db = Database(self.settings.db_path)  # the crawl writes through its own connection
        try:
            client = self._client_factory() if self._client_factory else None
            crawler = Crawler(db, lib, self.settings, client=client)
            job_id = crawler.prepare(fresh=fresh)
        except Exception:
            db.close()
            raise
        if index:
            self._index_state[job_id] = "pending"
        task = asyncio.get_running_loop().create_task(
            self._crawl_then_index(crawler, db, client, job_id, index), name=f"crawl-{lib.name}"
        )
        self._crawls[lib.name] = task
        return job_id

    async def _crawl_then_index(self, crawler, db, client, job_id: int, index: bool) -> None:
        name = crawler.lib.name
        try:
            await crawler.run()
            if index:
                self._index_state[job_id] = "running"
                await asyncio.to_thread(self._index_library, crawler.library_id, db)
                self._index_state[job_id] = "done"
        except asyncio.CancelledError:
            if index:
                self._index_state[job_id] = "failed: cancelled"
            raise
        except Exception as exc:
            log.exception("crawl/index of %s failed", name)
            if index:
                self._index_state[job_id] = f"failed: {type(exc).__name__}: {exc}"
        finally:
            if client is not None:
                await client.aclose()
            db.close()
            self._crawls.pop(name, None)

    def _index_library(self, library_id: int, db: Database) -> None:
        from docurag.index.store import SQLiteVectorStore

        embedder, shared_store = self._get_embedder_and_store()
        # sqlite: write through the crawl's own connection; other stores are thread-safe clients
        store = (
            SQLiteVectorStore(db, embedder.name)
            if self.settings.store.type == "sqlite" else shared_store
        )
        Indexer(db, embedder, store, self.settings.chunking).index_library(library_id)

    async def wait_for_crawls(self) -> None:
        tasks = list(self._crawls.values())
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def aclose(self) -> None:
        for task in list(self._crawls.values()):
            task.cancel()  # the crawler marks its job "interrupted"; it resumes next time
        await self.wait_for_crawls()
        self.db.close()
