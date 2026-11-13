"""Pages -> chunks -> vectors. Incremental: a page is re-indexed only when its
content or the embedding model changed since it was last indexed."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

from docurag.config import ChunkConfig
from docurag.db import Database
from docurag.index.chunker import chunk_markdown, context_line
from docurag.index.embedder import Embedder
from docurag.index.store import VectorItem, VectorStore


@dataclass
class IndexStats:
    pages_indexed: int = 0
    pages_skipped: int = 0
    chunks_written: int = 0
    chunks_deleted: int = 0


def embedding_text(title: str | None, heading_path: list[str], text: str) -> str:
    # F12: the heading path goes into the embedded text so a chunk is self-describing.
    ctx = context_line(title, heading_path)
    return f"{ctx}\n\n{text}" if ctx else text


class Indexer:
    def __init__(
        self, db: Database, embedder: Embedder, store: VectorStore, chunking: ChunkConfig
    ):
        self.db = db
        self.embedder = embedder
        self.store = store
        self.chunking = chunking

    def needs_index(self, page: sqlite3.Row) -> bool:
        return (
            page["indexed_hash"] != page["content_hash"]
            or page["indexed_model"] != self.embedder.name
        )

    def index_library(
        self,
        library_id: int,
        version: str | None = None,
        *,
        force: bool = False,
        on_page: Callable[[int, int, IndexStats], None] | None = None,
    ) -> IndexStats:
        stats = IndexStats()
        pages = self.db.pages_for_index(library_id, version)
        for i, page in enumerate(pages, 1):
            if force or self.needs_index(page):
                written, deleted = self.index_page(page)
                stats.pages_indexed += 1
                stats.chunks_written += written
                stats.chunks_deleted += deleted
            else:
                stats.pages_skipped += 1
            if on_page:
                on_page(i, len(pages), stats)
        return stats

    def index_page(self, page: sqlite3.Row) -> tuple[int, int]:
        anchors = [tuple(a) for a in json.loads(page["anchors_json"] or "[]")]
        chunks = chunk_markdown(
            page["markdown"],
            anchors=anchors,
            min_tokens=self.chunking.min_tokens,
            max_tokens=self.chunking.max_tokens,
            overlap=self.chunking.overlap,
        )
        # Embed first: if the model fails, the page's old chunks stay searchable.
        vectors = self.embedder.embed_documents(
            [embedding_text(page["title"], c.heading_path, c.text) for c in chunks]
        )
        old_ids = self.db.chunk_ids_for_page(page["id"])
        self.store.delete(old_ids)
        with self.db.transaction():
            new_ids = self.db.replace_chunks(
                page,
                [{**c.as_dict(), "context": context_line(page["title"], c.heading_path)}
                 for c in chunks],
            )
        self.store.upsert(
            [
                VectorItem(cid, page["library_id"], page["version"], vec)
                for cid, vec in zip(new_ids, vectors, strict=True)
            ]
        )
        # Marked last: a crash before this line just means the page is redone next time.
        self.db.mark_indexed(page["id"], page["content_hash"], self.embedder.name)
        return len(new_ids), len(old_ids)

    def remove_library(self, library_id: int) -> None:
        self.store.delete_library(library_id)
