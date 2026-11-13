"""Seed expansion (F1): plain URLs, sitemap.xml (incl. indexes and .gz) and llms.txt."""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit

from docurag.crawler.fetcher import Fetcher
from docurag.crawler.robots import RobotsCache
from docurag.extract import markdown_links
from docurag.urls import Scope, is_llms_txt_url, is_sitemap_url, normalize_url

log = logging.getLogger(__name__)

MAX_SITEMAP_DEPTH = 3
MAX_SITEMAP_URLS = 50_000


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def parse_sitemap(xml_text: str) -> tuple[list[tuple[str, str | None]], list[str]]:
    """Return ``([(page_url, lastmod), ...], child_sitemap_urls)``; empty for invalid XML."""
    try:
        root = ET.fromstring(xml_text.lstrip("\ufeff \r\n\t").encode("utf-8"))
    except ET.ParseError:
        return [], []
    entries: list[tuple[str, str | None]] = []
    for entry in root:  # <url> or <sitemap>; direct children only, so image:loc is skipped
        loc = lastmod = None
        for child in entry:
            tag = _local(child.tag)
            text = (child.text or "").strip()
            if tag == "loc" and text and loc is None:
                loc = text
            elif tag == "lastmod" and text:
                lastmod = text
        if loc:
            entries.append((loc, lastmod))
    if _local(root.tag) == "sitemapindex":
        return [], [loc for loc, _ in entries]
    return entries, []


async def _fetch_text(url: str, fetcher: Fetcher, robots: RobotsCache) -> str | None:
    if not await robots.allowed(url):
        log.info("robots.txt disallows seed %s", url)
        return None
    res = await fetcher.fetch(url)
    if not res.ok:
        log.info("seed %s not usable: %s", url, res.error or f"HTTP {res.status}")
        return None
    return res.text


async def sitemap_urls(
    sitemap: str, fetcher: Fetcher, robots: RobotsCache
) -> list[tuple[str, str | None]]:
    pages: list[tuple[str, str | None]] = []
    queue, seen = [(sitemap, 0)], {sitemap}
    while queue and len(pages) < MAX_SITEMAP_URLS:
        url, depth = queue.pop(0)
        text = await _fetch_text(url, fetcher, robots)
        if text is None:
            continue
        found, children = parse_sitemap(text)
        pages.extend(found)
        if depth < MAX_SITEMAP_DEPTH:
            for child in children:
                if child not in seen:
                    seen.add(child)
                    queue.append((child, depth + 1))
    return pages[:MAX_SITEMAP_URLS]


async def expand_seeds(
    start_urls: list[str],
    scope: Scope,
    fetcher: Fetcher,
    robots: RobotsCache,
    *,
    discover_sitemaps: bool = True,
) -> list[tuple[str, int, str | None]]:
    """Return ``(url, depth, lastmod)`` seeds. Start pages get depth 0, bypassing ``include``.

    ``lastmod`` comes from the sitemap; re-crawls use it to skip unchanged pages.
    """
    seeds: dict[str, tuple[int, str | None]] = {}

    def keep(raw: str, depth: int, lastmod: str | None = None) -> None:
        url = normalize_url(raw)
        if url is None:
            return
        if url in seeds:
            if seeds[url][1] is None and lastmod:  # e.g. a start page that is also listed
                seeds[url] = (seeds[url][0], lastmod)
        elif scope.in_scope(url) and scope.is_included(url):
            seeds[url] = (depth, lastmod)

    for raw in start_urls:
        start = normalize_url(raw)
        if start is None:
            log.warning("ignoring invalid start URL %r", raw)
            continue
        if is_sitemap_url(start):
            for u, lastmod in await sitemap_urls(start, fetcher, robots):
                keep(u, 1, lastmod)
        elif is_llms_txt_url(start):
            text = await _fetch_text(start, fetcher, robots)
            for u in markdown_links(text or "", start):
                keep(u, 1)
        else:
            seeds.setdefault(start, (0, None))
            if discover_sitemaps:
                origin = "{0.scheme}://{0.netloc}".format(urlsplit(start))
                candidates = list(dict.fromkeys([*await robots.sitemaps(start),
                                                 origin + "/sitemap.xml"]))
                for sm in candidates:
                    sm_url = normalize_url(sm)
                    if sm_url and urlsplit(sm_url).netloc in scope.netlocs:
                        for u, lastmod in await sitemap_urls(sm_url, fetcher, robots):
                            keep(u, 1, lastmod)
    return [(url, depth, lastmod) for url, (depth, lastmod) in seeds.items()]
