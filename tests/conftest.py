from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import httpx
import pytest

from docurag.config import LibraryConfig, Settings
from docurag.db import Database

LOREM = (
    "FastAPI dependency injection lets path operations declare what they need. "
    "The framework resolves each dependency, caches it per request and passes the "
    "result in. Sub-dependencies form a graph that is resolved once. "
)


def page(title: str, body: str, links: list[str] = ()) -> str:
    nav = "".join(f'<a href="{href}">{href}</a>' for href in links)
    return f"""<!doctype html><html><head><title>{title} - Docs</title></head><body>
<header class="site">Site header</header>
<nav class="menu">{nav}</nav>
<main><article><h1>{title}<a class="headerlink" href="#t">¶</a></h1>{body}</article></main>
<footer>Copyright</footer></body></html>"""


@dataclass
class FakeSite:
    """A tiny in-memory docs site. ``routes`` maps path -> (status, content-type, body)."""

    routes: dict[str, tuple[int, str, str]] = field(default_factory=dict)
    requests: list[httpx.Request] = field(default_factory=list)
    etags: bool = True
    # path -> event; requests for that path wait until the event is set
    blocked: dict[str, asyncio.Event] = field(default_factory=dict)

    def paths(self) -> list[str]:
        return [r.url.path for r in self.requests]

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if (gate := self.blocked.get(request.url.path)) is not None:
            await gate.wait()
        if request.url.host != "docs.test":
            return httpx.Response(404)
        route = self.routes.get(request.url.path)
        if route is None:
            return httpx.Response(404, text="not found")
        status, ctype, body = route
        headers = {"content-type": ctype}
        if self.etags and status == 200:
            etag = f'"{hash(body) & 0xFFFFFFFF:x}"'
            headers["etag"] = etag
            if request.headers.get("if-none-match") == etag:
                return httpx.Response(304, headers=headers)
        if status in (301, 302):
            return httpx.Response(status, headers={"location": body})
        return httpx.Response(status, headers=headers, text=body)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


def default_site() -> FakeSite:
    html = "text/html; charset=utf-8"
    advanced_body = (
        f"<h2>Advanced</h2><p>{LOREM * 3}</p><pre><code class='language-py'>x = 1</code></pre>"
    )
    return FakeSite(
        routes={
            "/robots.txt": (200, "text/plain", "User-agent: *\nDisallow: /private/\n"),
            "/sitemap.xml": (
                200,
                "application/xml",
                """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>http://docs.test/guide/orphan</loc></url>
  <url><loc>http://docs.test/zh/guide/</loc></url>
</urlset>""",
            ),
            "/": (
                200,
                html,
                page(
                    "Home",
                    f"<p>{LOREM}</p>",
                    [
                        "/guide/", "/private/secret", "/zh/guide/", "https://other.test/x",
                        "/guide/#section", "/logo.png", "mailto:a@b.c",
                    ],
                ),
            ),
            "/guide/": (
                200,
                html,
                page("Guide", f"<p>{LOREM * 2}</p>", ["advanced", "advanced-copy", "old"]),
            ),
            "/guide/advanced": (200, html, page("Advanced", advanced_body)),
            # Same content as /guide/advanced: must be dropped as a duplicate.
            "/guide/advanced-copy": (200, html, page("Advanced", advanced_body)),
            "/guide/old": (301, html, "/guide/advanced"),
            "/guide/orphan": (200, html, page("Orphan", f"<p>Only in sitemap. {LOREM}</p>")),
            "/private/secret": (200, html, page("Secret", f"<p>{LOREM}</p>")),
            "/zh/guide/": (200, html, page("中文", f"<p>{LOREM}</p>")),
        }
    )


@pytest.fixture
def site() -> FakeSite:
    return default_site()


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(data_dir=tmp_path, concurrency=4, max_retries=1, extract_processes=0)


@pytest.fixture
def db(settings) -> Database:
    d = Database(settings.db_path)
    yield d
    d.close()


@pytest.fixture
def lib() -> LibraryConfig:
    return LibraryConfig(
        name="demo", start_urls=["http://docs.test/"], exclude=["/zh/"], rate_limit=1000
    )
