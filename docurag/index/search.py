"""Query-time retrieval: vector, BM25 or hybrid (RRF), with optional reranking."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from docurag.crawler.versions import pick_latest
from docurag.db import Database
from docurag.index.chunker import context_line
from docurag.index.embedder import Embedder
from docurag.index.hybrid import Ranked, bm25_search, rrf
from docurag.index.pipeline import embedding_text
from docurag.index.rerank import Reranker
from docurag.index.store import VectorStore

MODES = ("vector", "bm25", "hybrid")


class UnknownLibrary(LookupError):
    pass


@dataclass
class SearchHit:
    chunk_id: int
    score: float  # final ranking score: cosine, BM25, RRF or reranker, depending on mode
    library: str
    version: str
    url: str
    anchor: str | None
    title: str | None
    heading_path: list[str]
    text: str
    # Every signal that contributed, e.g. {"vector": 0.81, "bm25": 7.2, "rrf": 0.032}
    scores: dict[str, float] = field(default_factory=dict)

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
    def __init__(
        self,
        db: Database,
        embedder: Embedder | None = None,
        store: VectorStore | None = None,
        reranker: Reranker | None = None,
        *,
        candidates: int = 50,
        rrf_k: int = 60,
        rerank_candidates: int = 20,
    ):
        self.db = db
        self.embedder = embedder
        self.store = store
        self.reranker = reranker
        self.candidates = candidates
        self.rrf_k = rrf_k
        self.rerank_candidates = rerank_candidates

    def _library_id(self, library: str | None) -> int | None:
        if library is None:
            return None
        row = self.db.get_library(library)
        if row is None:
            raise UnknownLibrary(library)
        return row["id"]

    def resolve_version(self, library_id: int | None, version: str | None) -> str | None:
        """'latest' means the newest indexed version unless pages carry that literal label."""
        if version != "latest" or library_id is None:
            return version
        return pick_latest(self.db.library_versions(library_id)) or version

    def _vector(self, query: str, k: int, lib: int | None, version: str | None) -> Ranked:
        if self.embedder is None or self.store is None:
            raise ValueError("vector and hybrid search need an embedder and a vector store")
        return self.store.search(self.embedder.embed_query(query), k, lib, version)

    def search(
        self,
        query: str,
        library: str | None = None,
        version: str | None = None,
        k: int = 5,
        *,
        mode: str = "hybrid",
        rerank: bool = False,
    ) -> list[SearchHit]:
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        if rerank and self.reranker is None:
            raise ValueError("rerank=True needs a reranker")
        lib = self._library_id(library)
        version = self.resolve_version(lib, version)
        query = query.strip()
        if not query:
            return []

        pool = max(k, self.rerank_candidates if rerank else k)
        signals: dict[int, dict[str, float]] = {}

        def note(name: str, ranked: Ranked) -> Ranked:
            for cid, s in ranked:
                signals.setdefault(cid, {})[name] = s
            return ranked

        if mode == "vector":
            ranked = note("vector", self._vector(query, pool, lib, version))
        elif mode == "bm25":
            ranked = note("bm25", bm25_search(self.db, query, pool, lib, version))
        else:
            n = max(self.candidates, pool)
            dense = note("vector", self._vector(query, n, lib, version))
            sparse = note("bm25", bm25_search(self.db, query, n, lib, version))
            ranked = note("rrf", rrf([dense, sparse], self.rrf_k))[:pool]

        chunks = self.db.get_chunks([cid for cid, _ in ranked])
        ranked = [(cid, s) for cid, s in ranked if cid in chunks]  # drop store/DB drift

        if rerank and ranked:
            assert self.reranker is not None
            docs = [
                embedding_text(chunks[cid]["title"], chunks[cid]["heading_path"],
                               chunks[cid]["text"])
                for cid, _ in ranked
            ]
            scores = self.reranker.score(query, docs)
            ranked = note("rerank", sorted(
                ((cid, s) for (cid, _), s in zip(ranked, scores, strict=True)),
                key=lambda kv: -kv[1],
            ))

        hits = []
        for cid, score in ranked[:k]:
            c = chunks[cid]
            hits.append(
                SearchHit(
                    chunk_id=cid, score=score, library=c["library"], version=c["version"],
                    url=c["url"], anchor=c["anchor"], title=c["title"],
                    heading_path=c["heading_path"], text=c["text"], scores=signals[cid],
                )
            )
        return hits
