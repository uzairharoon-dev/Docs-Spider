"""URL normalization and crawl scope rules."""

from __future__ import annotations

import fnmatch
import posixpath
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

_TRACKING_KEYS = {"gclid", "fbclid", "msclkid", "mc_cid", "mc_eid", "_ga"}

SKIP_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".bmp", ".avif",
    ".pdf", ".zip", ".gz", ".tgz", ".tar", ".bz2", ".xz", ".7z", ".rar", ".whl", ".exe",
    ".dmg", ".msi", ".deb", ".rpm", ".mp3", ".mp4", ".webm", ".mov", ".avi", ".wav",
    ".css", ".js", ".mjs", ".map", ".json", ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".xml", ".rss", ".atom", ".epub", ".ipynb",
}


def _is_tracking(key: str) -> bool:
    return key.lower().startswith("utm_") or key.lower() in _TRACKING_KEYS


def normalize_url(url: str, base: str | None = None) -> str | None:
    """Return a canonical absolute http(s) URL, or ``None`` if it can't be crawled.

    Drops fragments, default ports, credentials and tracking parameters; lowercases
    scheme and host; makes an empty path ``/``.
    """
    try:
        if base:
            url = urljoin(base, url.strip())
        parts = urlsplit(url.strip())
        scheme = parts.scheme.lower()
        if scheme not in ("http", "https"):
            return None
        host = (parts.hostname or "").lower().rstrip(".")
        if not host:
            return None
        port = parts.port
    except ValueError:
        return None
    default = (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    netloc = host if port is None or default else f"{host}:{port}"
    path = parts.path or "/"
    query = urlencode(
        [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _is_tracking(k)]
    )
    return urlunsplit((scheme, netloc, path, query, ""))


def has_skipped_extension(url: str) -> bool:
    return posixpath.splitext(urlsplit(url).path)[1].lower() in SKIP_EXTENSIONS


def is_sitemap_url(url: str) -> bool:
    path = urlsplit(url).path.lower()
    return path.endswith((".xml", ".xml.gz")) and "sitemap" in path


def is_llms_txt_url(url: str) -> bool:
    return urlsplit(url).path.lower().endswith(("/llms.txt", "/llms-full.txt"))


def _matches(pattern: str, path: str) -> bool:
    if any(c in pattern for c in "*?["):
        return fnmatch.fnmatchcase(path, pattern)
    return pattern in path


def derive_prefix(url: str) -> str:
    """Path prefix a crawl seeded at ``url`` stays inside."""
    if is_sitemap_url(url) or is_llms_txt_url(url):
        return "/"
    path = urlsplit(url).path or "/"
    if path.endswith("/"):
        return path
    parent = posixpath.dirname(path)
    return parent if parent.endswith("/") else parent + "/"


@dataclass
class Scope:
    """Which URLs a crawl may visit (``in_scope``) and which it stores (``is_included``)."""

    netlocs: set[str]
    prefixes: list[str] = field(default_factory=lambda: ["/"])
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)

    @classmethod
    def from_config(
        cls,
        start_urls: list[str],
        include: list[str] | None = None,
        exclude: list[str] | None = None,
        prefixes: list[str] | None = None,
    ) -> Scope:
        normalized = [u for u in (normalize_url(s) for s in start_urls) if u]
        if not normalized:
            raise ValueError("no valid http(s) start URL")
        return cls(
            netlocs={urlsplit(u).netloc for u in normalized},
            prefixes=list(prefixes) if prefixes else sorted({derive_prefix(u) for u in normalized}),
            include=list(include or []),
            exclude=list(exclude or []),
        )

    def in_scope(self, url: str) -> bool:
        parts = urlsplit(url)
        if parts.netloc not in self.netlocs or has_skipped_extension(url):
            return False
        path = parts.path or "/"
        if not any(path.startswith(p) for p in self.prefixes):
            return False
        return not any(_matches(p, path) for p in self.exclude)

    def is_included(self, url: str) -> bool:
        if not self.include:
            return True
        path = urlsplit(url).path or "/"
        return any(_matches(p, path) for p in self.include)
