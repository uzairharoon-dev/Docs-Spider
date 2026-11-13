"""robots.txt handling (F2), following RFC 9309 for fetch failures."""

from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

log = logging.getLogger(__name__)


class RobotsCache:
    """Fetches and caches one robots.txt per origin.

    * 2xx: rules apply.
    * 4xx: no rules, everything allowed.
    * 5xx or network error: treated as "disallow all" (RFC 9309 §2.3.1.4).
    """

    def __init__(self, client: httpx.AsyncClient, user_agent: str, timeout: float = 10.0):
        self.client = client
        self.user_agent = user_agent
        self.timeout = timeout
        self._parsers: dict[str, RobotFileParser] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    @staticmethod
    def _origin(url: str) -> str:
        p = urlsplit(url)
        return f"{p.scheme}://{p.netloc}"

    async def _parser(self, url: str) -> RobotFileParser:
        origin = self._origin(url)
        if origin in self._parsers:
            return self._parsers[origin]
        lock = self._locks.setdefault(origin, asyncio.Lock())
        async with lock:
            if origin not in self._parsers:
                self._parsers[origin] = await self._fetch(origin)
        return self._parsers[origin]

    async def _fetch(self, origin: str) -> RobotFileParser:
        parser = RobotFileParser(origin + "/robots.txt")
        try:
            resp = await self.client.get(
                origin + "/robots.txt",
                headers={"User-Agent": self.user_agent},
                timeout=self.timeout,
                follow_redirects=True,
            )
        except httpx.HTTPError as exc:
            log.warning("robots.txt unreachable for %s (%s); disallowing", origin, exc)
            parser.disallow_all = True
            return parser
        if resp.status_code >= 500:
            log.warning("robots.txt for %s returned %s; disallowing", origin, resp.status_code)
            parser.disallow_all = True
        elif resp.status_code >= 400:
            parser.allow_all = True
        else:
            parser.parse(resp.text.splitlines())
        parser.modified()
        return parser

    async def allowed(self, url: str) -> bool:
        return (await self._parser(url)).can_fetch(self.user_agent, url)

    async def crawl_delay(self, url: str) -> float | None:
        delay = (await self._parser(url)).crawl_delay(self.user_agent)
        return float(delay) if delay is not None else None

    async def sitemaps(self, url: str) -> list[str]:
        return list((await self._parser(url)).site_maps() or [])
