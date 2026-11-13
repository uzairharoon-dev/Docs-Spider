"""Heading-aware Markdown chunking (F10, F11, F12).

1. Parse Markdown into blocks: headings, paragraphs, tables and fenced code.
2. Group blocks into sections, one per heading, each with its heading path
   (e.g. ``["Security", "OAuth2", "Scopes"]``) and the heading's HTML anchor.
3. A section that fits in ``max_tokens`` is one unit. A larger one is split on
   block boundaries with ~``overlap`` of trailing prose repeated in the next
   window. Code blocks are never split, even when they exceed ``max_tokens``.
4. Neighbouring units below ``min_tokens`` are merged while they fit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_TOKEN = re.compile(r"\w+|[^\w\s]")
_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})(.*)$")
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_SENTENCE = re.compile(r"(?<=[.!?:;])\s+")
_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_NON_WORD = re.compile(r"[\W_]+")


def count_tokens(text: str) -> int:
    """Cheap tokenizer-free estimate: words plus punctuation marks.

    Tracks WordPiece/BPE counts closely enough for sizing chunks, and avoids
    loading a tokenizer just to split text.
    """
    return len(_TOKEN.findall(text))


def context_line(title: str | None, heading_path: list[str]) -> str:
    """'Page title > Section > Subsection', without repeating the title."""
    parts = [title] if title else []
    for h in heading_path:
        if not parts or parts[-1] != h:
            parts.append(h)
    return " > ".join(parts)


def normalize_heading(text: str) -> str:
    text = _MD_LINK.sub(r"\1", text)
    return _NON_WORD.sub(" ", text).strip().lower()


@dataclass
class Block:
    kind: str  # heading | text | table | code
    text: str
    level: int = 0
    tokens: int = 0

    def __post_init__(self) -> None:
        if not self.tokens:
            self.tokens = count_tokens(self.text)


@dataclass
class Chunk:
    text: str
    heading_path: list[str]
    anchor: str | None
    token_count: int

    def as_dict(self) -> dict:
        return {
            "text": self.text,
            "heading_path": self.heading_path,
            "anchor": self.anchor,
            "token_count": self.token_count,
        }


@dataclass
class _Unit:
    blocks: list[Block]
    path: list[str]
    anchors: list[str | None]
    tokens: int = field(init=False)

    def __post_init__(self) -> None:
        self.tokens = sum(b.tokens for b in self.blocks)


# ---------------------------------------------------------------- parsing
def parse_blocks(markdown: str) -> list[Block]:
    blocks: list[Block] = []
    buf: list[str] = []
    buf_kind = "text"

    def flush() -> None:
        nonlocal buf, buf_kind
        text = "\n".join(buf).strip("\n")
        if text.strip():
            blocks.append(Block(buf_kind, text))
        buf, buf_kind = [], "text"

    lines = markdown.replace("\r\n", "\n").split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        fence = _FENCE.match(line)
        if fence:
            flush()
            marker = fence.group(1)
            code = [line]
            i += 1
            while i < len(lines):
                code.append(lines[i])
                close = _FENCE.match(lines[i])
                if (
                    close
                    and close.group(1)[0] == marker[0]
                    and len(close.group(1)) >= len(marker)
                    and not close.group(2).strip()
                ):
                    break
                i += 1
            blocks.append(Block("code", "\n".join(code)))
            i += 1
            continue
        heading = _HEADING.match(line)
        if heading:
            flush()
            blocks.append(Block("heading", line.strip(), level=len(heading.group(1))))
        elif not line.strip():
            flush()
        elif line.lstrip().startswith("|"):
            if buf_kind != "table":
                flush()
                buf_kind = "table"
            buf.append(line)
        else:
            if buf_kind == "table":
                flush()
            buf.append(line)
        i += 1
    flush()
    return blocks


def _heading_text(block: Block) -> str:
    m = _HEADING.match(block.text)
    return m.group(2).strip() if m else block.text


class _AnchorResolver:
    """Matches Markdown headings to HTML ids in document order."""

    def __init__(self, anchors: list[tuple[str, str]] | None):
        self.items = [(normalize_heading(t), a) for t, a in (anchors or [])]
        self.pos = 0

    def resolve(self, heading: str) -> str | None:
        key = normalize_heading(heading)
        for j in range(self.pos, len(self.items)):
            if self.items[j][0] == key:
                self.pos = j + 1
                return self.items[j][1]
        return None


def sections(blocks: list[Block], anchors: list[tuple[str, str]] | None) -> list[_Unit]:
    resolver = _AnchorResolver(anchors)
    stack: list[tuple[int, str, str | None]] = []
    out: list[_Unit] = []
    current: list[Block] = []

    def close() -> None:
        if current:
            out.append(_Unit(list(current), [t for _, t, _ in stack], [a for _, _, a in stack]))

    for b in blocks:
        if b.kind == "heading":
            close()
            current = []
            while stack and stack[-1][0] >= b.level:
                stack.pop()
            text = _heading_text(b)
            stack.append((b.level, text, resolver.resolve(text)))
        current.append(b)
    close()
    return out


# ---------------------------------------------------------------- splitting
def _group(pieces: list[str], sep: str, max_tokens: int) -> list[str]:
    out: list[str] = []
    cur: list[str] = []
    cur_t = 0
    for p in pieces:
        t = count_tokens(p)
        if cur and cur_t + t > max_tokens:
            out.append(sep.join(cur))
            cur, cur_t = [], 0
        cur.append(p)
        cur_t += t
    if cur:
        out.append(sep.join(cur))
    return out


def _split_text(text: str, max_tokens: int) -> list[str]:
    """Split oversized prose by lines, then sentences, then words."""
    for sep, pieces in (
        ("\n", text.split("\n")),
        (" ", _SENTENCE.split(text)),
        (" ", text.split(" ")),
    ):
        if len(pieces) < 2:
            continue
        out: list[str] = []
        for g in _group(pieces, sep, max_tokens):
            # a group is only oversized when one piece is; split that piece more finely
            out.extend(_split_text(g, max_tokens) if count_tokens(g) > max_tokens else [g])
        return out
    return [text]


def _split_table(text: str, max_tokens: int) -> list[str]:
    """Split a table by rows, repeating the header and separator rows."""
    rows = text.split("\n")
    head, body = rows[:2], rows[2:]
    head_t = count_tokens("\n".join(head))
    out, cur, cur_t = [], [], head_t
    for r in body:
        t = count_tokens(r)
        if cur and cur_t + t > max_tokens:
            out.append("\n".join(head + cur))
            cur, cur_t = [], head_t
        cur.append(r)
        cur_t += t
    if cur:
        out.append("\n".join(head + cur))
    return out or [text]


def _expand(blocks: list[Block], max_tokens: int) -> list[Block]:
    out = []
    for b in blocks:
        if b.tokens <= max_tokens or b.kind in ("code", "heading"):
            out.append(b)
        elif b.kind == "table":
            out.extend(Block("table", t) for t in _split_table(b.text, max_tokens))
        else:
            out.extend(Block("text", t) for t in _split_text(b.text, max_tokens))
    return out


def _overlap_tail(blocks: list[Block], budget: int) -> list[Block]:
    """Trailing prose (never code or headings) of at most ``budget`` tokens."""
    tail: list[Block] = []
    used = 0
    for b in reversed(blocks):
        if b.kind not in ("text", "table"):
            break
        if used + b.tokens <= budget:
            tail.insert(0, b)
            used += b.tokens
            continue
        if b.kind == "text" and not tail:  # take the last sentences of a long paragraph
            sentences = _SENTENCE.split(b.text)
            picked: list[str] = []
            for s in reversed(sentences):
                t = count_tokens(s)
                if used + t > budget:
                    break
                picked.insert(0, s)
                used += t
            if picked:
                tail.insert(0, Block("text", " ".join(picked)))
        break
    return tail


def _split_unit(unit: _Unit, max_tokens: int, overlap: float) -> list[_Unit]:
    if unit.tokens <= max_tokens:
        return [unit]
    budget = int(max_tokens * overlap)
    windows: list[list[Block]] = []
    cur: list[Block] = []
    cur_t = 0
    # Pieces leave room for the overlap, or it would never fit in the next window.
    for b in _expand(unit.blocks, max(1, max_tokens - budget)):
        only_headings = all(x.kind == "heading" for x in cur)
        if cur and not only_headings and cur_t + b.tokens > max_tokens:
            windows.append(cur)
            cur = _overlap_tail(cur, budget)
            cur_t = sum(x.tokens for x in cur)
            if cur_t + b.tokens > max_tokens:
                cur, cur_t = [], 0
        cur.append(b)
        cur_t += b.tokens
    if cur:
        windows.append(cur)
    return [_Unit(w, unit.path, unit.anchors) for w in windows]


def _common_prefix(a: _Unit, b: _Unit) -> tuple[list[str], list[str | None]]:
    n = 0
    while n < min(len(a.path), len(b.path)) and a.path[n] == b.path[n]:
        n += 1
    if n == 0:  # unrelated paths: keep the first non-empty one
        first = a if a.path else b
        return first.path, first.anchors
    return a.path[:n], a.anchors[:n]


def _merge(a: _Unit, b: _Unit, max_tokens: int) -> _Unit:
    single_section = sum(x.kind == "heading" for x in a.blocks) <= 1
    if single_section and a.tokens <= max_tokens // 10 and b.path[: len(a.path)] == a.path:
        # "# Title" + one line, then its first subsection: cite the subsection
        return _Unit(a.blocks + b.blocks, b.path, b.anchors)
    path, anchors = _common_prefix(a, b)
    return _Unit(a.blocks + b.blocks, path, anchors)


def _fits(a: _Unit, b: _Unit, max_tokens: int) -> bool:
    total = a.tokens + b.tokens
    if total <= max_tokens:
        return True
    # A fragment (heading plus a line or two) is noise on its own; allow 10% slack for it.
    fragment = min(a.tokens, b.tokens) <= max_tokens // 10
    return fragment and total <= max_tokens * 1.1


def _merge_small(units: list[_Unit], min_tokens: int, max_tokens: int) -> list[_Unit]:
    out: list[_Unit] = []
    for u in units:
        if out and out[-1].tokens < min_tokens and _fits(out[-1], u, max_tokens):
            out[-1] = _merge(out[-1], u, max_tokens)
        else:
            out.append(u)
    # a small trailing unit joins its predecessor when it fits
    if len(out) > 1 and out[-1].tokens < min_tokens and _fits(out[-2], out[-1], max_tokens):
        out[-2:] = [_merge(out[-2], out[-1], max_tokens)]
    return out


def chunk_markdown(
    markdown: str,
    *,
    anchors: list[tuple[str, str]] | None = None,
    min_tokens: int = 300,
    max_tokens: int = 512,
    overlap: float = 0.12,
) -> list[Chunk]:
    if min_tokens > max_tokens:
        raise ValueError("min_tokens must be <= max_tokens")
    units: list[_Unit] = []
    for section in sections(parse_blocks(markdown), anchors):
        units.extend(_split_unit(section, max_tokens, overlap))
    units = _merge_small(units, min_tokens, max_tokens)
    chunks = []
    for u in units:
        text = "\n\n".join(b.text for b in u.blocks).strip()
        if not text:
            continue
        # deepest heading with a known id; an ancestor section is still a valid citation
        anchor = next((a for a in reversed(u.anchors) if a), None)
        chunks.append(Chunk(text, list(u.path), anchor, count_tokens(text)))
    return chunks
