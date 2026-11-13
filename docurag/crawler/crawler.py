"""Crawl orchestration: frontier -> fetch -> extract -> dedup -> store."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx

from docurag.config import LibraryConfig, Settings
from docurag.crawler.fetcher import Fetcher, FetchResult
from docurag.crawler.frontier import Frontier
from docurag.crawler.ratelimit import DomainRateLimiter
from docurag.crawler.renderer import (
    PlaywrightRenderer,
    Renderer,
    RendererUnavailable,
    looks_like_js_shell,
)
from docurag.crawler.robots import RobotsCache
from docurag.crawler.seeds import expand_seeds
from docurag.crawler.versions import compile_pattern, detect_version
from docurag.db import Database
from docurag.extract import ExtractedPage, extract_html, extract_markdown
from docurag.extract.dedup import DedupIndex, content_hash, simhash
from docurag.urls import Scope, normalize_url

log = logging.getLogger(__name__)

HTML_TYPES = {"text/html", "application/xhtml+xml"}
MARKDOWN_TYPES = {"text/markdown", "text/x-markdown"}
MIN_MARKDOWN_CHARS = 40
PROGRESS_EVERY = 25

# Outcome names, also the keys of the job stats.
SAVED, UNCHANGED, DUPLICATE = "saved", "unchanged", "duplicate"
SKIPPED, FAILED, REMOVED = "skipped", "failed", "removed"

EventCallback = Callable[["CrawlEvent"], None]


class _BudgetExhausted(Exception):
    """max_pages reached before this URL was fetched; it stays pending."""


@dataclass
class CrawlEvent:
    url: str
    outcome: str
    detail: str = ""
    stats: dict[str, int] | None = None
    queued: int = 0


def _kind(res: FetchResult) -> str | None:
    ctype = res.content_type
    path = urlsplit(res.final_url).path.lower()
    if ctype in HTML_TYPES or (not ctype and path.endswith((".html", ".htm", "/"))):
        return "html"
    if ctype in MARKDOWN_TYPES or (ctype == "text/plain" and path.endswith((".md", ".txt"))):
        return "markdown"
    return None


def parse_time(value: str | None) -> datetime | None:
    """W3C datetime from a sitemap (date or datetime) or our own ISO timestamps, as UTC."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


class Crawler:
    def __init__(
        self,
        db: Database,
        library: LibraryConfig,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
        on_event: EventCallback | None = None,
        renderer: Renderer | None = None,
    ):
        self.db = db
        self.lib = library
        self.settings = settings
        self.scope = Scope.from_config(
            library.start_urls, library.include, library.exclude, library.scope_prefixes
        )
        self._client = client
        self._on_event = on_event
        self.stats: Counter[str] = Counter()
        self._inflight = 0
        self._fetched = 0
        self._reserved = 0  # fetch slots taken against max_pages
        self._stop = False
        self.job_id: int | None = None
        self._renderer = renderer
        self._own_renderer = False
        self._pool: ProcessPoolExecutor | None = None
        self._dedups: dict[str, DedupIndex] = {}  # per version: v2 is no duplicate of v1
        # An explicit version labels every page; otherwise URLs may carry one.
        self._detect = library.version == "latest" and library.version_detection == "auto"
        self._version_rx = (
            compile_pattern(library.version_pattern) if library.version_pattern else None
        )

    # ------------------------------------------------------------------
    def prepare(self, *, fresh: bool = False) -> int:
        """Register the library and create or resume its job, without any network I/O.

        Returns the job id, so callers (e.g. the REST API) can report it before crawling.
        """
        lib_id = self.db.upsert_library(
            self.lib.name, self.lib.start_urls[0], self.lib.model_dump(mode="json")
        )
        self.library_id = lib_id

        job = None if fresh else self.db.resumable_job(lib_id)
        if job is not None:
            job_id = job["id"]
            self.frontier = Frontier(self.db, job_id)
            requeued = self.frontier.reset_in_progress()
            self.stats.update(self.db.job_stats(job_id))
            self._fetched = self._reserved = job["pages_done"]
            log.info("resuming job %s (%s URLs re-queued)", job_id, requeued)
        else:
            job_id = self.db.create_job(lib_id)
            self.frontier = Frontier(self.db, job_id)
        # A job that died before its seeds were stored has an empty frontier: seed it again.
        self._needs_seeds = job is None or not self.frontier.counts()
        self.job_id = job_id
        return job_id

    async def run(self, *, fresh: bool = False) -> int:
        """Crawl the library and return the job id. Resumes an unfinished job unless ``fresh``."""
        job_id = self.job_id if self.job_id is not None else self.prepare(fresh=fresh)
        lib_id = self.library_id
        own_client = self._client is None
        client = self._client or httpx.AsyncClient(
            timeout=self.settings.request_timeout,
            limits=httpx.Limits(max_connections=self.settings.concurrency * 2),
            headers={"User-Agent": self.settings.user_agent},
        )
        self.limiter = DomainRateLimiter(self.lib.rate_limit)
        self.robots = RobotsCache(client, self.settings.user_agent)
        self.fetcher = Fetcher(
            client,
            self.limiter,
            user_agent=self.settings.user_agent,
            max_retries=self.settings.max_retries,
            max_bytes=self.settings.max_page_bytes,
        )
        if self.lib.render_js and self._renderer is None:
            self._renderer = PlaywrightRenderer(self.settings.user_agent)
            self._own_renderer = True
        if self.settings.extract_workers > 0:
            self._pool = ProcessPoolExecutor(self.settings.extract_workers)

        status = "failed"
        try:
            if self._needs_seeds:
                seeds = await expand_seeds(
                    self.lib.start_urls, self.scope, self.fetcher, self.robots,
                    discover_sitemaps=self.lib.discover_sitemaps,
                )
                # Re-check pages we already have (any version, including ones that went
                # away), so `update` revisits them even if nothing links to them any more.
                known = [(u, 1) for u in self.db.page_urls(lib_id)]
                self.frontier.add(seeds + known)
            n = self.settings.concurrency
            workers = [asyncio.create_task(self._worker()) for _ in range(n)]
            try:
                await asyncio.gather(*workers)
            finally:
                for w in workers:
                    w.cancel()
            # Hitting max_pages is a deliberate stop, not something to resume.
            done = self._stop or self.frontier.pending() == 0
            status = "completed" if done else "interrupted"
        except asyncio.CancelledError:
            status = "interrupted"
            raise
        finally:
            self.db.finish_job(job_id, status, self._fetched, dict(self.stats))
            if own_client:
                await client.aclose()
            if self._own_renderer and self._renderer is not None:
                await self._renderer.aclose()
            if self._pool is not None:
                self._pool.shutdown(wait=False, cancel_futures=True)
                self._pool = None
        return job_id

    async def _extract(self, fn, text: str, url: str) -> ExtractedPage:
        """Run CPU-bound extraction in the process pool (all cores) or a thread."""
        if self._pool is not None:
            return await asyncio.get_running_loop().run_in_executor(self._pool, fn, text, url)
        return await asyncio.to_thread(fn, text, url)

    # ------------------------------------------------------------------
    async def _worker(self) -> None:
        while True:
            if self._stop:
                return
            item = self.frontier.claim()
            if item is None:
                if self._inflight == 0:
                    return
                await asyncio.sleep(0.05)
                continue
            self._inflight += 1
            url, depth, lastmod = item
            try:
                outcome, detail = await self._process(url, depth, lastmod)
            except _BudgetExhausted:
                self.frontier.complete(url, "pending")
                self._stop = True
                return
            except Exception as exc:  # one bad page must not kill the crawl
                log.exception("error processing %s", url)
                outcome, detail = FAILED, f"{type(exc).__name__}: {exc}"
            finally:
                self._inflight -= 1
            self._record(url, outcome, detail)

    def _record(self, url: str, outcome: str, detail: str) -> None:
        self.stats[outcome] += 1
        frontier_status = {FAILED: "failed", SKIPPED: "skipped"}.get(outcome, "done")
        self.frontier.complete(url, frontier_status, detail or None)
        if outcome == FAILED:
            self.db.add_job_error(self.job_id, url, detail)
        if sum(self.stats.values()) % PROGRESS_EVERY == 0:
            self.db.save_job_progress(self.job_id, self._fetched, dict(self.stats))
        if self._on_event:
            self._on_event(
                CrawlEvent(url, outcome, detail, dict(self.stats), self.frontier.pending())
            )

    def _enqueue(self, links: list[str], depth: int) -> None:
        if depth > self.lib.max_depth:
            return
        keep = [(u, depth) for u in links if self.scope.in_scope(u) and self.scope.is_included(u)]
        self.frontier.add(keep)

    # ------------------------------------------------------------------ helpers
    def version_for(self, url: str) -> str:
        if self._detect:
            return detect_version(url, self._version_rx) or self.lib.version
        return self.lib.version

    def _dedup(self, version: str) -> DedupIndex:
        if version not in self._dedups:
            index = DedupIndex()
            for url, chash, h in self.db.page_fingerprints(self.library_id, version):
                index.add(url, chash, h)
            self._dedups[version] = index
        return self._dedups[version]

    def _should_render(self, html: str, page: ExtractedPage) -> bool:
        if self._renderer is None or not self.lib.render_js:
            return False
        return self.lib.render_js is True or looks_like_js_shell(html, page.markdown)

    async def _render(self, url: str) -> tuple[str, str] | None:
        """Rendered (final_url, html), or None when the browser is unavailable or fails."""
        await self.limiter.wait(urlsplit(url).netloc)
        try:
            return await self._renderer.render(url)
        except RendererUnavailable as exc:
            log.warning("%s; continuing without JS rendering", exc)
            self._renderer = None  # warn once, then crawl the static HTML
        except Exception as exc:
            log.warning("rendering %s failed (%s); using the static HTML", url, exc)
        return None

    # ------------------------------------------------------------------ one URL
    async def _process(self, url: str, depth: int, lastmod: str | None = None) -> tuple[str, str]:
        if not await self.robots.allowed(url):
            return SKIPPED, "disallowed by robots.txt"
        host = urlsplit(url).netloc
        delay = await self.robots.crawl_delay(url)
        if delay:
            self.limiter.set_min_interval(host, delay)

        version = self.version_for(url)
        existing = self.db.get_page(self.library_id, version, url)

        # F5: the sitemap says the page hasn't changed since we fetched it. No request at all.
        changed_at, fetched_at = parse_time(lastmod), parse_time(
            existing["fetched_at"] if existing else None
        )
        if self.lib.trust_sitemap_lastmod and existing is not None and not existing["gone"] \
                and changed_at and fetched_at and changed_at <= fetched_at:
            self._enqueue(json.loads(existing["links_json"]), depth + 1)
            return UNCHANGED, "sitemap lastmod older than last fetch"

        budget = self.lib.max_pages
        if budget is not None:
            if self._stop or self._reserved >= budget:
                raise _BudgetExhausted
            self._reserved += 1

        res = await self.fetcher.fetch(
            url,
            etag=existing["etag"] if existing else None,
            last_modified=existing["last_modified"] if existing else None,
        )
        self._fetched += 1
        if res.error:
            return FAILED, res.error
        if res.not_modified and existing is not None:
            self.db.touch_page(existing["id"], res.etag, res.last_modified, None)
            self._enqueue(json.loads(existing["links_json"]), depth + 1)
            return UNCHANGED, "304 not modified"
        if res.status in (404, 410):
            if existing is not None and not existing["gone"]:
                self.db.mark_page_gone(existing["id"])
                return REMOVED, f"HTTP {res.status}: page removed from the docs"
            return SKIPPED, f"HTTP {res.status}"
        if not res.ok:
            return FAILED, f"HTTP {res.status}"

        final = normalize_url(res.final_url) or url
        if final != url:
            if not self.scope.in_scope(final):
                return SKIPPED, f"redirects out of scope to {final}"
            if not self.frontier.mark_seen(final, depth):
                return SKIPPED, f"redirects to already-seen {final}"
            version = self.version_for(final)
            existing = self.db.get_page(self.library_id, version, final)

        kind = _kind(res)
        if kind is None:
            return SKIPPED, f"unsupported content type {res.content_type or '?'}"
        html = res.text
        page = await self._extract(
            extract_html if kind == "html" else extract_markdown, html, final
        )
        if kind == "html" and self._should_render(html, page):  # F3
            rendered = await self._render(final)
            if rendered is not None:
                html = rendered[1]
                static_links = page.links
                page = await self._extract(extract_html, html, final)
                page.links = list(dict.fromkeys(static_links + page.links))
        self._enqueue(page.links, depth + 1)

        if depth == 0 and not self.scope.is_included(final):
            return SKIPPED, "start page outside include patterns (links followed)"
        if len(page.markdown.strip()) < MIN_MARKDOWN_CHARS:
            return SKIPPED, "no main content"

        chash = content_hash(page.markdown)
        if existing is not None and existing["content_hash"] == chash:
            self.db.touch_page(existing["id"], res.etag, res.last_modified, page.links)
            return UNCHANGED, "content unchanged"
        h = simhash(page.markdown)
        dedup = self._dedup(version)
        dup_of = dedup.find(chash, h, exclude_url=final)
        if dup_of is not None:
            return DUPLICATE, f"duplicate of {dup_of}"

        self.db.upsert_page(
            library_id=self.library_id, version=version, url=final, title=page.title,
            markdown=page.markdown, content_hash=chash, simhash=h,
            etag=res.etag, last_modified=res.last_modified, links=page.links,
            anchors=page.anchors,
        )
        dedup.add(final, chash, h)
        return SAVED, page.title
