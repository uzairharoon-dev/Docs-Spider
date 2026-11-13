"""Async HTTP fetcher with retries, size limits and conditional requests."""

from __future__ import annotations

import asyncio
import gzip
import logging
import random
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import httpx

from docurag.crawler.ratelimit import DomainRateLimiter

log = logging.getLogger(__name__)

RETRY_STATUSES = {408, 425, 429, 500, 502, 503, 504}
MAX_BACKOFF = 60.0


@dataclass
class FetchResult:
    url: str
    final_url: str
    status: int
    content: bytes = b""
    content_type: str = ""
    encoding: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    error: str | None = None
    retry_after: float | None = None

    @property
    def not_modified(self) -> bool:
        return self.status == 304

    @property
    def ok(self) -> bool:
        return self.error is None and 200 <= self.status < 300

    @property
    def text(self) -> str:
        data = self.content
        if self.final_url.lower().endswith(".gz") and data[:2] == b"\x1f\x8b":
            data = gzip.decompress(data)
        return data.decode(self.encoding or "utf-8", errors="replace")


def _retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


class Fetcher:
    def __init__(
        self,
        client: httpx.AsyncClient,
        limiter: DomainRateLimiter,
        *,
        user_agent: str,
        max_retries: int = 3,
        max_bytes: int = 5_000_000,
        backoff_base: float = 0.5,
    ):
        self.client = client
        self.limiter = limiter
        self.user_agent = user_agent
        self.max_retries = max_retries
        self.max_bytes = max_bytes
        self.backoff_base = backoff_base

    async def fetch(
        self, url: str, *, etag: str | None = None, last_modified: str | None = None
    ) -> FetchResult:
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,text/markdown,text/plain;q=0.9,*/*;q=0.5",
        }
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        host = urlsplit(url).netloc
        last_error = "unknown error"

        for attempt in range(self.max_retries + 1):
            await self.limiter.wait(host)
            wait: float | None = None
            try:
                result = await self._get(url, headers)
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}".rstrip(": ")
                result = None
            else:
                if result.status not in RETRY_STATUSES:
                    return result
                last_error = f"HTTP {result.status}"
                wait = result.retry_after
            if attempt == self.max_retries:
                break
            backoff = wait if wait is not None else self.backoff_base * (2**attempt)
            backoff = min(MAX_BACKOFF, backoff) + random.uniform(0, self.backoff_base / 2)
            log.debug("retrying %s in %.1fs (%s)", url, backoff, last_error)
            await asyncio.sleep(backoff)

        status = result.status if result is not None else 0
        return FetchResult(url=url, final_url=url, status=status, error=last_error)

    async def _get(self, url: str, headers: dict[str, str]) -> FetchResult:
        async with self.client.stream("GET", url, headers=headers, follow_redirects=True) as resp:
            result = FetchResult(
                url=url,
                final_url=str(resp.url),
                status=resp.status_code,
                content_type=resp.headers.get("content-type", "").split(";")[0].strip().lower(),
                encoding=resp.charset_encoding,
                etag=resp.headers.get("etag"),
                last_modified=resp.headers.get("last-modified"),
            )
            if resp.status_code in RETRY_STATUSES:
                result.retry_after = _retry_after(resp.headers.get("retry-after"))
                return result
            if not 200 <= resp.status_code < 300:
                return result
            declared = resp.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > self.max_bytes:
                result.error = f"too large ({declared} bytes)"
                return result
            chunks: list[bytes] = []
            size = 0
            async for chunk in resp.aiter_bytes():
                size += len(chunk)
                if size > self.max_bytes:
                    result.error = f"too large (> {self.max_bytes} bytes)"
                    return result
                chunks.append(chunk)
            result.content = b"".join(chunks)
            return result
