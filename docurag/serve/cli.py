"""``docurag`` command-line interface."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.markdown import Markdown
from rich.table import Table

from docurag import __version__
from docurag.config import LibraryConfig, Settings, load_settings
from docurag.crawler.crawler import FAILED, Crawler, CrawlEvent
from docurag.db import Database
from docurag.index.pipeline import Indexer, IndexStats
from docurag.index.search import Searcher, UnknownLibrary
from docurag.urls import normalize_url

app = typer.Typer(
    no_args_is_help=True,
    help="DocuRAG: crawl docs sites into a versioned knowledge base for AI coding assistants.",
)
console = Console()
err = Console(stderr=True)


class State:
    config_path: Path | None = None


state = State()


@app.callback()
def main(
    config: Annotated[
        Path | None, typer.Option("--config", "-c", help="Path to docurag.yaml.")
    ] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Debug logging.")] = False,
) -> None:
    state.config_path = config
    # Docs contain emoji and CJK; don't crash on Windows code pages.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(message)s",
        handlers=[RichHandler(console=err, show_path=False)],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _settings() -> Settings:
    try:
        return load_settings(state.config_path)
    except Exception as exc:
        err.print(f"[red]Invalid config:[/red] {exc}")
        raise typer.Exit(2) from exc


def _db(settings: Settings) -> Database:
    return Database(settings.db_path)


def _index_parts(settings: Settings, db: Database):
    """Embedder + vector store, imported lazily so crawl-only commands stay fast."""
    from docurag.index.embedder import make_embedder
    from docurag.index.store import make_store

    try:
        with console.status(f"Loading embedder {settings.embedder.model}..."):
            embedder = make_embedder(settings.embedder, settings.data_dir)
        store = make_store(settings.store, db, settings.data_dir, embedder.name)
    except Exception as exc:
        err.print(f"[red]Can't set up the index:[/red] {exc}")
        raise typer.Exit(1) from exc
    return embedder, store


def _searcher(settings: Settings, db: Database, modes: set[str], rerank: bool) -> Searcher:
    """Load only what the requested modes need: BM25 alone needs no model."""
    embedder = store = reranker = None
    if modes & {"vector", "hybrid"}:
        embedder, store = _index_parts(settings, db)
    if rerank:
        from docurag.index.rerank import FastEmbedReranker

        try:
            with console.status(f"Loading reranker {settings.search.reranker_model}..."):
                reranker = FastEmbedReranker(
                    settings.search.reranker_model, settings.embedder.cache_dir
                    or settings.data_dir / "models",
                )
        except Exception as exc:
            err.print(f"[red]Can't load the reranker:[/red] {exc}")
            raise typer.Exit(1) from exc
    cfg = settings.search
    return Searcher(
        db, embedder, store, reranker,
        candidates=cfg.candidates, rrf_k=cfg.rrf_k, rerank_candidates=cfg.rerank_candidates,
    )


def _run_index(
    db: Database, settings: Settings, library_id: int, name: str, force: bool = False
) -> IndexStats:
    embedder, store = _index_parts(settings, db)
    indexer = Indexer(db, embedder, store, settings.chunking)
    with console.status(f"Indexing {name}...") as status:

        def on_page(i: int, total: int, st: IndexStats) -> None:
            status.update(
                f"Indexing [bold]{name}[/bold] {i}/{total} pages · {st.chunks_written} chunks"
            )

        stats = indexer.index_library(library_id, force=force, on_page=on_page)
    console.print(
        f"[green]Indexed[/green] [bold]{name}[/bold]: {stats.pages_indexed} pages "
        f"({stats.chunks_written} chunks), {stats.pages_skipped} unchanged"
        + (f", {stats.pages_removed} removed" if stats.pages_removed else "")
        + f" · {embedder.name}"
    )
    return stats


def _run_crawl(db: Database, lib: LibraryConfig, settings: Settings, fresh: bool) -> None:
    stats_line = {"text": ""}

    with console.status("Starting crawl...") as status:

        def on_event(ev: CrawlEvent) -> None:
            s = ev.stats or {}
            stats_line["text"] = (
                f"saved {s.get('saved', 0)} · unchanged {s.get('unchanged', 0)} · "
                f"dup {s.get('duplicate', 0)} · skipped {s.get('skipped', 0)} · "
                f"failed {s.get('failed', 0)}"
                + (f" · removed {s['removed']}" if s.get("removed") else "")
                + f" · queued {ev.queued}"
            )
            status.update(f"[bold]{lib.name}[/bold] {stats_line['text']}")
            if ev.outcome == FAILED:
                err.print(f"[yellow]failed[/yellow] {ev.url}: {ev.detail}")

        crawler = Crawler(db, lib, settings, on_event=on_event)
        try:
            job_id = asyncio.run(crawler.run(fresh=fresh))
        except KeyboardInterrupt:
            err.print("[yellow]Interrupted.[/yellow] Run the same command again to resume.")
            raise typer.Exit(130) from None

    job = db.get_job(job_id) or {}
    console.print(
        f"[green]Job {job_id} {job.get('status')}[/green] for [bold]{lib.name}[/bold]: "
        f"{stats_line['text'] or 'nothing to do'}"
    )


@app.command()
def add(
    url: Annotated[str, typer.Argument(help="Docs URL, sitemap.xml or llms.txt to start from.")],
    name: Annotated[str, typer.Option("--name", "-n", help="Library name, e.g. fastapi.")],
    include: Annotated[
        list[str] | None, typer.Option("--include", "-i", help="Only keep paths matching.")
    ] = None,
    exclude: Annotated[
        list[str] | None, typer.Option("--exclude", "-x", help="Skip paths matching.")
    ] = None,
    version: Annotated[str | None, typer.Option(help="Version label for these pages.")] = None,
    rate_limit: Annotated[float | None, typer.Option(help="Requests/second per domain.")] = None,
    max_pages: Annotated[int | None, typer.Option(help="Stop after this many fetches.")] = None,
    max_depth: Annotated[int | None, typer.Option(help="Maximum link depth.")] = None,
    no_sitemap: Annotated[bool, typer.Option(help="Don't auto-discover sitemaps.")] = False,
    render_js: Annotated[
        str | None,
        typer.Option(help="Render pages in Chromium: off, on, or auto (only JS-shell pages)."),
    ] = None,
    version_pattern: Annotated[
        str | None,
        typer.Option(help=r"Regex with one group reading the version from the URL path, "
                          r"e.g. '^/docs/(\d+\.\d+)/'."),
    ] = None,
    fresh: Annotated[bool, typer.Option(help="Start a new job instead of resuming.")] = False,
    no_index: Annotated[bool, typer.Option(help="Crawl only; skip chunking/embedding.")] = False,
) -> None:
    """Add a docs site, crawl it and index it. Re-running resumes an unfinished crawl."""
    from docurag.serve.service import resolve_library

    settings = _settings()
    overrides = {
        "include": include or None, "exclude": exclude or None, "version": version,
        "rate_limit": rate_limit, "max_pages": max_pages, "max_depth": max_depth,
        "discover_sitemaps": False if no_sitemap else None,
        "version_pattern": version_pattern,
    }
    if render_js is not None:
        modes = {"off": False, "on": True, "auto": "auto"}
        if render_js not in modes:
            err.print("[red]--render-js must be off, on or auto[/red]")
            raise typer.Exit(2)
        overrides["render_js"] = modes[render_js]
    db = _db(settings)
    try:
        try:
            lib = resolve_library(settings, db, name, url, overrides)
        except Exception as exc:
            err.print(f"[red]Invalid options:[/red] {exc}")
            raise typer.Exit(2) from exc
        _run_crawl(db, lib, settings, fresh)
        if not no_index:
            _run_index(db, settings, db.get_library(lib.name)["id"], lib.name)
    finally:
        db.close()


@app.command()
def update(
    name: Annotated[str, typer.Argument(help="Library to re-crawl.")],
    fresh: Annotated[bool, typer.Option(help="Start a new job instead of resuming.")] = False,
    no_index: Annotated[bool, typer.Option(help="Crawl only; skip chunking/embedding.")] = False,
    full: Annotated[
        bool, typer.Option(help="Ignore sitemap <lastmod> and ask the server about every page.")
    ] = False,
) -> None:
    """Re-crawl and re-index a library. Unchanged pages are skipped."""
    from docurag.serve.service import resolve_library

    settings = _settings()
    db = _db(settings)
    try:
        try:
            overrides = {"trust_sitemap_lastmod": False} if full else None
            lib = resolve_library(settings, db, name, overrides=overrides)
        except LookupError:
            err.print(f"[red]Unknown library:[/red] {name}. Add it with `docurag add`.")
            raise typer.Exit(1) from None
        _run_crawl(db, lib, settings, fresh)
        if not no_index:
            _run_index(db, settings, db.get_library(lib.name)["id"], lib.name)
    finally:
        db.close()


@app.command()
def index(
    name: Annotated[str | None, typer.Argument(help="Library to index (default: all).")] = None,
    force: Annotated[bool, typer.Option(help="Re-chunk and re-embed every page.")] = False,
) -> None:
    """Chunk and embed crawled pages. Only new or changed pages are processed."""
    settings = _settings()
    db = _db(settings)
    try:
        if name:
            row = db.get_library(name)
            if row is None:
                err.print(f"[red]Unknown library:[/red] {name}")
                raise typer.Exit(1)
            targets = [(row["id"], name)]
        else:
            targets = [(r["id"], r["name"]) for r in db.list_libraries()]
        if not targets:
            err.print("No libraries yet. Add one with `docurag add`.")
            raise typer.Exit(1)
        for lib_id, lib_name in targets:
            _run_index(db, settings, lib_id, lib_name, force=force)
    finally:
        db.close()


@app.command()
def search(
    query: Annotated[str, typer.Argument(help="What to look for.")],
    lib: Annotated[str | None, typer.Option("--lib", "-l", help="Library name.")] = None,
    version: Annotated[str | None, typer.Option(help="Only this version.")] = None,
    k: Annotated[int, typer.Option("--k", "-k", min=1, max=50, help="Results to show.")] = 5,
    mode: Annotated[
        str | None, typer.Option(help="vector, bm25 or hybrid (default from config: hybrid).")
    ] = None,
    rerank: Annotated[
        bool | None, typer.Option("--rerank/--no-rerank", help="Cross-encoder reranking.")
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print JSON.")] = False,
    full: Annotated[bool, typer.Option(help="Print whole chunks, not snippets.")] = False,
) -> None:
    """Search indexed docs (hybrid vector + BM25 by default)."""
    import time

    from docurag.index.search import MODES

    settings = _settings()
    mode = mode or settings.search.mode
    if mode not in MODES:
        err.print(f"[red]--mode must be one of:[/red] {', '.join(MODES)}")
        raise typer.Exit(2)
    rerank = settings.search.rerank if rerank is None else rerank
    db = _db(settings)
    try:
        searcher = _searcher(settings, db, {mode}, rerank)
        started = time.perf_counter()
        try:
            hits = searcher.search(query, lib, version, k, mode=mode, rerank=rerank)
        except UnknownLibrary:
            err.print(f"[red]Unknown library:[/red] {lib}")
            raise typer.Exit(1) from None
        elapsed_ms = (time.perf_counter() - started) * 1000
    finally:
        db.close()

    if as_json:
        typer.echo(json.dumps([h.as_dict() for h in hits], ensure_ascii=False, indent=2))
        return
    if not hits:
        console.print("No results. Is the library indexed? Try `docurag index`.")
        return
    for rank, h in enumerate(hits, 1):
        console.print(
            f"[bold cyan]{rank}.[/bold cyan] [bold]{h.section}[/bold] "
            f"[dim]({h.library}@{h.version}, score {h.score:.3f})[/dim]"
        )
        console.print(f"   [link={h.citation}]{h.citation}[/link]", style="blue")
        body = h.text if full else " ".join(h.text.split())[:280] + (
            "…" if len(h.text) > 280 else ""
        )
        console.print(Markdown(body) if full else f"   {body}", highlight=False)
        console.print()
    label = mode + ("+rerank" if rerank else "")
    console.print(f"[dim]{len(hits)} results in {elapsed_ms:.0f} ms ({label})[/dim]")


@app.command("eval")
def eval_cmd(
    dataset: Annotated[
        Path | None, typer.Option(help="Questions JSONL (default: bundled eval set).")
    ] = None,
    configs: Annotated[
        str, typer.Option(help="Comma-separated: vector, bm25, hybrid, hybrid+rerank.")
    ] = "vector,bm25,hybrid,hybrid+rerank",
    k: Annotated[int, typer.Option("--k", "-k", min=1, max=50)] = 10,
    out: Annotated[Path | None, typer.Option(help="Write full results as JSON.")] = None,
) -> None:
    """Measure retrieval quality: Recall@k, MRR and latency per configuration."""
    from docurag.eval.run_eval import (
        DEFAULT_DATASET,
        RunConfig,
        evaluate,
        load_dataset,
        missing_pages,
        to_json,
        to_markdown,
    )

    settings = _settings()
    try:
        runs = [RunConfig.parse(c) for c in configs.split(",") if c.strip()]
        questions = load_dataset(dataset or DEFAULT_DATASET)
    except (ValueError, OSError) as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc

    db = _db(settings)
    try:
        known = {r[0] for r in db.conn.execute("SELECT url FROM pages")}
        missing = missing_pages(questions, known)
        if missing:
            err.print(
                f"[yellow]{len(missing)} of {len(questions)} questions skipped: none of their "
                "expected pages are indexed.[/yellow] Build the corpus first, see "
                "docurag/eval/corpus.yaml."
            )
            questions = [q for q in questions if q.id not in missing]
        if not questions:
            raise typer.Exit(1)
        searcher = _searcher(
            settings, db, {r.mode for r in runs}, any(r.rerank for r in runs)
        )

        def run(q, cfg, top_k):
            return searcher.search(q.question, q.library, q.version, top_k,
                                   mode=cfg.mode, rerank=cfg.rerank)

        with console.status("Evaluating...") as status:
            reports = evaluate(
                run, questions, runs, k,
                on_progress=lambda c, i, n: status.update(f"Evaluating {c.label}: {i}/{n}"),
            )
    finally:
        db.close()

    console.print(f"{len(questions)} questions · embedder {settings.embedder.model}"
                  + (f" · reranker {settings.search.reranker_model}"
                     if any(r.rerank for r in runs) else ""))
    typer.echo(to_markdown(reports))
    if out:
        meta = {
            "questions": len(questions), "skipped": sorted(missing), "k": k,
            "embedder": settings.embedder.model_dump(mode="json"),
            "search": settings.search.model_dump(mode="json"),
            "chunking": settings.chunking.model_dump(mode="json"),
        }
        out.write_text(to_json(reports, meta), encoding="utf-8")
        console.print(f"[dim]Full results: {out}[/dim]")


@app.command("libraries")
def libraries() -> None:
    """List indexed libraries."""
    settings = _settings()
    db = _db(settings)
    try:
        rows = db.list_libraries()
    finally:
        db.close()
    if not rows:
        console.print(
            "No libraries yet. Try: docurag add https://fastapi.tiangolo.com --name fastapi"
        )
        return
    table = Table("name", "pages", "indexed", "chunks", "versions", "base url", "last fetched")
    for r in rows:
        table.add_row(
            r["name"], str(r["pages"]), str(r["pages_indexed"]), str(r["chunks"]),
            ", ".join(r["versions"]) or "-", r["base_url"], r["last_fetched"] or "-",
        )
    console.print(table)


@app.command()
def jobs(
    name: Annotated[str | None, typer.Argument(help="Filter by library.")] = None,
    limit: int = 10,
) -> None:
    """Show recent crawl jobs."""
    settings = _settings()
    db = _db(settings)
    try:
        lib_id = None
        if name:
            row = db.get_library(name)
            if row is None:
                err.print(f"[red]Unknown library:[/red] {name}")
                raise typer.Exit(1)
            lib_id = row["id"]
        rows = db.list_jobs(lib_id, limit)
    finally:
        db.close()
    table = Table("id", "library", "status", "fetched", "stats", "started", "finished")
    for r in rows:
        stats = json.loads(r["stats_json"])
        table.add_row(
            str(r["id"]), r["library"], r["status"], str(r["pages_done"]),
            " ".join(f"{k}={v}" for k, v in sorted(stats.items())),
            r["started_at"], r["finished_at"] or "-",
        )
    console.print(table)


@app.command()
def page(
    url: Annotated[str, typer.Argument(help="Page URL.")],
    raw: Annotated[bool, typer.Option(help="Print raw Markdown.")] = False,
) -> None:
    """Print the stored Markdown for a page."""
    settings = _settings()
    db = _db(settings)
    try:
        rows = db.find_pages_by_url(normalize_url(url) or url)
    finally:
        db.close()
    if not rows:
        err.print(f"[red]Not indexed:[/red] {url}")
        raise typer.Exit(1)
    row = rows[0]
    if raw:
        typer.echo(row["markdown"])
    else:
        meta = f"{row['library']} @ {row['version']} · fetched {row['fetched_at']}"
        console.print(f"[dim]{meta}[/dim]")
        console.print(Markdown(row["markdown"]))


@app.command()
def remove(name: Annotated[str, typer.Argument(help="Library to delete.")]) -> None:
    """Delete a library and all its pages."""
    settings = _settings()
    db = _db(settings)
    try:
        row = db.get_library(name)
        if row is not None and settings.store.type == "qdrant":
            from docurag.index.embedder import embedder_name
            from docurag.index.store import make_store

            # sqlite vectors go with the cascade; Qdrant points must be removed explicitly
            model = embedder_name(settings.embedder)
            make_store(settings.store, db, settings.data_dir, model).delete_library(row["id"])
        if not db.delete_library(name):
            err.print(f"[red]Unknown library:[/red] {name}")
            raise typer.Exit(1)
    finally:
        db.close()
    console.print(f"Removed {name}.")


@app.command()
def serve(
    mcp: Annotated[
        bool, typer.Option("--mcp", help="Run the MCP server over stdio (for Claude Code, Cursor).")
    ] = False,
    host: Annotated[str, typer.Option(help="HTTP bind address.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="HTTP port.")] = 8000,
    no_mcp_http: Annotated[
        bool, typer.Option(help="Don't mount MCP at /mcp on the HTTP server.")
    ] = False,
) -> None:
    """Serve the docs: REST API + MCP at /mcp over HTTP, or MCP over stdio with --mcp."""
    import os
    import threading

    from docurag.serve.service import DocuRAGService

    settings = _settings()
    if mcp:
        # stdout belongs to the MCP protocol: everything else goes to stderr.
        from docurag.serve.mcp_server import create_mcp_server

        service = DocuRAGService(settings)
        threading.Thread(target=service.warm_up, daemon=True).start()
        try:
            create_mcp_server(service).run("stdio")
        finally:
            service.db.close()
        return

    import uvicorn

    from docurag.serve.api import API_KEY_ENV, create_app

    if host not in ("127.0.0.1", "localhost", "::1") and not os.environ.get(API_KEY_ENV):
        err.print(
            f"[yellow]Warning:[/yellow] listening on {host} without {API_KEY_ENV}. Anyone who "
            "can reach this port can search your docs and make the server crawl any URL."
        )
    app_ = create_app(settings, mount_mcp=not no_mcp_http, host=host)
    err.print(f"DocuRAG API on http://{host}:{port}  (docs: /docs"
              + ("" if no_mcp_http else ", MCP: /mcp") + ")")
    uvicorn.run(app_, host=host, port=port, log_level="warning")


@app.command()
def version() -> None:
    """Print the DocuRAG version."""
    typer.echo(__version__)


if __name__ == "__main__":
    app()
