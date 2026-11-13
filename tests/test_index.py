import json
from pathlib import Path

import httpx
import numpy as np
import pytest
import yaml
from typer.testing import CliRunner

from docurag.config import ChunkConfig, EmbedderConfig, StoreConfig
from docurag.crawler.crawler import Crawler
from docurag.extract.dedup import content_hash, simhash
from docurag.index.chunker import context_line
from docurag.index.embedder import HashEmbedder, OpenAIEmbedder, embedder_name, make_embedder
from docurag.index.pipeline import Indexer
from docurag.index.search import Searcher, UnknownLibrary
from docurag.index.store import QdrantVectorStore, SQLiteVectorStore, VectorItem

DOCS = {
    "http://docs.test/deps/": (
        "Dependencies",
        "# Dependencies\n\nDependency injection: declare a dependency with Depends and FastAPI "
        "calls it for every request, caching the result.\n",
        [("Dependencies", "dependencies")],
    ),
    "http://docs.test/cookies/": (
        "Cookie Parameters",
        "# Cookie Parameters\n\n## Import Cookie\n\nRead cookie values with the Cookie class "
        "just like Query and Path parameters.\n",
        [("Cookie Parameters", "cookie-parameters"), ("Import Cookie", "import-cookie")],
    ),
    "http://docs.test/cors/": (
        "CORS",
        "# CORS\n\nCross-origin resource sharing middleware allows browsers on other origins "
        "to call your API.\n",
        [("CORS", "cors")],
    ),
}


def add_pages(db, name="demo", version="latest", docs=DOCS):
    lib_id = db.upsert_library(name, "http://docs.test/", {})
    for url, (title, md, anchors) in docs.items():
        db.upsert_page(
            library_id=lib_id, version=version, url=url, title=title, markdown=md,
            content_hash=content_hash(md), simhash=simhash(md), etag=None, last_modified=None,
            links=[], anchors=anchors,
        )
    return lib_id


def make_indexer(db, store=None, embedder=None):
    embedder = embedder or HashEmbedder(256)
    store = store or SQLiteVectorStore(db, embedder.name)
    return Indexer(db, embedder, store, ChunkConfig(min_tokens=20, max_tokens=200)), store


# ---------------------------------------------------------------- embedders
def test_hash_embedder_normalized_and_deterministic():
    e = HashEmbedder(64)
    m = e.embed_documents(["hello world", "hello world", ""])
    assert m.shape == (3, 64) and m.dtype == np.float32
    assert np.allclose(np.linalg.norm(m[:2], axis=1), 1.0)
    assert np.allclose(m[0], m[1])
    assert e.embed_documents([]).shape == (0, 64)


def test_openai_embedder_batches_and_sorts(monkeypatch):
    monkeypatch.setenv("TEST_KEY", "sk-test")
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        seen.append((req.headers["authorization"], body))
        n = len(body["input"])
        data = [{"index": i, "embedding": [float(i + 1), 0.0, 1.0]} for i in range(n)]
        return httpx.Response(200, json={"data": list(reversed(data))})

    cfg = EmbedderConfig(provider="openai", model="m", api_key_env="TEST_KEY", batch_size=2,
                         base_url="http://emb.test/v1", dim=3)
    e = OpenAIEmbedder(cfg, client=httpx.Client(transport=httpx.MockTransport(handler)))
    m = e.embed_documents(["a", "b", "c"])
    assert m.shape == (3, 3)
    assert [len(b["input"]) for _, b in seen] == [2, 1]
    assert seen[0][0] == "Bearer sk-test" and seen[0][1]["dimensions"] == 3
    assert m[1, 0] > m[0, 0]  # sorted back into input order by "index"
    assert e.name == embedder_name(cfg) == "openai:m:3"


def test_openai_embedder_needs_key(monkeypatch):
    monkeypatch.delenv("NOPE_KEY", raising=False)
    with pytest.raises(RuntimeError, match="NOPE_KEY"):
        OpenAIEmbedder(EmbedderConfig(provider="openai", api_key_env="NOPE_KEY"))


def test_make_embedder_hash(tmp_path):
    e = make_embedder(EmbedderConfig(provider="hash", dim=32), tmp_path)
    assert e.name == "hash:32" == embedder_name(EmbedderConfig(provider="hash", dim=32))


# ---------------------------------------------------------------- stores
@pytest.fixture(params=["sqlite", "qdrant"])
def store(request, db):
    if request.param == "sqlite":
        return SQLiteVectorStore(db, "test-model")
    return QdrantVectorStore(StoreConfig(type="qdrant", path=Path(":memory:")), Path("."), "m")


def _vec(*xs):
    v = np.array(xs, dtype=np.float32)
    return v / np.linalg.norm(v)


def test_store_contract(store):
    assert store.search(_vec(1, 0, 0), 3) == []
    store.upsert([
        VectorItem(1, 10, "latest", _vec(1, 0, 0)),
        VectorItem(2, 10, "latest", _vec(0.9, 0.1, 0)),
        VectorItem(3, 10, "v1", _vec(0, 1, 0)),
        VectorItem(4, 20, "latest", _vec(1, 0, 0)),
    ])
    assert [i for i, _ in store.search(_vec(1, 0, 0), 2, library_id=10)] == [1, 2]
    assert [i for i, _ in store.search(_vec(1, 0, 0), 5, library_id=10, version="v1")] == [3]
    assert {i for i, _ in store.search(_vec(1, 0, 0), 10)} == {1, 2, 3, 4}
    top = store.search(_vec(1, 0, 0), 1, library_id=10)[0]
    assert top[0] == 1 and top[1] == pytest.approx(1.0, abs=1e-5)

    store.upsert([VectorItem(1, 10, "latest", _vec(0, 0, 1))])  # replace
    assert store.search(_vec(1, 0, 0), 1, library_id=10)[0][0] == 2
    store.delete([2])
    assert [i for i, _ in store.search(_vec(1, 0, 0), 5, library_id=10, version="latest")] == [1]
    store.delete_library(10)
    assert [i for i, _ in store.search(_vec(1, 0, 0), 5)] == [4]


def test_sqlite_store_cache_sees_new_writes(db):
    s = SQLiteVectorStore(db, "m")
    s.upsert([VectorItem(1, 1, "latest", _vec(1, 0))])
    assert len(s.search(_vec(1, 0), 5)) == 1
    s.upsert([VectorItem(2, 1, "latest", _vec(0, 1))])
    assert len(s.search(_vec(1, 0), 5)) == 2


def test_sqlite_store_dim_mismatch_is_explained(db):
    s = SQLiteVectorStore(db, "m")
    s.upsert([VectorItem(1, 1, "latest", _vec(1, 0, 0))])
    with pytest.raises(RuntimeError, match="--force"):
        s.search(_vec(1, 0), 1)


# ---------------------------------------------------------------- pipeline
def test_index_and_search(db):
    lib_id = add_pages(db)
    indexer, store = make_indexer(db)
    stats = indexer.index_library(lib_id)
    assert stats.pages_indexed == 3 and stats.chunks_written >= 3

    hits = Searcher(db, indexer.embedder, store).search("cookie class", "demo", k=2)
    assert hits[0].url == "http://docs.test/cookies/"
    assert hits[0].citation.startswith("http://docs.test/cookies/#")
    assert hits[0].section.startswith("Cookie Parameters")
    assert hits[0].as_dict()["citation"] == hits[0].citation


def test_reindex_is_incremental(db):
    lib_id = add_pages(db)
    indexer, store = make_indexer(db)
    indexer.index_library(lib_id)
    again = indexer.index_library(lib_id)
    assert again.pages_indexed == 0 and again.pages_skipped == 3

    changed = dict(DOCS)
    title, md, anchors = changed["http://docs.test/cors/"]
    changed["http://docs.test/cors/"] = (title, md + "\nNow with credentials support.\n", anchors)
    add_pages(db, docs=changed)
    third = indexer.index_library(lib_id)
    assert third.pages_indexed == 1 and third.chunks_deleted >= 1
    n_chunks = db.conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    n_vecs = db.conn.execute("SELECT COUNT(*) FROM chunk_vectors").fetchone()[0]
    assert n_chunks == n_vecs  # no orphans after replacing a page's chunks


def test_model_change_triggers_reindex(db):
    lib_id = add_pages(db)
    make_indexer(db, embedder=HashEmbedder(64))[0].index_library(lib_id)
    stats = make_indexer(db, embedder=HashEmbedder(128))[0].index_library(lib_id)
    assert stats.pages_indexed == 3


def test_search_filters_library_and_version(db):
    add_pages(db, "a")
    add_pages(db, "b", version="2.0")
    indexer, store = make_indexer(db)
    for row in db.list_libraries():
        indexer.index_library(row["id"])
    s = Searcher(db, indexer.embedder, store)
    assert {h.library for h in s.search("cookie", "a", k=10)} == {"a"}
    assert {h.version for h in s.search("cookie", "b", version="2.0", k=10)} == {"2.0"}
    assert s.search("cookie", "b", version="nope") == []
    assert s.search("   ", "a") == []
    with pytest.raises(UnknownLibrary):
        s.search("x", "missing")


def test_context_line_dedupes_title():
    assert context_line("Security", ["Security", "OAuth2"]) == "Security > OAuth2"
    assert context_line(None, []) == ""


async def test_crawl_then_index_keeps_html_anchors(db, lib, settings, site):
    body = (
        '<h2 id="declare-deps">Declare <code>Depends</code></h2>'
        "<p>" + "Declare dependencies in path operation functions. " * 10 + "</p>"
    )
    from tests.conftest import page

    site.routes["/"] = (200, "text/html", page("Deps", body))
    lib = lib.model_copy(update={"discover_sitemaps": False})
    async with site.client() as client:
        await Crawler(db, lib, settings, client=client).run()
    indexer, store = make_indexer(db)
    indexer.index_library(db.get_library("demo")["id"])
    hit = Searcher(db, indexer.embedder, store).search("declare dependencies", "demo")[0]
    assert hit.anchor == "declare-deps"
    assert hit.citation == "http://docs.test/#declare-deps"


# ---------------------------------------------------------------- CLI
def test_cli_index_and_search(tmp_path, monkeypatch):
    from docurag.db import Database
    from docurag.serve.cli import app

    cfg = tmp_path / "docurag.yaml"
    cfg.write_text(yaml.safe_dump({
        "data_dir": str(tmp_path / "data"),
        "embedder": {"provider": "hash", "dim": 128},
    }))
    db = Database(tmp_path / "data" / "docurag.db")
    add_pages(db)
    db.close()

    runner = CliRunner()
    res = runner.invoke(app, ["-c", str(cfg), "index", "demo"])
    assert res.exit_code == 0, res.output
    assert "3 pages" in res.output

    res = runner.invoke(app, ["-c", str(cfg), "search", "cors middleware", "--lib", "demo",
                              "-k", "2", "--json"])
    assert res.exit_code == 0, res.output
    hits = json.loads(res.output)
    assert hits[0]["url"] == "http://docs.test/cors/"
    assert hits[0]["citation"] == "http://docs.test/cors/#cors"

    res = runner.invoke(app, ["-c", str(cfg), "search", "x", "--lib", "missing"])
    assert res.exit_code == 1

    res = runner.invoke(app, ["-c", str(cfg), "libraries"])
    assert res.exit_code == 0 and "demo" in res.output
