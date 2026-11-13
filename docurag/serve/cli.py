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


def _run_crawl(db: Database, lib: LibraryConfig, settings: Settings, fresh: bool) -> None:
    stats_line = {"text": ""}

    with console.status("Starting crawl...") as status:

        def on_event(ev: CrawlEvent) -> None:
            s = ev.stats or {}
            stats_line["text"] = (
                f"saved {s.get('saved', 0)} · unchanged {s.get('unchanged', 0)} · "
                f"dup {s.get('duplicate', 0)} · skipped {s.get('skipped', 0)} · "
                f"failed {s.get('failed', 0)} · queued {ev.queued}"
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
    fresh: Annotated[bool, typer.Option(help="Start a new job instead of resuming.")] = False,
) -> None:
    """Add a docs site and crawl it. Re-running resumes an unfinished crawl."""
    settings = _settings()
    if normalize_url(url) is None:
        err.print(f"[red]Not an http(s) URL:[/red] {url}")
        raise typer.Exit(2)
    base = settings.library(name)
    data = base.model_dump() if base else {"name": name}
    data["start_urls"] = [url]
    overrides = {
        "include": include or None, "exclude": exclude or None, "version": version,
        "rate_limit": rate_limit, "max_pages": max_pages, "max_depth": max_depth,
    }
    data.update({k: v for k, v in overrides.items() if v is not None})
    if no_sitemap:
        data["discover_sitemaps"] = False
    try:
        lib = LibraryConfig.model_validate(data)
    except Exception as exc:
        err.print(f"[red]Invalid options:[/red] {exc}")
        raise typer.Exit(2) from exc
    db = _db(settings)
    try:
        _run_crawl(db, lib, settings, fresh)
    finally:
        db.close()


@app.command()
def update(
    name: Annotated[str, typer.Argument(help="Library to re-crawl.")],
    fresh: Annotated[bool, typer.Option(help="Start a new job instead of resuming.")] = False,
) -> None:
    """Re-crawl a library. Unchanged pages are skipped via ETag / Last-Modified."""
    settings = _settings()
    db = _db(settings)
    try:
        lib = settings.library(name)
        if lib is None:
            row = db.get_library(name)
            if row is None:
                err.print(f"[red]Unknown library:[/red] {name}. Add it with `docurag add`.")
                raise typer.Exit(1)
            lib = LibraryConfig.model_validate(json.loads(row["config_json"]))
        _run_crawl(db, lib, settings, fresh)
    finally:
        db.close()


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
    table = Table("name", "pages", "versions", "base url", "last fetched")
    for r in rows:
        table.add_row(
            r["name"], str(r["pages"]), ", ".join(r["versions"]) or "-", r["base_url"],
            r["last_fetched"] or "-",
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
        if not db.delete_library(name):
            err.print(f"[red]Unknown library:[/red] {name}")
            raise typer.Exit(1)
    finally:
        db.close()
    console.print(f"Removed {name}.")


@app.command()
def version() -> None:
    """Print the DocuRAG version."""
    typer.echo(__version__)


if __name__ == "__main__":
    app()
