"""Find the main content of a docs page and strip navigation and chrome (F7)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urljoin

from selectolax.lexbor import LexborHTMLParser, LexborNode

# Removed from the whole document before the main content is chosen.
GLOBAL_JUNK = [
    "script", "style", "noscript", "template", "iframe", "svg", "canvas", "link", "meta",
    "form", "button", "input", "select", "textarea", "dialog",
    "img", "picture", "video", "audio", "source",
]

# Removed inside the chosen main content.
CONTENT_JUNK = [
    "nav", "aside", "footer",
    "[role=navigation]", "[role=banner]", "[role=contentinfo]", "[role=search]",
    "[aria-hidden=true]",
    # heading permalink anchors (Sphinx, MkDocs, Docusaurus, VitePress, ...)
    "a.headerlink", "a.hash-link", "a.anchor", "a.header-anchor", "a.anchor-link",
    # tables of contents, breadcrumbs, pagination, edit links
    ".toc", "#toc", ".table-of-contents", ".breadcrumb", ".breadcrumbs",
    ".pagination", ".pagination-nav", ".prev-next", ".edit-this-page", ".theme-edit-this-page",
    ".theme-doc-toc-mobile", ".theme-doc-breadcrumbs", ".theme-doc-footer",
    ".md-sidebar", ".md-header", ".md-footer", ".md-source-file", ".md-banner",
    ".sidebar", ".sphinxsidebar", ".related", ".rst-footer-buttons",
    ".skip-link", ".skip-to-content", ".announcement", ".announcement-bar",
    "#cookie-banner", "#cookie-consent", ".cookie-banner", ".cookie-consent",
    # code-block chrome
    ".linenos", ".linenodiv", ".copybtn", ".copy-button", ".code-copy",
]

# Tried in order; the largest match with enough text wins.
MAIN_CANDIDATES = [
    "article",
    "[role=main]",
    "main",
    ".md-content",
    ".theme-doc-markdown",
    "[itemprop=articleBody]",
    ".markdown-body",
    ".document",
    "#content",
    ".content",
]
MIN_MAIN_TEXT = 200

_WS = re.compile(r"\s+")


@dataclass
class CleanedPage:
    title: str
    root: LexborNode
    tree: LexborHTMLParser  # keeps the parsed document alive while ``root`` is used


def _text_len(node: LexborNode) -> int:
    return len(node.text(deep=True, strip=True))


def _decompose_all(root: LexborNode | LexborHTMLParser, selectors: list[str]) -> None:
    for sel in selectors:
        for node in root.css(sel):
            node.decompose()


def pick_main(tree: LexborHTMLParser) -> LexborNode:
    for sel in MAIN_CANDIDATES:
        nodes = tree.css(sel)
        if not nodes:
            continue
        best = max(nodes, key=_text_len)
        if _text_len(best) >= MIN_MAIN_TEXT:
            return best
    return tree.body or tree.root


def page_title(tree: LexborHTMLParser, root: LexborNode) -> str:
    h1 = root.css_first("h1") or tree.css_first("h1")
    if h1 is not None:
        text = _WS.sub(" ", h1.text(deep=True)).strip().rstrip("¶#").strip()
        if text:
            return text
    return ""


def _strip_site_suffix(title: str) -> str:
    """'Body - Fields - FastAPI' -> 'Body - Fields'."""
    for sep in (" | ", " - ", " — ", " · "):
        if sep in title:
            return title.rsplit(sep, 1)[0].strip()
    return title


def clean_html(tree: LexborHTMLParser, base_url: str) -> CleanedPage:
    """Mutates ``tree``: removes chrome and returns the main content node."""
    title_node = tree.css_first("title")
    fallback_title = _WS.sub(" ", title_node.text()).strip() if title_node is not None else ""

    _decompose_all(tree, GLOBAL_JUNK)
    root = pick_main(tree)
    if root.tag == "body" or root.tag == "html":
        _decompose_all(root, ["header", *CONTENT_JUNK])
    else:
        _decompose_all(root, CONTENT_JUNK)

    for a in root.css("a[href]"):
        href = (a.attributes.get("href") or "").strip()
        if href.startswith(("javascript:", "mailto:", "tel:")) or not href:
            a.unwrap()
        else:
            a.attrs["href"] = urljoin(base_url, href)

    title = page_title(tree, root) or _strip_site_suffix(fallback_title)
    return CleanedPage(title=title, root=root, tree=tree)
