import pytest

from docurag.config import LibraryConfig
from docurag.crawler.crawler import Crawler, parse_time
from docurag.crawler.renderer import RendererUnavailable, looks_like_js_shell
from docurag.crawler.versions import detect_version, pick_latest, version_key
from docurag.index.embedder import HashEmbedder
from docurag.index.search import Searcher
from tests.conftest import LOREM, FakeSite, page
from tests.test_index import make_indexer

HTML = "text/html; charset=utf-8"
BASE = "http://docs.test"


async def crawl(db, lib, settings, site, renderer=None):
    async with site.client() as client:
        c = Crawler(db, lib, settings, client=client, renderer=renderer)
        job_id = await c.run()
    return db.get_job(job_id)


def stored(db):
    return {(r[0], r[1]) for r in db.conn.execute("SELECT url, version FROM pages WHERE gone=0")}


# ---------------------------------------------------------------- F4 versions
@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://fastapi.tiangolo.com/0.115/tutorial/", "0.115"),
        ("https://x.test/docs/v2/guide/", "v2"),
        ("https://x.readthedocs.io/en/stable/api.html", "stable"),
        ("https://docs.python.org/3.12/library/os.html", "3.12"),
        ("https://x.test/2.0.0rc1/", "2.0.0rc1"),
        ("https://x.test/guide/intro/", None),
        ("https://x.test/v2", None),  # last segment is the page, not a directory
        ("https://x.test/a/b/c/v2/", None),  # too deep: e.g. /api/.../v1/ reference pages
    ],
)
def test_detect_version(url, expected):
    assert detect_version(url) == expected


def test_detect_version_custom_pattern():
    assert detect_version("https://x.test/docs/next/a/", r"^/docs/(?P<version>[^/]+)/") == "next"
    assert detect_version("https://x.test/other/", r"^/docs/([^/]+)/") is None
    with pytest.raises(ValueError):
        LibraryConfig(name="x", start_urls=["https://x.test/"], version_pattern="^/docs/")


def test_version_ordering_and_latest():
    assert version_key("0.115") > version_key("0.99")
    assert version_key("2.0") > version_key("2.0rc1")
    assert version_key("v10") > version_key("v9")
    assert pick_latest(["0.99", "0.115", "0.116rc1"]) == "0.115"  # releases beat pre-releases
    assert pick_latest(["0.116rc1", "0.116rc2"]) == "0.116rc2"
    assert pick_latest(["v1", "latest", "v2"]) == "v2"  # unversioned pages don't win
    assert pick_latest(["latest", "stable"]) == "latest"
    assert pick_latest(["stable", "dev"]) == "stable"
    assert pick_latest([]) is None


def versioned_site() -> FakeSite:
    body = f"<p>{LOREM * 3}</p>"
    return FakeSite(routes={
        "/robots.txt": (200, "text/plain", "User-agent: *\n"),
        "/": (200, HTML, page("Home", f"<p>{LOREM}</p>", ["/v1/guide/", "/v2/guide/"])),
        # Same text in both versions: must not be dropped as duplicates of each other.
        "/v1/guide/": (200, HTML, page("Guide v1", body + "<p>Use old_api() here.</p>")),
        "/v2/guide/": (200, HTML, page("Guide v2", body + "<p>Use new_api() here.</p>")),
    })


async def test_crawl_detects_versions_and_search_latest(db, settings):
    lib = LibraryConfig(name="demo", start_urls=[f"{BASE}/"], rate_limit=1000,
                        discover_sitemaps=False)
    job = await crawl(db, lib, settings, versioned_site())
    assert job["status"] == "completed"
    assert stored(db) == {(f"{BASE}/", "latest"), (f"{BASE}/v1/guide/", "v1"),
                          (f"{BASE}/v2/guide/", "v2")}
    assert job["stats"].get("duplicate", 0) == 0

    indexer, store = make_indexer(db, embedder=HashEmbedder(128))
    indexer.index_library(db.get_library("demo")["id"])
    s = Searcher(db, indexer.embedder, store)
    hits = s.search("guide api", "demo", version="latest", k=5, mode="bm25")
    assert {h.version for h in hits} == {"v2"}  # newest numbered version wins
    assert {h.version for h in s.search("guide api", "demo", version="v1", mode="bm25")} \
        == {"v1"}
    assert "v1" in db.list_libraries()[0]["versions"]


async def test_explicit_version_overrides_detection(db, settings):
    lib = LibraryConfig(name="demo", start_urls=[f"{BASE}/"], rate_limit=1000,
                        discover_sitemaps=False, version="3.0")
    await crawl(db, lib, settings, versioned_site())
    assert {v for _, v in stored(db)} == {"3.0"}


# ---------------------------------------------------------------- F5 incremental
def sitemap_site(lastmod: str) -> FakeSite:
    site = FakeSite(routes={
        "/robots.txt": (200, "text/plain", "User-agent: *\n"),
        "/sitemap.xml": (200, "application/xml",
                         '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                         f"<url><loc>{BASE}/a/</loc><lastmod>{lastmod}</lastmod></url>"
                         "</urlset>"),
        "/": (200, HTML, page("Home", f"<p>{LOREM}</p>")),
        "/a/": (200, HTML, page("A", f"<p>Page A. {LOREM}</p>", ["/b/"])),
        "/b/": (200, HTML, page("B", f"<p>Page B. {LOREM}</p>")),
    })
    site.etags = False  # a server without ETags: only lastmod can save the request
    return site


async def test_sitemap_lastmod_skips_unchanged_pages(db, settings):
    lib = LibraryConfig(name="demo", start_urls=[f"{BASE}/"], rate_limit=1000)
    site = sitemap_site("2020-01-01")
    await crawl(db, lib, settings, site)
    assert (f"{BASE}/a/", "latest") in stored(db) and (f"{BASE}/b/", "latest") in stored(db)

    site.requests.clear()
    job = await crawl(db, lib, settings, site)
    assert "/a/" not in site.paths()  # lastmod (2020) is older than our fetch: no request
    assert "/b/" in site.paths()  # still reached through /a/'s stored links
    assert job["stats"]["unchanged"] >= 2

    newer = sitemap_site("2999-01-01T00:00:00Z")
    newer.requests.clear()
    await crawl(db, lib, settings, newer)
    assert "/a/" in newer.paths()  # lastmod after our fetch: fetch again


async def test_untrusted_lastmod_refetches(db, settings):
    lib = LibraryConfig(name="demo", start_urls=[f"{BASE}/"], rate_limit=1000)
    site = sitemap_site("2020-01-01")
    await crawl(db, lib, settings, site)
    site.requests.clear()
    await crawl(db, lib.model_copy(update={"trust_sitemap_lastmod": False}), settings, site)
    assert "/a/" in site.paths()


def test_parse_time():
    assert parse_time("2024-05-01").isoformat() == "2024-05-01T00:00:00+00:00"
    assert parse_time("2024-05-01T10:00:00Z").hour == 10
    assert parse_time("garbage") is None and parse_time(None) is None


async def test_removed_page_is_hidden_then_pruned_then_revived(db, settings):
    lib = LibraryConfig(name="demo", start_urls=[f"{BASE}/"], rate_limit=1000,
                        discover_sitemaps=False)
    site = FakeSite(routes={
        "/robots.txt": (200, "text/plain", "User-agent: *\n"),
        "/": (200, HTML, page("Home", f"<p>{LOREM}</p>", ["/old/"])),
        "/old/": (200, HTML, page("Old", f"<p>Deprecated zebra API. {LOREM}</p>")),
    })
    await crawl(db, lib, settings, site)
    lib_id = db.get_library("demo")["id"]
    indexer, store = make_indexer(db, embedder=HashEmbedder(128))
    indexer.index_library(lib_id)
    searcher = Searcher(db, indexer.embedder, store)
    assert searcher.search("zebra", "demo", mode="bm25")

    removed = dict(site.routes)
    removed.pop("/old/")  # now 404, and no longer linked from /
    removed["/"] = (200, HTML, page("Home", f"<p>{LOREM}</p>"))
    site.routes = removed
    job = await crawl(db, lib, settings, site)
    assert job["stats"]["removed"] == 1
    # hidden right away, before any re-index
    assert searcher.search("zebra", "demo", mode="bm25") == []
    vector_hits = searcher.search("zebra", "demo", mode="vector", k=10)
    assert all(h.url != f"{BASE}/old/" for h in vector_hits)
    assert db.find_pages_by_url(f"{BASE}/old/") == []
    assert db.list_libraries()[0]["pages"] == 1

    stats = indexer.index_library(lib_id)
    assert stats.pages_removed == 1 and stats.chunks_deleted >= 1
    assert db.conn.execute("SELECT COUNT(*) FROM pages WHERE url=?",
                           (f"{BASE}/old/",)).fetchone()[0] == 0
    n_chunks = db.conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    assert n_chunks == db.conn.execute("SELECT COUNT(*) FROM chunk_vectors").fetchone()[0]


async def test_gone_page_comes_back(db, settings):
    lib = LibraryConfig(name="demo", start_urls=[f"{BASE}/"], rate_limit=1000,
                        discover_sitemaps=False)
    routes = {
        "/robots.txt": (200, "text/plain", "User-agent: *\n"),
        "/": (200, HTML, page("Home", f"<p>{LOREM}</p>", ["/p/"])),
        "/p/": (200, HTML, page("P", f"<p>Page P. {LOREM}</p>")),
    }
    site = FakeSite(routes=dict(routes))
    await crawl(db, lib, settings, site)
    site.routes = {k: v for k, v in routes.items() if k != "/p/"}
    await crawl(db, lib, settings, site)
    assert (f"{BASE}/p/", "latest") not in stored(db)
    site.routes = dict(routes)
    await crawl(db, lib, settings, site)  # known URLs are re-checked even if gone
    assert (f"{BASE}/p/", "latest") in stored(db)


# ---------------------------------------------------------------- F3 rendering
SHELL = ('<!doctype html><html><head><title>App</title><script src="/app.js"></script>'
         '</head><body><div id="root"></div></body></html>')


class FakeRenderer:
    def __init__(self, html=None, fail: Exception | None = None):
        self.html = html
        self.fail = fail
        self.calls = []

    async def render(self, url):
        self.calls.append(url)
        if self.fail:
            raise self.fail
        return url, self.html

    async def aclose(self):
        pass


def shell_site() -> FakeSite:
    return FakeSite(routes={
        "/robots.txt": (200, "text/plain", "User-agent: *\n"),
        "/": (200, HTML, SHELL),
    })


def test_js_shell_heuristic():
    assert looks_like_js_shell(SHELL, "")
    assert not looks_like_js_shell(page("Real", f"<p>{LOREM * 3}</p>"), LOREM * 3)
    assert looks_like_js_shell("<html><script></script><p>Loading…</p></html>", "Loading…")


async def test_render_js_auto_renders_shell_pages(db, settings):
    rendered = page("Rendered", f"<p>Client-side docs. {LOREM}</p>", ["/next/"])
    renderer = FakeRenderer(rendered)
    lib = LibraryConfig(name="demo", start_urls=[f"{BASE}/"], rate_limit=1000,
                        discover_sitemaps=False, render_js="auto")
    await crawl(db, lib, settings, shell_site(), renderer)
    assert renderer.calls == [f"{BASE}/"]
    md = db.find_pages_by_url(f"{BASE}/")[0]["markdown"]
    assert "Client-side docs" in md


async def test_render_js_auto_leaves_static_pages_alone(db, settings, site):
    renderer = FakeRenderer("<html></html>")
    lib = LibraryConfig(name="demo", start_urls=[f"{BASE}/"], rate_limit=1000,
                        exclude=["/zh/"], render_js="auto")
    await crawl(db, lib, settings, site, renderer)
    assert renderer.calls == []


async def test_render_js_off_keeps_shell_unrendered(db, settings):
    renderer = FakeRenderer("<html></html>")
    lib = LibraryConfig(name="demo", start_urls=[f"{BASE}/"], rate_limit=1000,
                        discover_sitemaps=False)
    job = await crawl(db, lib, settings, shell_site(), renderer)
    assert renderer.calls == [] and job["stats"]["skipped"] == 1


async def test_missing_browser_falls_back_to_static_html(db, settings, caplog):
    renderer = FakeRenderer(fail=RendererUnavailable("JS rendering needs Playwright"))
    lib = LibraryConfig(name="demo", start_urls=[f"{BASE}/"], rate_limit=1000,
                        discover_sitemaps=False, render_js=True)
    job = await crawl(db, lib, settings, shell_site(), renderer)
    assert job["status"] == "completed"
    assert "needs Playwright" in caplog.text


async def test_real_playwright_renders_page(tmp_path):
    """Runs only where Playwright and Chromium are installed (`uv sync --extra js`)."""
    pytest.importorskip("playwright")
    from docurag.crawler.renderer import PlaywrightRenderer

    html_file = tmp_path / "app.html"
    html_file.write_text(
        "<html><body><div id='root'></div><script>"
        "document.getElementById('root').innerHTML = '<h1>Built by JS</h1>';"
        "</script></body></html>"
    )
    r = PlaywrightRenderer("DocuRAG-test")
    try:
        _, html = await r.render(html_file.as_uri())
    except RendererUnavailable as exc:
        pytest.skip(str(exc))
    finally:
        await r.aclose()
    assert "<h1>Built by JS</h1>" in html
