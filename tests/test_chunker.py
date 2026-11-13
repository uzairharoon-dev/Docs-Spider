import pytest

from docurag.index.chunker import (
    chunk_markdown,
    count_tokens,
    normalize_heading,
    parse_blocks,
)

PROSE = "OAuth2 lets an application act on behalf of a user without their password. "


def doc() -> str:
    return (
        "# Security\n\nIntro to security.\n\n"
        "## OAuth2\n\n" + PROSE * 60 + "\n\n"
        "```python\n" + "token = get_token()\n" * 200 + "```\n\n"
        "### Scopes\n\nScopes limit what a token can do.\n\n"
        "| scope | meaning |\n| --- | --- |\n| read | read items |\n| write | change items |\n"
    )


ANCHORS = [("Security", "security"), ("OAuth2", "oauth2"), ("Scopes", "scopes")]


def test_parse_blocks_keeps_fences_whole():
    blocks = parse_blocks("# T\n\n```md\n# not a heading\n\nstill code\n```\n\nafter")
    kinds = [b.kind for b in blocks]
    assert kinds == ["heading", "code", "text"]
    assert "# not a heading" in blocks[1].text and "still code" in blocks[1].text


def test_parse_blocks_unclosed_fence_runs_to_end():
    blocks = parse_blocks("text\n\n```\ncode forever\n# x")
    assert [b.kind for b in blocks] == ["text", "code"]


def test_sizes_and_heading_paths():
    chunks = chunk_markdown(doc(), anchors=ANCHORS, min_tokens=100, max_tokens=300)
    paths = [c.heading_path for c in chunks]
    assert paths[0][:1] == ["Security"]
    assert ["Security", "OAuth2"] in paths
    assert paths[-1] == ["Security", "OAuth2", "Scopes"]
    for c in chunks:
        if "```" not in c.text:
            assert c.token_count <= 300 * 1.1
    assert chunks[-1].anchor == "scopes"


def test_code_block_never_split():
    chunks = chunk_markdown(doc(), min_tokens=100, max_tokens=300)
    code_chunks = [c for c in chunks if "```python" in c.text]
    assert len(code_chunks) == 1
    assert code_chunks[0].text.count("token = get_token()") == 200
    assert code_chunks[0].text.count("```") == 2
    for c in chunks:
        assert c.text.count("```") % 2 == 0  # every chunk has balanced fences


def test_overlap_between_split_windows():
    md = "## Long\n\n" + " ".join(f"Sentence number {i} explains a detail." for i in range(300))
    chunks = chunk_markdown(md, min_tokens=50, max_tokens=200, overlap=0.15)
    assert len(chunks) > 2
    for a, b in zip(chunks, chunks[1:], strict=False):
        tail = a.text.split(". ")[-2]  # a full sentence near the end of the previous chunk
        assert tail in b.text
    assert all(c.heading_path == ["Long"] for c in chunks)


def test_heading_stays_with_its_body():
    md = "# A\n\nshort.\n\n## B\n\n" + PROSE * 40
    chunks = chunk_markdown(md, min_tokens=50, max_tokens=200)
    assert not any(c.text.rstrip().endswith("## B") for c in chunks)


def test_small_sections_merge_under_common_parent():
    md = "# Guide\n\n## A\n\nalpha text.\n\n## B\n\nbeta text.\n\n## C\n\ngamma text."
    chunks = chunk_markdown(md, anchors=[("Guide", "guide")], min_tokens=300, max_tokens=512)
    assert len(chunks) == 1
    assert chunks[0].heading_path == ["Guide"]
    assert chunks[0].anchor == "guide"
    assert "## B" in chunks[0].text and "gamma" in chunks[0].text


def test_large_table_split_repeats_header():
    rows = "".join(f"| r{i} | value {i} |\n" for i in range(200))
    md = "## T\n\n| a | b |\n| --- | --- |\n" + rows
    chunks = chunk_markdown(md, min_tokens=50, max_tokens=150)
    assert len(chunks) > 1
    assert all("| a | b |\n| --- | --- |" in c.text for c in chunks)


def test_anchor_matching_ignores_markdown_and_order():
    md = "# Page\n\n## Use `Depends`\n\nx " * 1 + "\n\n## [Link](http://a) title\n\ny"
    anchors = [("Page", "p"), ("Use Depends", "use-depends"), ("Link title", "link-title")]
    chunks = chunk_markdown(md, anchors=anchors, min_tokens=1, max_tokens=8)
    assert {c.anchor for c in chunks} >= {"use-depends", "link-title"}
    assert normalize_heading("Use `Depends()`") == "use depends"


def test_no_anchor_falls_back_to_ancestor_or_none():
    chunks = chunk_markdown("# Top\n\n## Sub\n\n" + PROSE * 5, anchors=[("Top", "top")],
                            min_tokens=1, max_tokens=60)
    assert all(c.anchor == "top" for c in chunks)
    assert chunk_markdown("plain text only", min_tokens=1)[0].anchor is None


def test_empty_and_invalid():
    assert chunk_markdown("") == []
    with pytest.raises(ValueError):
        chunk_markdown("x", min_tokens=500, max_tokens=100)


def test_count_tokens():
    assert count_tokens("def f(x): return x") == 8
