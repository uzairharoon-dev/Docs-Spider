"""Query-time retrieval. M2: dense vectors only; hybrid BM25 + RRF arrives in M3."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from docurag.db import Database
from docurag.index.embedder import Embedder
from docurag.index.pipeline import context_line
from docurag.index.store import VectorStore


class UnknownLibrary(LookupError):
    pass


@dataclass
class SearchHit:
    chunk_id: int
    score: float
    library: str
    version: str
    url: str
    anchor: str | None
    title: str | None
    heading_path: list[str]
    text: str

    @property
    def citation(self) -> str:
        """Source URL with the section fragment when known (G3)."""
        return f"{self.url}#{self.anchor}" if self.anchor else self.url

    @property
    def section(self) -> str:
        return context_line(self.title, self.heading_path)

    def as_dict(self) -> dict:
        d = asdict(self)
        d["citation"] = self.citation
        d["section"] = self.section
        return d


class Searcher:
    def __init__(self, db: Database, embedder: Embedder, store: VectorStore):
        self.db = db
        self.embedder = embedder
        self.store = store

    def search(
        self, query: str, library: str | None = None, version: str | None = None, k: int = 5
    ) -> list[SearchHit]:
        library_id = None
        if library is not None:
            row = self.db.get_library(library)
            if row is None:
                raise UnknownLibrary(library)
            library_id = row["id"]
        query = query.strip()
        if not query:
            return []
        vector = self.embedder.embed_query(query)
        scored = self.store.search(vector, k, library_id=library_id, version=version)
        chunks = self.db.get_chunks([cid for cid, _ in scored])
        hits = []
        for cid, score in scored:
            c = chunks.get(cid)
            if c is None:  # vector without a chunk row (store out of sync); skip it
                continue
            hits.append(
                SearchHit(
                    chunk_id=cid, score=score, library=c["library"], version=c["version"],
                    url=c["url"], anchor=c["anchor"], title=c["title"],
                    heading_path=c["heading_path"], text=c["text"],
                )
            )
        return hits
