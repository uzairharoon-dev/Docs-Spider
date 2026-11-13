from selectolax.lexbor import LexborHTMLParser

from docurag.extract import extract_html, extract_markdown
from docurag.extract.dedup import DedupIndex, content_hash, hamming, simhash
from docurag.extract.markdown import code_language
from tests.conftest import LOREM, page


def test_strips_chrome_keeps_content():
    html = page("Auth", f"<p>{LOREM}</p><aside>Sidebar junk</aside>", ["/a", "/b"])
    out = extract_html(html, "https://docs.test/guide/")
    assert out.title == "Auth"
    assert out.markdown.startswith("# Auth\n")
    for junk in ("Site header", "Copyright", "Sidebar junk", "¶"):
        assert junk not in out.markdown
    assert "dependency injection" in out.markdown
    # nav links are still discovered even though nav is stripped from the text
    assert out.links == ["https://docs.test/a", "https://docs.test/b"]


def test_code_blocks_keep_language_and_whitespace():
    html = page(
        "Code",
        f"<p>{LOREM}</p>"
        '<div class="highlight-python notranslate"><div class="highlight"><pre>'
        "<span>def</span> f():\n    return {'a': 1}\n</pre></div></div>"
        '<pre class="prism-code language-tsx"><code>'
        '<span class="token-line">const a = 1;<br></span><span class="token-line">'
        "a_b * c<br></span></code></pre>"
        "<pre><code>no language</code></pre>",
    )
    md = extract_html(html, "https://docs.test/").markdown
    assert "```python\ndef f():\n    return {'a': 1}\n```" in md
    assert "```tsx\nconst a = 1;\na_b * c\n```" in md
    assert "```\nno language\n```" in md


def test_code_fence_longer_than_inner_backticks():
    html = page("Fence", f"<p>{LOREM}</p><pre class='language-md'>```py\nx\n```</pre>")
    md = extract_html(html, "https://docs.test/").markdown
    assert "````md\n```py\nx\n```\n````" in md


def test_tables_and_links_are_absolute():
    html = page(
        "Tbl",
        f"<p>{LOREM} <a href='../ref/#x'>ref</a></p>"
        "<table><thead><tr><th>Param</th><th>Type</th></tr></thead>"
        "<tbody><tr><td>q</td><td>str</td></tr></tbody></table>",
    )
    md = extract_html(html, "https://docs.test/guide/a/").markdown
    assert "| Param | Type |" in md and "| q | str |" in md
    assert "[ref](https://docs.test/guide/ref/#x)" in md


def test_code_language_aliases():
    tree = LexborHTMLParser('<div class="language-py3"><pre>x</pre></div>')
    assert code_language(tree.css_first("pre")) == "python"


def test_extract_markdown_document():
    out = extract_markdown("# Title\n\nSee [b](./b.md) and [c](https://x.test/c).\n", "https://x.test/a/")
    assert out.title == "Title"
    assert out.links == ["https://x.test/a/b.md", "https://x.test/c"]


def test_simhash_near_duplicates():
    a = LOREM * 5
    b = LOREM * 5 + " Updated footer line."
    c = "Completely different text about Kubernetes ingress controllers and TLS. " * 5
    assert hamming(simhash(a), simhash(b)) <= 3
    assert hamming(simhash(a), simhash(c)) > 10


def test_dedup_index():
    idx = DedupIndex()
    a = LOREM * 5
    idx.add("u1", content_hash(a), simhash(a))
    assert idx.find(content_hash(a), simhash(a), exclude_url="u1") is None
    assert idx.find(content_hash(a), simhash(a), exclude_url="u2") == "u1"
    b = a + " tiny change."
    assert idx.find(content_hash(b), simhash(b), exclude_url="u2") == "u1"


def test_heading_ids_are_not_mistaken_for_junk():
    html = page("Cookies", f'<h2 id="cookie-parameters">Cookie params</h2><p>{LOREM}</p>')
    assert "## Cookie params" in extract_html(html, "https://docs.test/").markdown


def test_title_falls_back_to_title_tag_without_site_suffix():
    head = "<head><title>Body - Fields - Docs</title></head>"
    html = f"<html>{head}<body><p>{LOREM}</p></body></html>"
    assert extract_html(html, "https://docs.test/").title == "Body - Fields"
