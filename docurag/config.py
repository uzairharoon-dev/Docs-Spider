"""Configuration models and loading of ``docurag.yaml``."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from docurag import USER_AGENT

DEFAULT_CONFIG_FILE = "docurag.yaml"


def default_data_dir() -> Path:
    env = os.environ.get("DOCURAG_HOME")
    return Path(env).expanduser() if env else Path.home() / ".docurag"


class LibraryConfig(BaseModel):
    """One documentation source to crawl."""

    model_config = ConfigDict(extra="forbid")

    name: str
    start_urls: list[str] = Field(min_length=1)
    include: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)
    # Path prefixes the crawl is confined to. Derived from start_urls when empty.
    scope_prefixes: list[str] = Field(default_factory=list)
    # False: plain HTTP. True: render every page in Chromium. "auto": render only pages
    # whose static HTML looks like a JavaScript shell (needs the `js` extra).
    render_js: bool | Literal["auto"] = False
    rate_limit: float = Field(default=2.0, gt=0)
    max_pages: int | None = Field(default=None, gt=0)
    max_depth: int = Field(default=20, ge=0)
    discover_sitemaps: bool = True
    # Skip pages whose sitemap <lastmod> predates our last fetch. Turn off (or run
    # `docurag update --full`) for sites whose sitemaps carry stale dates.
    trust_sitemap_lastmod: bool = True
    # Label for pages with no version in their URL; an explicit value labels every page.
    version: str = "latest"
    # "auto": read the version from URLs like /0.115/ or /en/stable/ when `version` is
    # left at "latest". `version_pattern` (regex with one group) replaces the heuristic.
    version_detection: Literal["auto", "off"] = "auto"
    version_pattern: str | None = None

    @field_validator("version_pattern")
    @classmethod
    def _pattern_ok(cls, v: str | None) -> str | None:
        if v is not None:
            from docurag.crawler.versions import compile_pattern

            compile_pattern(v)
        return v

    @field_validator("name")
    @classmethod
    def _name_ok(cls, v: str) -> str:
        v = v.strip()
        if not v or any(c.isspace() for c in v):
            raise ValueError("library name must be non-empty and contain no whitespace")
        return v


class EmbedderConfig(BaseModel):
    """Which embedding model turns chunks into vectors (F13)."""

    model_config = ConfigDict(extra="forbid")

    # local: fastembed (ONNX, CPU). openai: any OpenAI-compatible /embeddings API.
    # hash: dependency-free feature hashing, for tests and offline smoke runs.
    provider: Literal["local", "openai", "hash"] = "local"
    model: str = "BAAI/bge-small-en-v1.5"
    batch_size: int = Field(default=32, ge=1, le=2048)
    base_url: str = "https://api.openai.com/v1"
    api_key_env: str = "OPENAI_API_KEY"
    dim: int | None = Field(default=None, ge=1)  # hash provider size / openai `dimensions`
    query_prefix: str | None = None  # default: BGE retrieval instruction for BGE models
    cache_dir: Path | None = None  # local model files; default <data_dir>/models


class StoreConfig(BaseModel):
    """Where vectors live. ``sqlite`` needs no server; ``qdrant`` scales further."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["sqlite", "qdrant"] = "sqlite"
    url: str | None = None  # qdrant server, e.g. http://localhost:6333
    path: Path | None = None  # qdrant embedded mode; default <data_dir>/qdrant
    api_key_env: str = "QDRANT_API_KEY"
    collection_prefix: str = "docurag"


class ChunkConfig(BaseModel):
    """Heading-aware chunking (F10). Sizes are estimated tokens."""

    model_config = ConfigDict(extra="forbid")

    min_tokens: int = Field(default=300, ge=16)
    # 512 stays inside the spec's 300-800 range and matches bge-small's input limit.
    max_tokens: int = Field(default=512, ge=32)
    overlap: float = Field(default=0.12, ge=0, le=0.5)


class SearchConfig(BaseModel):
    """Retrieval defaults (F14, F15). CLI flags override them per query."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["vector", "bm25", "hybrid"] = "hybrid"
    candidates: int = Field(default=50, ge=1, le=1000)  # per retriever, before fusion
    rrf_k: int = Field(default=60, ge=1)  # reciprocal rank fusion constant
    rerank: bool = False
    reranker_model: str = "Xenova/ms-marco-MiniLM-L-6-v2"
    rerank_candidates: int = Field(default=20, ge=1, le=200)


class Settings(BaseModel):
    """Top-level settings. Unknown sections are kept for later milestones."""

    model_config = ConfigDict(extra="allow")

    data_dir: Path = Field(default_factory=default_data_dir)
    concurrency: int = Field(default=16, ge=1, le=256)
    user_agent: str = USER_AGENT
    request_timeout: float = Field(default=20.0, gt=0)
    max_retries: int = Field(default=3, ge=0)
    max_page_bytes: int = Field(default=5_000_000, gt=0)
    libraries: list[LibraryConfig] = Field(default_factory=list)
    embedder: EmbedderConfig = Field(default_factory=EmbedderConfig)
    store: StoreConfig = Field(default_factory=StoreConfig)
    chunking: ChunkConfig = Field(default_factory=ChunkConfig)
    search: SearchConfig = Field(default_factory=SearchConfig)

    @field_validator("data_dir", mode="before")
    @classmethod
    def _expand(cls, v: object) -> object:
        return Path(v).expanduser() if isinstance(v, str | Path) else v

    @property
    def db_path(self) -> Path:
        return self.data_dir / "docurag.db"

    def library(self, name: str) -> LibraryConfig | None:
        return next((lib for lib in self.libraries if lib.name == name), None)


def load_settings(path: Path | None = None) -> Settings:
    """Load settings from ``path``, or ``./docurag.yaml`` if present, else defaults."""
    if path is None:
        candidate = Path.cwd() / DEFAULT_CONFIG_FILE
        if not candidate.exists():
            return Settings()
        path = candidate
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: top level must be a mapping")
    return Settings.model_validate(data)
