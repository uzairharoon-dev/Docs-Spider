"""Pluggable text embedders (F13).

* ``local``  - fastembed (ONNX on CPU, no torch). Default: BAAI/bge-small-en-v1.5.
* ``openai`` - any OpenAI-compatible ``/embeddings`` endpoint (OpenAI, Azure, vLLM, ...).
* ``hash``   - feature hashing; no model download. For tests and offline smoke runs only.

All embedders return L2-normalized float32 vectors, so dot product = cosine.
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Protocol

import httpx
import numpy as np

from docurag.config import EmbedderConfig

BGE_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "
_TOKEN = re.compile(r"\w+")


class Embedder(Protocol):
    name: str  # stable id stored with vectors, e.g. "local:BAAI/bge-small-en-v1.5"

    @property
    def dim(self) -> int: ...

    def embed_documents(self, texts: list[str]) -> np.ndarray: ...

    def embed_query(self, text: str) -> np.ndarray: ...


def embedder_name(cfg: EmbedderConfig) -> str:
    """Stable id stored with every vector; changing it triggers a re-index."""
    if cfg.provider == "hash":
        return f"hash:{cfg.dim or 384}"
    if cfg.provider == "openai":
        return f"openai:{cfg.model}" + (f":{cfg.dim}" if cfg.dim else "")
    return f"local:{cfg.model}"


def _normalize(m: np.ndarray) -> np.ndarray:
    m = np.asarray(m, dtype=np.float32)
    if m.ndim == 1:
        m = m[None, :]
    norms = np.linalg.norm(m, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return m / norms


class HashEmbedder:
    """Signed feature hashing of word unigrams and bigrams. Lexical, not semantic."""

    def __init__(self, dim: int = 384):
        self._dim = dim
        self.name = f"hash:{dim}"

    @property
    def dim(self) -> int:
        return self._dim

    def _one(self, text: str) -> np.ndarray:
        v = np.zeros(self._dim, dtype=np.float32)
        words = _TOKEN.findall(text.lower())
        for feat in words + [f"{a} {b}" for a, b in zip(words, words[1:], strict=False)]:
            h = int.from_bytes(hashlib.blake2b(feat.encode(), digest_size=8).digest(), "big")
            v[h % self._dim] += 1.0 if (h >> 63) & 1 else -1.0
        return v

    def embed_documents(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self._dim), dtype=np.float32)
        return _normalize(np.stack([self._one(t) for t in texts]))

    def embed_query(self, text: str) -> np.ndarray:
        return self.embed_documents([text])[0]


class FastEmbedEmbedder:
    def __init__(self, cfg: EmbedderConfig, cache_dir: Path):
        # Windows without Developer Mode can't symlink; HF falls back to copies and warns.
        os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
        try:
            from fastembed import TextEmbedding
        except ImportError as exc:  # pragma: no cover - fastembed is a core dependency
            raise RuntimeError("provider 'local' needs fastembed: pip install fastembed") from exc
        cache_dir.mkdir(parents=True, exist_ok=True)
        self._model = TextEmbedding(
            model_name=cfg.model, cache_dir=str(cache_dir), enable_cpu_mem_arena=cfg.cpu_mem_arena
        )
        self.name = embedder_name(cfg)
        self.batch_size = cfg.batch_size
        is_bge = "bge" in cfg.model.lower() and "-m3" not in cfg.model.lower()
        self.query_prefix = cfg.query_prefix if cfg.query_prefix is not None else (
            BGE_QUERY_PREFIX if is_bge else ""
        )
        self._dim: int | None = None

    @property
    def dim(self) -> int:
        if self._dim is None:
            self._dim = int(self.embed_documents(["dimension probe"]).shape[1])
        return self._dim

    def embed_documents(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self._dim or 0), dtype=np.float32)
        out = _normalize(np.stack(list(self._model.embed(texts, batch_size=self.batch_size))))
        self._dim = out.shape[1]
        return out

    def embed_query(self, text: str) -> np.ndarray:
        return self.embed_documents([self.query_prefix + text])[0]


class OpenAIEmbedder:
    def __init__(self, cfg: EmbedderConfig, client: httpx.Client | None = None):
        key = os.environ.get(cfg.api_key_env)
        if not key:
            raise RuntimeError(f"provider 'openai' needs an API key in ${cfg.api_key_env}")
        self.model = cfg.model
        self.requested_dim = cfg.dim
        self.batch_size = cfg.batch_size
        self.name = embedder_name(cfg)
        self._client = client or httpx.Client(timeout=60)
        self._url = cfg.base_url.rstrip("/") + "/embeddings"
        self._headers = {"Authorization": f"Bearer {key}"}
        self._dim: int | None = cfg.dim

    @property
    def dim(self) -> int:
        if self._dim is None:
            self._dim = int(self.embed_documents(["dimension probe"]).shape[1])
        return self._dim

    def _request(self, batch: list[str]) -> list[list[float]]:
        body: dict = {"model": self.model, "input": batch}
        if self.requested_dim:
            body["dimensions"] = self.requested_dim
        resp = self._client.post(self._url, json=body, headers=self._headers)
        if resp.status_code >= 400:
            raise RuntimeError(f"embedding API error {resp.status_code}: {resp.text[:300]}")
        data = sorted(resp.json()["data"], key=lambda d: d["index"])
        return [d["embedding"] for d in data]

    def embed_documents(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self._dim or 0), dtype=np.float32)
        rows: list[list[float]] = []
        for i in range(0, len(texts), self.batch_size):
            rows.extend(self._request(texts[i : i + self.batch_size]))
        out = _normalize(np.array(rows, dtype=np.float32))
        self._dim = out.shape[1]
        return out

    def embed_query(self, text: str) -> np.ndarray:
        return self.embed_documents([text])[0]


def make_embedder(cfg: EmbedderConfig, data_dir: Path) -> Embedder:
    if cfg.provider == "hash":
        return HashEmbedder(cfg.dim or 384)
    if cfg.provider == "openai":
        return OpenAIEmbedder(cfg)
    return FastEmbedEmbedder(cfg, cfg.cache_dir or data_dir / "models")
