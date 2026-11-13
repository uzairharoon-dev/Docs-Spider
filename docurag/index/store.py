"""Vector stores.

* ``sqlite`` (default): vectors as float32 blobs in the metadata DB, searched by
  brute-force dot product in numpy. No server; fine up to a few hundred thousand chunks.
* ``qdrant``: a Qdrant server (``url``) or Qdrant's embedded mode (``path``).

Both keep one vector space per embedding model, so switching models never mixes vectors.
"""

from __future__ import annotations

import contextlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np

from docurag.config import StoreConfig
from docurag.db import Database


@dataclass
class VectorItem:
    chunk_id: int
    library_id: int
    version: str
    vector: np.ndarray


class VectorStore(Protocol):
    def upsert(self, items: list[VectorItem]) -> None: ...

    def delete(self, chunk_ids: list[int]) -> None: ...

    def delete_library(self, library_id: int) -> None: ...

    def search(
        self, vector: np.ndarray, k: int, library_id: int | None = None, version: str | None = None
    ) -> list[tuple[int, float]]: ...


Hits = list[tuple[int, float]]  # (chunk_id, cosine score), best first


def _top_k(ids: np.ndarray, matrix: np.ndarray, vector: np.ndarray, k: int) -> Hits:
    if len(ids) == 0 or k <= 0:
        return []
    scores = matrix @ vector.astype(np.float32)
    k = min(k, len(ids))
    top = np.argpartition(-scores, k - 1)[:k]
    top = top[np.argsort(-scores[top])]
    return [(int(ids[i]), float(scores[i])) for i in top]


class SQLiteVectorStore:
    def __init__(self, db: Database, model: str):
        self.db = db
        self.model = model
        # (library_id, version) -> (change signature, ids, matrix)
        self._cache: dict[tuple, tuple[tuple, np.ndarray, np.ndarray]] = {}

    def upsert(self, items: list[VectorItem]) -> None:
        if not items:
            return
        with self.db.transaction() as conn:
            seq = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM chunk_vectors").fetchone()[0]
            rows = [
                (it.chunk_id, self.model, it.library_id, it.version, seq,
                 np.asarray(it.vector, dtype="<f4").tobytes())
                for it in items
            ]
            conn.executemany(
                """INSERT OR REPLACE INTO chunk_vectors
                       (chunk_id, model, library_id, version, seq, vector)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                rows,
            )

    def delete(self, chunk_ids: list[int]) -> None:
        with self.db.transaction() as conn:
            conn.executemany(
                "DELETE FROM chunk_vectors WHERE chunk_id=?", [(i,) for i in chunk_ids]
            )

    def delete_library(self, library_id: int) -> None:
        self.db.conn.execute("DELETE FROM chunk_vectors WHERE library_id=?", (library_id,))

    def _where(self, library_id: int | None, version: str | None) -> tuple[str, tuple]:
        sql, params = "model=?", [self.model]
        if library_id is not None:
            sql += " AND library_id=?"
            params.append(library_id)
        if version is not None:
            sql += " AND version=?"
            params.append(version)
        return sql, tuple(params)

    def _load(self, library_id: int | None, version: str | None) -> tuple[np.ndarray, np.ndarray]:
        where, params = self._where(library_id, version)
        # Cheap change detector, also across processes: every write raises MAX(seq)
        # and every delete lowers COUNT(*).
        sig = tuple(self.db.conn.execute(
            f"SELECT COUNT(*), MAX(seq) FROM chunk_vectors WHERE {where}", params
        ).fetchone())
        key = (library_id, version)
        cached = self._cache.get(key)
        if cached and cached[0] == sig:
            return cached[1], cached[2]
        rows = self.db.conn.execute(
            f"SELECT chunk_id, vector FROM chunk_vectors WHERE {where}", params
        ).fetchall()
        if rows:
            ids = np.fromiter((r[0] for r in rows), dtype=np.int64, count=len(rows))
            matrix = np.frombuffer(b"".join(r[1] for r in rows), dtype="<f4").reshape(len(rows), -1)
        else:
            ids, matrix = np.zeros(0, dtype=np.int64), np.zeros((0, 0), dtype=np.float32)
        self._cache[key] = (sig, ids, matrix)
        return ids, matrix

    def search(
        self, vector: np.ndarray, k: int, library_id: int | None = None, version: str | None = None
    ) -> list[tuple[int, float]]:
        ids, matrix = self._load(library_id, version)
        if matrix.size and matrix.shape[1] != vector.shape[0]:
            raise RuntimeError(
                f"stored vectors have dim {matrix.shape[1]} but the query has {vector.shape[0]};"
                " re-run `docurag index --force`"
            )
        return _top_k(ids, matrix, vector, k)


class QdrantVectorStore:
    def __init__(self, cfg: StoreConfig, data_dir: Path, model: str):
        try:
            from qdrant_client import QdrantClient, models
        except ImportError as exc:
            raise RuntimeError("store 'qdrant' needs: pip install 'docurag[qdrant]'") from exc
        self.models = models
        self._remote = bool(cfg.url)
        if cfg.url:
            self.client = QdrantClient(url=cfg.url, api_key=os.environ.get(cfg.api_key_env))
        else:
            path = cfg.path or data_dir / "qdrant"
            self.client = QdrantClient(path=str(path)) if str(path) != ":memory:" else (
                QdrantClient(":memory:")
            )
        slug = re.sub(r"[^a-zA-Z0-9]+", "_", model).strip("_").lower()
        self.collection = f"{cfg.collection_prefix}_{slug}"
        self._ready = self.client.collection_exists(self.collection)

    def _ensure(self, dim: int) -> None:
        if self._ready:
            return
        m = self.models
        self.client.create_collection(
            self.collection, vectors_config=m.VectorParams(size=dim, distance=m.Distance.COSINE)
        )
        if not self._remote:  # embedded mode has no payload indexes; filters still work
            self._ready = True
            return
        for field, schema in (("library_id", m.PayloadSchemaType.INTEGER),
                              ("version", m.PayloadSchemaType.KEYWORD)):
            with contextlib.suppress(Exception):
                self.client.create_payload_index(self.collection, field, field_schema=schema)
        self._ready = True

    def _filter(self, library_id: int | None, version: str | None):
        m = self.models
        must = []
        if library_id is not None:
            must.append(m.FieldCondition(key="library_id", match=m.MatchValue(value=library_id)))
        if version is not None:
            must.append(m.FieldCondition(key="version", match=m.MatchValue(value=version)))
        return m.Filter(must=must) if must else None

    def upsert(self, items: list[VectorItem]) -> None:
        if not items:
            return
        self._ensure(int(np.asarray(items[0].vector).shape[0]))
        points = [
            self.models.PointStruct(
                id=it.chunk_id,
                vector=np.asarray(it.vector, dtype=np.float32).tolist(),
                payload={"library_id": it.library_id, "version": it.version},
            )
            for it in items
        ]
        self.client.upsert(self.collection, points=points)

    def delete(self, chunk_ids: list[int]) -> None:
        if self._ready and chunk_ids:
            self.client.delete(
                self.collection, points_selector=self.models.PointIdsList(points=chunk_ids)
            )

    def delete_library(self, library_id: int) -> None:
        if self._ready:
            self.client.delete(
                self.collection,
                points_selector=self.models.FilterSelector(filter=self._filter(library_id, None)),
            )

    def search(
        self, vector: np.ndarray, k: int, library_id: int | None = None, version: str | None = None
    ) -> list[tuple[int, float]]:
        if not self._ready:
            return []
        res = self.client.query_points(
            self.collection,
            query=np.asarray(vector, dtype=np.float32).tolist(),
            limit=k,
            query_filter=self._filter(library_id, version),
        )
        return [(int(p.id), float(p.score)) for p in res.points]


def make_store(cfg: StoreConfig, db: Database, data_dir: Path, model: str) -> VectorStore:
    if cfg.type == "qdrant":
        return QdrantVectorStore(cfg, data_dir, model)
    return SQLiteVectorStore(db, model)
