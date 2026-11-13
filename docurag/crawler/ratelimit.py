"""Per-domain request rate limiting (F2)."""

from __future__ import annotations

import asyncio
import time


class DomainRateLimiter:
    """Spaces requests to the same host at least ``1 / rate`` seconds apart.

    Each caller reserves the next free slot under a short lock and then sleeps
    outside it, so many workers can queue on one host without serializing the
    whole crawl.
    """

    def __init__(self, rate: float):
        if rate <= 0:
            raise ValueError("rate must be > 0")
        self.default_interval = 1.0 / rate
        self._interval: dict[str, float] = {}
        self._next_slot: dict[str, float] = {}
        self._lock = asyncio.Lock()

    def set_min_interval(self, host: str, seconds: float) -> None:
        """Slow a host down further, e.g. to honor robots.txt Crawl-delay."""
        current = self._interval.get(host, self.default_interval)
        self._interval[host] = max(current, seconds)

    async def wait(self, host: str) -> None:
        async with self._lock:
            now = time.monotonic()
            slot = max(now, self._next_slot.get(host, 0.0))
            self._next_slot[host] = slot + self._interval.get(host, self.default_interval)
        delay = slot - now
        if delay > 0:
            await asyncio.sleep(delay)
