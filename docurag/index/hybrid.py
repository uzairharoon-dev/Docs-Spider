"""Keyword retrieval (SQLite FTS5 BM25) and reciprocal rank fusion (F14)."""

from __future__ import annotations

import re
from collections.abc import Iterable

from docurag.db import Database

# Chunk text weighs 1, "Title > Section" context weighs 2: a query term in a heading
# says more about what the chunk is about than the same term in passing.
BM25_WEIGHTS = (1.0, 2.0)
MAX_QUERY_TERMS = 32

_TERM = re.compile(r"\w+", re.UNICODE)
# Question words and glue that only add noise to an OR query. Kept short on purpose:
# words like "not", "use" or "return" can matter in API docs.
STOPWORDS = frozenset(
    [
        "a", "an", "and", "are", "as", "at", "be", "by", "can", "do", "does", "for", "from",
        "how", "i", "if", "in", "into", "is", "it", "its", "me", "my", "of", "on", "or", "so",
        "than", "that", "the", "their", "them", "then", "there", "these", "this", "to", "was",
        "we", "what", "when", "where", "which", "who", "why", "will", "with", "you", "your",
    ]
)

Ranked = list[tuple[int, float]]  # (chunk_id, score), best first


def fts_query(text: str) -> str | None:
    """Turn free text into a safe FTS5 query: quoted terms joined by OR.

    Quoting neutralises FTS5 syntax (``AND``, ``NEAR``, ``*``, ``:``, quotes) in user
    input. OR instead of FTS5's default AND, so one missing word doesn't empty the result.
    """
    terms = [t for t in _TERM.findall(text.lower()) if t not in STOPWORDS]
    if not terms:  # a query made only of stopwords: search them anyway
        terms = _TERM.findall(text.lower())
    terms = list(dict.fromkeys(terms))[:MAX_QUERY_TERMS]
    return " OR ".join(f'"{t}"' for t in terms) if terms else None


def bm25_search(
    db: Database,
    query: str,
    k: int,
    library_id: int | None = None,
    version: str | None = None,
) -> Ranked:
    match = fts_query(query)
    if match is None or k <= 0:
        return []
    sql = f"""SELECT c.id, bm25(chunks_fts, {BM25_WEIGHTS[0]}, {BM25_WEIGHTS[1]}) AS score
              FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid
              WHERE chunks_fts MATCH ?"""
    params: list = [match]
    if library_id is not None:
        sql += " AND c.library_id = ?"
        params.append(library_id)
    if version is not None:
        sql += " AND c.version = ?"
        params.append(version)
    sql += " ORDER BY score LIMIT ?"
    params.append(k)
    # FTS5's bm25() is "lower is better"; flip it so every retriever ranks high-to-low.
    return [(r[0], -r[1]) for r in db.conn.execute(sql, params)]


def rrf(rankings: Iterable[Ranked], k: int = 60) -> Ranked:
    """Reciprocal rank fusion: score(d) = sum over lists of 1 / (k + rank(d)).

    Uses ranks only, so BM25 scores and cosine similarities need no calibration.
    """
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, (doc, _) in enumerate(ranking, 1):
            scores[doc] = scores.get(doc, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
