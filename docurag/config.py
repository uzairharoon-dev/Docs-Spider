"""Configuration models and loading of ``docurag.yaml``."""

from __future__ import annotations

import os
from pathlib import Path

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
    render_js: bool = False
    rate_limit: float = Field(default=2.0, gt=0)
    max_pages: int | None = Field(default=None, gt=0)
    max_depth: int = Field(default=20, ge=0)
    discover_sitemaps: bool = True
    version: str = "latest"

    @field_validator("name")
    @classmethod
    def _name_ok(cls, v: str) -> str:
        v = v.strip()
        if not v or any(c.isspace() for c in v):
            raise ValueError("library name must be non-empty and contain no whitespace")
        return v


class Settings(BaseModel):
    """Top-level settings. Unknown sections (embedder, store, ...) are kept for later milestones."""

    model_config = ConfigDict(extra="allow")

    data_dir: Path = Field(default_factory=default_data_dir)
    concurrency: int = Field(default=16, ge=1, le=256)
    user_agent: str = USER_AGENT
    request_timeout: float = Field(default=20.0, gt=0)
    max_retries: int = Field(default=3, ge=0)
    max_page_bytes: int = Field(default=5_000_000, gt=0)
    libraries: list[LibraryConfig] = Field(default_factory=list)

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
