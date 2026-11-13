"""MCP server (F16): lets Claude Code, Cursor and other assistants search the docs.

Tools:
  search_docs(query, library?, version?, k?)  ranked chunks with url#section citations
  list_libraries()                           what is indexed, with versions and sizes
  get_page(url)                              a whole page as Markdown

Results are Markdown, because the reader is a language model.
"""

from __future__ import annotations

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from docurag import __version__
from docurag.index.search import SearchHit, UnknownLibrary
from docurag.serve.service import DocuRAGService

INSTRUCTIONS = """\
DocuRAG serves up-to-date, versioned library documentation crawled from the official docs.
Prefer it over memory for APIs that change between versions.
1. Call list_libraries to see which libraries and versions are indexed.
2. Call search_docs with a specific question and the library name.
3. Call get_page on a result's URL when you need the full page.
Cite the Source URL of the sections you rely on."""

READ_ONLY = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)
MAX_K = 20


def format_hits(query: str, hits: list[SearchHit], scope: str) -> str:
    if not hits:
        return f"No results for {query!r} in {scope}. Try other words or check list_libraries."
    out = [f"{len(hits)} results for {query!r} in {scope}:\n"]
    for i, h in enumerate(hits, 1):
        out.append(
            f"## {i}. {h.section or h.title or h.url}\n"
            f"Source: {h.citation} ({h.library}@{h.version})\n\n{h.text.strip()}\n"
        )
    return "\n".join(out)


def format_libraries(rows: list[dict]) -> str:
    if not rows:
        return "No libraries indexed yet. Add one with: docurag add <docs-url> --name <name>"
    lines = ["| library | versions | pages | chunks | source |", "| --- | --- | --- | --- | --- |"]
    for r in rows:
        versions = ", ".join(r["versions"]) or "-"
        note = " (crawl running)" if r.get("crawling") else ""
        lines.append(
            f"| {r['name']}{note} | {versions} | {r['pages']} | {r['chunks']} | {r['base_url']} |"
        )
    return "\n".join(lines)


def create_mcp_server(service: DocuRAGService) -> MCPServer:
    mcp = MCPServer(
        "docurag",
        title="DocuRAG",
        version=__version__,
        instructions=INSTRUCTIONS,
        description="Search up-to-date, versioned library documentation with citations.",
    )

    @mcp.tool(annotations=READ_ONLY)
    async def search_docs(
        query: str, library: str | None = None, version: str | None = None, k: int = 5
    ) -> str:
        """Search indexed documentation and return the most relevant sections.

        Args:
            query: A specific question or API name, e.g. "declare cookie parameters".
            library: Library name from list_libraries, e.g. "fastapi". Omit to search all.
            version: Only this version, e.g. "0.115". Omit for all indexed versions.
            k: Number of sections to return (1-20).
        """
        k = max(1, min(MAX_K, k))
        try:
            hits, _, _ = await anyio.to_thread.run_sync(
                lambda: service.search(query, library, version, k)
            )
        except UnknownLibrary:
            names = ", ".join(service.library_names()) or "none yet"
            return f"Unknown library {library!r}. Indexed libraries: {names}."
        scope = f"{library}@{version}" if library and version else (library or "all libraries")
        return format_hits(query, hits, scope)

    @mcp.tool(annotations=READ_ONLY)
    async def list_libraries() -> str:
        """List indexed libraries with their versions and sizes."""
        return format_libraries(await anyio.to_thread.run_sync(service.list_libraries))

    @mcp.tool(annotations=READ_ONLY)
    async def get_page(url: str, max_chars: int = 40_000) -> str:
        """Return a whole documentation page as Markdown.

        Args:
            url: Page URL from a search result (a #section fragment is fine).
            max_chars: Truncate longer pages to this many characters.
        """
        page = await anyio.to_thread.run_sync(lambda: service.get_page(url.split("#")[0]))
        if page is None:
            return f"{url} is not indexed. Use search_docs to find indexed pages."
        md = page["markdown"]
        if len(md) > max_chars:
            md = md[:max_chars] + f"\n\n[truncated: page has {len(page['markdown'])} characters]"
        return (
            f"# {page['title'] or page['url']}\n"
            f"Source: {page['url']} ({page['library']}@{page['version']}, "
            f"fetched {page['fetched_at']})\n\n{md}"
        )

    return mcp
