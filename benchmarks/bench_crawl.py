"""Crawl throughput benchmark on a local synthetic multi-domain docs site.

Spec target: >= 20 pages/s across domains. Politeness caps one domain at
``rate_limit`` requests/s (default 2), so the target is about crawling many
domains at once. This script serves N "domains" (one port each) from a separate
process and crawls them all as one library, twice:

* polite: the default 2 req/s per domain;
* unthrottled: no rate limit, which measures the crawler's own ceiling (fetch +
  HTML-to-Markdown + dedup + SQLite writes).

Nothing on the internet is touched.

    uv run python benchmarks/bench_crawl.py --domains 12 --pages 60
"""

from __future__ import annotations

import argparse
import asyncio
import json
import multiprocessing as mp
import random
import socket
import tempfile
import time
from pathlib import Path

WORDS = (
    "request response dependency parameter router middleware schema validation token "
    "session database model field query path header cookie client server async await "
    "handler config setting option version deploy cache stream upload download"
).split()


def page_html(host: int, i: int, pages: int, rng: random.Random) -> str:
    def para(n: int) -> str:
        return " ".join(rng.choice(WORDS) for _ in range(n)).capitalize() + "."

    nav = "".join(f'<li><a href="/p{j}/">Page {j}</a></li>' for j in range(min(pages, 40)))
    links = "".join(
        f'<a href="/p{(i * 7 + k) % pages}/">see {k}</a> ' for k in range(1, 6)
    )
    body = "".join(
        f"<h2 id='s{s}'>Section {s}</h2><p>{para(80)}</p><p>{para(60)} {links}</p>"
        f"<div class='highlight-python'><pre>def f_{s}(x):\n    return x * {s}\n</pre></div>"
        f"<table><tr><th>name</th><th>type</th></tr><tr><td>p{s}</td><td>int</td></tr></table>"
        for s in range(6)
    )
    return (
        f"<!doctype html><html><head><title>Host {host} page {i}</title></head><body>"
        f"<header>Site</header><nav><ul>{nav}</ul></nav>"
        f"<main><article><h1>Host {host} page {i}</h1>{body}</article></main>"
        "<footer>footer</footer></body></html>"
    )


def serve(ports: list[int], pages: int, ready) -> None:
    import uvicorn
    from starlette.applications import Starlette
    from starlette.responses import HTMLResponse, PlainTextResponse
    from starlette.routing import Route

    def make_app(host: int) -> Starlette:
        cache = {
            i: page_html(host, i, pages, random.Random(host * 10_000 + i)) for i in range(pages)
        }

        async def index(request):
            return HTMLResponse(cache[0])

        async def one(request):
            i = int(request.path_params["i"])
            return HTMLResponse(cache[i]) if i in cache else PlainTextResponse("nf", 404)

        async def robots(request):
            return PlainTextResponse("User-agent: *\n")

        return Starlette(routes=[
            Route("/", index), Route("/p{i:int}/", one), Route("/robots.txt", robots),
        ])

    async def main():
        servers = [
            uvicorn.Server(uvicorn.Config(make_app(n), host="127.0.0.1", port=p,
                                          log_level="error", access_log=False))
            for n, p in enumerate(ports)
        ]
        tasks = [asyncio.create_task(s.serve()) for s in servers]
        while not all(s.started for s in servers):
            await asyncio.sleep(0.05)
        ready.set()
        await asyncio.gather(*tasks)

    asyncio.run(main())


def free_ports(n: int) -> list[int]:
    socks, ports = [], []
    for _ in range(n):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        socks.append(s)
        ports.append(s.getsockname()[1])
    for s in socks:
        s.close()
    return ports


async def crawl_once(ports: list[int], rate: float, concurrency: int) -> dict:
    from docurag.config import LibraryConfig, Settings
    from docurag.crawler.crawler import Crawler
    from docurag.db import Database

    with tempfile.TemporaryDirectory() as tmp:
        settings = Settings(data_dir=Path(tmp), concurrency=concurrency)
        lib = LibraryConfig(
            name="bench", start_urls=[f"http://127.0.0.1:{p}/" for p in ports],
            rate_limit=rate, discover_sitemaps=False,
        )
        db = Database(settings.db_path)
        started = time.perf_counter()
        job_id = await Crawler(db, lib, settings).run()
        elapsed = time.perf_counter() - started
        job = db.get_job(job_id)
        db.close()
    saved = job["stats"].get("saved", 0)
    return {
        "rate_limit_per_domain": rate,
        "pages_fetched": job["pages_done"],
        "pages_saved": saved,
        "seconds": round(elapsed, 2),
        "pages_per_second": round(job["pages_done"] / elapsed, 1),
        "status": job["status"],
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--domains", type=int, default=12)
    ap.add_argument("--pages", type=int, default=60, help="pages per domain")
    ap.add_argument("--concurrency", type=int, default=32)
    ap.add_argument("--json", type=Path, help="write results here")
    args = ap.parse_args()

    ports = free_ports(args.domains)
    ready = mp.Event()
    server = mp.Process(target=serve, args=(ports, args.pages, ready), daemon=True)
    server.start()
    if not ready.wait(60):
        raise SystemExit("synthetic site did not start")
    try:
        results = {
            "domains": args.domains,
            "pages_per_domain": args.pages,
            "concurrency": args.concurrency,
            "polite": asyncio.run(crawl_once(ports, 2.0, args.concurrency)),
            "unthrottled": asyncio.run(crawl_once(ports, 10_000.0, args.concurrency)),
        }
    finally:
        server.terminate()
    print(json.dumps(results, indent=2))
    if args.json:
        args.json.write_text(json.dumps(results, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
