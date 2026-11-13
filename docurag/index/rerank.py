"""Optional cross-encoder reranking (F15).

A cross-encoder reads the query and a candidate together, so it judges relevance
better than comparing two independent embeddings, but it costs one model pass per
candidate. It therefore only re-orders the top few fused candidates.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol


class Reranker(Protocol):
    name: str

    def score(self, query: str, documents: list[str]) -> list[float]: ...


class FastEmbedReranker:
    def __init__(self, model: str, cache_dir: Path, batch_size: int = 16):
        os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
        from fastembed.rerank.cross_encoder import TextCrossEncoder

        cache_dir.mkdir(parents=True, exist_ok=True)
        self._model = TextCrossEncoder(model_name=model, cache_dir=str(cache_dir))
        self.name = f"local:{model}"
        self.batch_size = batch_size

    def score(self, query: str, documents: list[str]) -> list[float]:
        if not documents:
            return []
        return [float(s) for s in self._model.rerank(query, documents, batch_size=self.batch_size)]
