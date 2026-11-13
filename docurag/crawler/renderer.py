"""Headless-browser rendering for JavaScript-built docs pages (F3).

Optional: needs ``pip install 'docurag[js]'`` and ``playwright install chromium``.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Protocol

log = logging.getLogger(__name__)

# Below this much extracted text, an HTML page that ships scripts is probably a JS shell.
JS_SHELL_MAX_CHARS = 200
_SCRIPT = re.compile(r"<script\b", re.IGNORECASE)
_APP_ROOT = re.compile(
    r"""<div[^>]+id=["'](?:root|app|__next|___gatsby|__nuxt|docusaurus)["'][^>]*>\s*</div>""",
    re.IGNORECASE,
)


class RendererUnavailable(RuntimeError):
    pass


class Renderer(Protocol):
    async def render(self, url: str) -> tuple[str, str]:
        """Returns (final_url, rendered_html)."""
        ...

    async def aclose(self) -> None: ...


def looks_like_js_shell(html: str, markdown: str) -> bool:
    """True when the static HTML has scripts but almost no readable content."""
    if _APP_ROOT.search(html):
        return True
    return len(markdown.strip()) < JS_SHELL_MAX_CHARS and bool(_SCRIPT.search(html))


class PlaywrightRenderer:
    def __init__(self, user_agent: str, *, timeout_ms: int = 30_000, concurrency: int = 2):
        self.user_agent = user_agent
        self.timeout_ms = timeout_ms
        self._sem = asyncio.Semaphore(concurrency)
        self._start_lock = asyncio.Lock()
        self._pw = None
        self._browser = None
        self._context = None

    async def _ensure(self) -> None:
        async with self._start_lock:
            if self._context is not None:
                return
            try:
                from playwright.async_api import async_playwright
            except ImportError as exc:
                raise RendererUnavailable(
                    "JS rendering needs Playwright: uv sync --extra js && "
                    "uv run playwright install chromium"
                ) from exc
            self._pw = await async_playwright().start()
            try:
                self._browser = await self._pw.chromium.launch()
            except Exception as exc:
                await self._pw.stop()
                self._pw = None
                raise RendererUnavailable(
                    f"could not start Chromium ({exc}); run: uv run playwright install chromium"
                ) from exc
            self._context = await self._browser.new_context(user_agent=self.user_agent)

    async def render(self, url: str) -> tuple[str, str]:
        await self._ensure()
        async with self._sem:
            page = await self._context.new_page()
            try:
                await page.goto(url, wait_until="networkidle", timeout=self.timeout_ms)
                return page.url, await page.content()
            finally:
                await page.close()

    async def aclose(self) -> None:
        if self._browser is not None:
            await self._browser.close()
        if self._pw is not None:
            await self._pw.stop()
        self._pw = self._browser = self._context = None
