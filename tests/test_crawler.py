import asyncio

import pytest

from docurag.crawler.crawler import Crawler
from docurag.db import Database

BASE = "http://docs.test"


async def crawl(db, lib, settings, site, **kw):
    async with site.client() as client:
        return await Crawler(db, lib, settings, client=client).run(**kw)


def stored(db) -> set[str]:
    return {r[0] for r in db.conn.execute("SELECT url FROM pages")}


async def test_full_crawl(db, lib, settings, site):
    job_id = await crawl(db, lib, settings, site)
    job = db.get_job(job_id)
    assert job["status"] == "completed"

    assert stored(db) == {
        f"{BASE}/",
        f"{BASE}/guide/",
        f"{BASE}/guide/advanced",
        f"{BASE}/guide/orphan",  # discovered only through sitemap.xml
    }
    paths = site.paths()
    assert "/private/secret" not in paths  # robots.txt
    assert "/zh/guide/" not in paths  # exclude
    assert all(r.url.host == "docs.test" for r in site.requests)  # no off-site
    assert job["stats"]["duplicate"] == 1  # advanced-copy
    assert job["errors"] == []

    md = db.find_pages_by_url(f"{BASE}/guide/advanced")[0]["markdown"]
    assert "```python\nx = 1\n```" in md
    assert "Site header" not in md


async def test_recrawl_uses_conditional_requests(db, lib, settings, site):
    await crawl(db, lib, settings, site)
    site.requests.clear()
    job_id = await crawl(db, lib, settings, site)
    job = db.get_job(job_id)
    assert job["status"] == "completed"
    assert job["stats"].get("saved", 0) == 0
    assert job["stats"]["unchanged"] >= 4
    conditional = [r for r in site.requests if r.headers.get("if-none-match")]
    assert len(conditional) >= 4


async def test_changed_page_is_updated(db, lib, settings, site):
    await crawl(db, lib, settings, site)
    status, ctype, body = site.routes["/guide/orphan"]
    site.routes["/guide/orphan"] = (status, ctype, body.replace("Only in sitemap.", "New text!"))
    job = db.get_job(await crawl(db, lib, settings, site))
    assert job["stats"]["saved"] == 1
    assert "New text!" in db.find_pages_by_url(f"{BASE}/guide/orphan")[0]["markdown"]


async def test_include_filter_still_follows_start_page(db, settings, site, lib):
    lib = lib.model_copy(update={"include": ["/guide/adv*"], "discover_sitemaps": False})
    # The start page is outside `include` but its links must still be followed.
    site.routes["/"] = (200, "text/html", site.routes["/"][2].replace(
        'href="/guide/"', 'href="/guide/advanced"'))
    await crawl(db, lib, settings, site)
    assert stored(db) == {f"{BASE}/guide/advanced"}


async def test_max_pages(db, lib, settings, site):
    lib = lib.model_copy(update={"max_pages": 2})
    job = db.get_job(await crawl(db, lib, settings, site))
    assert job["status"] == "completed"
    assert job["pages_done"] == 2


async def test_resume_after_interruption(lib, settings, site):
    gate = asyncio.Event()
    site.blocked["/guide/advanced"] = gate  # first crawl hangs here
    db = Database(settings.db_path)
    task = asyncio.create_task(crawl(db, lib, settings, site))
    for _ in range(500):
        await asyncio.sleep(0.01)
        if any(r.url.path == "/guide/advanced" for r in site.requests):
            break
    task.cancel()  # what Ctrl-C does inside asyncio.run
    with pytest.raises(asyncio.CancelledError):
        await task
    first = db.list_jobs()[0]
    assert first["status"] == "interrupted"
    assert f"{BASE}/guide/advanced" not in stored(db)

    gate.set()
    site.requests.clear()
    job_id = await crawl(db, lib, settings, site)
    assert job_id == first["id"]  # same job, resumed
    assert db.get_job(job_id)["status"] == "completed"
    assert f"{BASE}/guide/advanced" in stored(db)
    # already-finished URLs are not fetched again
    assert "/guide/" not in site.paths()
    db.close()


async def test_markdown_pages_from_llms_txt(db, settings, site, lib):
    site.routes["/llms.txt"] = (
        200, "text/plain", "# Demo\n\n- [Intro](http://docs.test/intro.md): start here\n",
    )
    site.routes["/intro.md"] = (
        200, "text/markdown", "# Intro\n\n" + "Plain markdown docs page. " * 5,
    )
    lib = lib.model_copy(update={"start_urls": [f"{BASE}/llms.txt"]})
    await crawl(db, lib, settings, site)
    assert stored(db) == {f"{BASE}/intro.md"}
    assert db.find_pages_by_url(f"{BASE}/intro.md")[0]["title"] == "Intro"


async def test_full_crawl_with_extraction_process_pool(db, lib, settings, site):
    settings = settings.model_copy(update={"extract_processes": 2})
    job = db.get_job(await crawl(db, lib, settings, site))
    assert job["status"] == "completed" and job["stats"]["saved"] == 4
    md = db.find_pages_by_url(f"{BASE}/guide/advanced")[0]["markdown"]
    assert "```python\nx = 1\n```" in md
