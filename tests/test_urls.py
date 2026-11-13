import pytest

from docurag.urls import Scope, derive_prefix, normalize_url


@pytest.mark.parametrize(
    ("raw", "base", "expected"),
    [
        ("HTTPS://Docs.Test:443/a/b#frag", None, "https://docs.test/a/b"),
        ("http://docs.test", None, "http://docs.test/"),
        ("http://docs.test:8080/x", None, "http://docs.test:8080/x"),
        ("../c?utm_source=x&page=2", "https://docs.test/a/b/", "https://docs.test/a/c?page=2"),
        ("mailto:a@b.c", None, None),
        ("javascript:void(0)", None, None),
        ("http://[bad", None, None),
    ],
)
def test_normalize_url(raw, base, expected):
    assert normalize_url(raw, base) == expected


@pytest.mark.parametrize(
    ("url", "prefix"),
    [
        ("https://x.test/", "/"),
        ("https://x.test/en/latest/", "/en/latest/"),
        ("https://x.test/docs/intro", "/docs/"),
        ("https://x.test/sitemap.xml", "/"),
        ("https://x.test/docs/llms.txt", "/"),
    ],
)
def test_derive_prefix(url, prefix):
    assert derive_prefix(url) == prefix


def test_scope_rules():
    scope = Scope.from_config(
        ["https://docs.test/en/latest/"], include=["/tutorial/"], exclude=["*/zh/*"]
    )
    assert scope.in_scope("https://docs.test/en/latest/tutorial/a")
    assert not scope.in_scope("https://other.test/en/latest/tutorial/a")
    assert not scope.in_scope("https://docs.test/en/stable/tutorial/a")
    assert not scope.in_scope("https://docs.test/en/latest/zh/tutorial/a")
    assert not scope.in_scope("https://docs.test/en/latest/logo.png")
    assert scope.is_included("https://docs.test/en/latest/tutorial/a")
    assert not scope.is_included("https://docs.test/en/latest/reference/a")
