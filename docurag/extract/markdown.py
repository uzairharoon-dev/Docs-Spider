"""HTML to Markdown that keeps headings, tables and tagged code blocks intact (F7, F8)."""

from __future__ import annotations

import re

from markdownify import markdownify
from selectolax.lexbor import LexborNode

_LANG_PREFIXES = ("language-", "lang-", "highlight-source-", "highlight-", "brush:")
_LANG_IGNORE = {"default", "none", "plain", "highlight", "notranslate", "nohighlight"}
_LANG_ALIASES = {
    "py": "python", "python3": "python", "py3": "python", "pycon": "python",
    "js": "javascript", "ts": "typescript", "sh": "bash", "shell": "bash", "console": "bash",
    "shell-session": "bash", "yml": "yaml", "plaintext": "text", "txt": "text",
}
_TOKEN = "DOCURAGCODEBLOCK{}X"
_TOKEN_RE = re.compile(r"DOCURAGCODEBLOCK(\d+)X")


def _lang_from(node: LexborNode | None) -> str | None:
    if node is None:
        return None
    attrs = node.attributes
    for key in ("data-lang", "data-language"):
        if attrs.get(key):
            return attrs[key]
    for cls in (attrs.get("class") or "").split():
        for prefix in _LANG_PREFIXES:
            if cls.startswith(prefix) and len(cls) > len(prefix):
                return cls[len(prefix):]
    return None


def code_language(pre: LexborNode) -> str:
    """Language tag for a ``<pre>``: checks pre, its ``<code>``, then up to 3 ancestors."""
    candidates: list[LexborNode | None] = [pre, pre.css_first("code")]
    parent = pre.parent
    for _ in range(3):
        if parent is None:
            break
        candidates.append(parent)
        parent = parent.parent
    for node in candidates:
        lang = _lang_from(node)
        if lang:
            lang = lang.strip().lower()
            if lang in _LANG_IGNORE:
                continue
            return _LANG_ALIASES.get(lang, lang)
    return ""


def _code_text(pre: LexborNode) -> str:
    for br in pre.css("br"):
        br.replace_with("\n")
    text = pre.text(deep=True, strip=False)
    return text.strip("\n").rstrip()


def _fence(lang: str, code: str) -> str:
    longest = max((len(m) for m in re.findall(r"`{3,}", code)), default=2)
    ticks = "`" * max(3, longest + 1)
    return f"\n\n{ticks}{lang}\n{code}\n{ticks}\n\n"


def _block_owner(pre: LexborNode) -> LexborNode:
    """Pygments line-number tables wrap ``<pre>``; replace the whole table."""
    node = pre.parent
    for _ in range(5):
        if node is None:
            break
        if node.tag == "table" and "highlighttable" in (node.attributes.get("class") or ""):
            return node
        node = node.parent
    return pre


def to_markdown(root: LexborNode) -> str:
    blocks: list[str] = []
    for pre in root.css("pre"):
        if pre.parent is None:  # already removed with an earlier owner
            continue
        lang, code = code_language(pre), _code_text(pre)
        blocks.append(_fence(lang, code))
        _block_owner(pre).replace_with(_TOKEN.format(len(blocks) - 1))

    md = markdownify(
        root.html or "",
        heading_style="ATX",
        bullets="-",
        escape_underscores=False,
        escape_asterisks=False,
        strip=["img"],
    )
    md = "\n".join(line.rstrip() for line in md.splitlines())
    md = _TOKEN_RE.sub(lambda m: blocks[int(m.group(1))], md)
    md = re.sub(r"\n{3,}", "\n\n", md)
    return md.strip() + "\n"
