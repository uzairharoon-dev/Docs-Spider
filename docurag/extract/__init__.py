"""Turn fetched documents into clean Markdown plus outgoing links."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urljoin

from selectolax.lexbor import LexborHTMLParser

from docurag.extract.cleaner import clean_html
from docurag.extract.markdown import to_markdown
from docurag.urls import normalize_url

_MD_LINK = re.compile(r"\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
_MD_H1 = re.compile(r"^#\s+(.+?)\s*#*\s*$", re.MULTILINE)


@dataclass
class ExtractedPage:
    title: str
    markdown: str
    links: list[str] = field(default_factory=list)


def _dedupe(urls: list[str]) -> list[str]:
    return list(dict.fromkeys(urls))


def extract_links(tree: LexborHTMLParser, base_url: str) -> list[str]:
    base = tree.css_first("base[href]")
    if base is not None:
        base_url = urljoin(base_url, base.attributes.get("href") or "")
    out = []
    for a in tree.css("a[href]"):
        rel = (a.attributes.get("rel") or "").lower()
        if "nofollow" in rel:
            continue
        href = (a.attributes.get("href") or "").strip()
        if not href or href.startswith("#"):  # same-page anchors
            continue
        url = normalize_url(href, base_url)
        if url:
            out.append(url)
    return _dedupe(out)


def extract_html(html: str, url: str) -> ExtractedPage:
    tree = LexborHTMLParser(html)
    links = extract_links(tree, url)  # before cleaning: nav menus are how we discover pages
    cleaned = clean_html(tree, url)
    return ExtractedPage(title=cleaned.title, markdown=to_markdown(cleaned.root), links=links)


def markdown_links(text: str, base_url: str) -> list[str]:
    return _dedupe([u for u in (normalize_url(m, base_url) for m in _MD_LINK.findall(text)) if u])


def extract_markdown(text: str, url: str) -> ExtractedPage:
    m = _MD_H1.search(text)
    md = re.sub(r"\n{3,}", "\n\n", text.replace("\r\n", "\n")).strip() + "\n"
    return ExtractedPage(title=m.group(1) if m else "", markdown=md, links=markdown_links(md, url))
