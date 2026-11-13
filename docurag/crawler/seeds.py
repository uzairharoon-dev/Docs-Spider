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


def parse_sitemap(xml_text: str) -> tuple[list[str], list[str]]:
    """Return ``(page_urls, child_sitemap_urls)``; empty lists for invalid XML."""
    try:
        root = ET.fromstring(xml_text.lstrip("﻿ \r\n\t").encode("utf-8"))
    except ET.ParseError:
        return [], []
    locs = []
    for entry in root:  # <url> or <sitemap>; skips nested image:loc etc.
        for child in entry:
            if _local(child.tag) == "loc" and child.text and child.text.strip():
                locs.append(child.text.strip())
                break
    if _local(root.tag) == "sitemapindex":
        return [], locs
    return locs, []


async def _fetch_text(url: str, fetcher: Fetcher, robots: RobotsCache) -> str | None:
    if not await robots.allowed(url):
        log.info("robots.txt disallows seed %s", url)
        return None
    res = await fetcher.fetch(url)
    if not res.ok:
        log.info("seed %s not usable: %s", url, res.error or f"HTTP {res.status}")
        return None
    return res.text


async def sitemap_urls(sitemap: str, fetcher: Fetcher, robots: RobotsCache) -> list[str]:
    pages: list[str] = []
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
) -> list[tuple[str, int]]:
    """Return ``(url, depth)`` seeds. Start pages get depth 0, which bypasses ``include``."""
    seeds: dict[str, int] = {}

    def keep(raw: str, depth: int) -> None:
        url = normalize_url(raw)
        if url and url not in seeds and scope.in_scope(url) and scope.is_included(url):
            seeds[url] = depth

    for raw in start_urls:
        start = normalize_url(raw)
        if start is None:
            log.warning("ignoring invalid start URL %r", raw)
            continue
        if is_sitemap_url(start):
            for u in await sitemap_urls(start, fetcher, robots):
                keep(u, 1)
        elif is_llms_txt_url(start):
            text = await _fetch_text(start, fetcher, robots)
            for u in markdown_links(text or "", start):
                keep(u, 1)
        else:
            seeds.setdefault(start, 0)
            if discover_sitemaps:
                origin = "{0.scheme}://{0.netloc}".format(urlsplit(start))
                candidates = list(dict.fromkeys([*await robots.sitemaps(start),
                                                 origin + "/sitemap.xml"]))
                for sm in candidates:
                    sm_url = normalize_url(sm)
                    if sm_url and urlsplit(sm_url).netloc in scope.netlocs:
                        for u in await sitemap_urls(sm_url, fetcher, robots):
                            keep(u, 1)
    return list(seeds.items())
