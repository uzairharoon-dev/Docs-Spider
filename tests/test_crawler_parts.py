import asyncio
import time

import httpx

from docurag.crawler.fetcher import Fetcher
from docurag.crawler.frontier import Frontier
from docurag.crawler.ratelimit import DomainRateLimiter
from docurag.crawler.robots import RobotsCache
from docurag.crawler.seeds import parse_sitemap


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_robots_rules_and_failures():
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "rules.test":
            return httpx.Response(
                200,
                text="User-agent: *\nDisallow: /private/\nCrawl-delay: 2\n"
                "Sitemap: http://rules.test/sm.xml\n",
            )
        if req.url.host == "missing.test":
            return httpx.Response(404)
        return httpx.Response(503)

    async with _client(handler) as client:
        robots = RobotsCache(client, "DocuRAG/0.1")
        assert await robots.allowed("http://rules.test/guide/")
        assert not await robots.allowed("http://rules.test/private/x")
        assert await robots.crawl_delay("http://rules.test/") == 2.0
        assert await robots.sitemaps("http://rules.test/") == ["http://rules.test/sm.xml"]
        assert await robots.allowed("http://missing.test/anything")
        assert not await robots.allowed("http://broken.test/anything")


async def test_rate_limiter_spaces_requests():
    limiter = DomainRateLimiter(rate=20)  # 50 ms apart
    start = time.monotonic()
    await asyncio.gather(*(limiter.wait("a.test") for _ in range(5)))
    assert time.monotonic() - start >= 0.19
    start = time.monotonic()
    await asyncio.gather(*(limiter.wait(f"h{i}.test") for i in range(5)))
    assert time.monotonic() - start < 0.05  # different hosts don't wait on each other


async def test_fetcher_retries_then_succeeds():
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503, headers={"retry-after": "0"})
        return httpx.Response(200, headers={"content-type": "text/html", "etag": '"v1"'}, text="ok")

    async with _client(handler) as client:
        f = Fetcher(client, DomainRateLimiter(1000), user_agent="t", max_retries=3, backoff_base=0)
        res = await f.fetch("http://x.test/")
    assert res.ok and res.text == "ok" and res.etag == '"v1"' and calls["n"] == 3


async def test_fetcher_gives_up_and_limits_size():
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/big":
            return httpx.Response(200, text="x" * 2000)
        return httpx.Response(500)

    async with _client(handler) as client:
        f = Fetcher(client, DomainRateLimiter(1000), user_agent="t", max_retries=1,
                    max_bytes=1000, backoff_base=0)
        failed = await f.fetch("http://x.test/err")
        big = await f.fetch("http://x.test/big")
    assert failed.error == "HTTP 500" and not failed.ok
    assert big.error and "too large" in big.error


def test_parse_sitemap_index_and_urlset():
    pages, children = parse_sitemap(
        '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        "<sitemap><loc>http://x.test/a.xml</loc></sitemap></sitemapindex>"
    )
    assert pages == [] and children == ["http://x.test/a.xml"]
    pages, children = parse_sitemap(
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
        'xmlns:image="http://www.google.com/schemas/sitemap-image/1.1">'
        "<url><loc> http://x.test/p </loc><image:image><image:loc>http://x.test/i.png"
        "</image:loc></image:image></url></urlset>"
    )
    assert pages == [("http://x.test/p", None)] and children == []
    assert parse_sitemap("not xml") == ([], [])


def test_frontier_dedup_claim_and_resume(db):
    lib_id = db.upsert_library("demo", "http://docs.test/", {})
    job = db.create_job(lib_id)
    fr = Frontier(db, job)
    assert fr.add([("http://docs.test/a", 1), ("http://docs.test/", 0)]) == 2
    assert fr.add([("http://docs.test/a", 0)]) == 0
    assert fr.claim() == ("http://docs.test/", 0, None)  # lowest depth first
    assert fr.claim() == ("http://docs.test/a", 1, None)
    assert fr.claim() is None
    # simulate a crash: both URLs in progress -> back to pending
    assert fr.reset_in_progress() == 2
    assert fr.pending() == 2


def test_frontier_round_robins_hosts_within_a_depth(db):
    lib_id = db.upsert_library("demo", "http://a.test/", {})
    fr = Frontier(db, db.create_job(lib_id))
    fr.add([(f"http://a.test/{i}", 1) for i in range(3)] + [("http://b.test/0", 1)])
    fr.add([("http://c.test/0", 1), ("http://a.test/root", 0)])
    order = [fr.claim()[0] for _ in range(6)]
    assert order[0] == "http://a.test/root"  # depth first
    assert order[1:4] == ["http://a.test/0", "http://b.test/0", "http://c.test/0"]
    assert order[4:] == ["http://a.test/1", "http://a.test/2"]
