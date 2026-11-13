import json
import sqlite3

import pytest
import yaml
from typer.testing import CliRunner

from docurag.db import Database
from docurag.eval.run_eval import (
    RunConfig,
    evaluate,
    first_relevant_rank,
    is_relevant,
    load_dataset,
    missing_pages,
    to_markdown,
)
from docurag.index.embedder import HashEmbedder
from docurag.index.hybrid import bm25_search, fts_query, rrf
from docurag.index.search import Searcher, SearchHit
from docurag.index.store import SQLiteVectorStore
from tests.test_index import DOCS, add_pages, make_indexer


def indexed(db):
    lib_id = add_pages(db)
    indexer, store = make_indexer(db)
    indexer.index_library(lib_id)
    return lib_id, indexer, store


# ---------------------------------------------------------------- BM25
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("How do I read cookies?", '"read" OR "cookies"'),
        # FTS5 operators become plain quoted terms ("and"/"or" are also stopwords)
        ('AND OR "quote" NEAR(x) col:val *', '"quote" OR "near" OR "x" OR "col" OR "val"'),
        ("what is it", '"what" OR "is" OR "it"'),  # only stopwords: keep them
        ("   ?!  ", None),
        ("read read READ", '"read"'),
    ],
)
def test_fts_query_is_safe(text, expected):
    assert fts_query(text) == expected


def test_bm25_finds_keyword_match_and_filters(db):
    lib_id, *_ = indexed(db)
    ranked = bm25_search(db, "cookie class", 5, lib_id)
    chunks = db.get_chunks([cid for cid, _ in ranked])
    assert chunks[ranked[0][0]]["url"] == "http://docs.test/cookies/"
    assert all(s > 0 for _, s in ranked)  # flipped: higher is better
    assert ranked == sorted(ranked, key=lambda kv: -kv[1])
    assert bm25_search(db, "cookie", 5, lib_id, version="nope") == []
    assert bm25_search(db, "cookie", 5, library_id=999) == []
    assert bm25_search(db, 'weird " syntax ) (', 5) is not None  # no FTS5 syntax error


def test_bm25_matches_heading_context_and_stems(db):
    lib_id, *_ = indexed(db)
    # "Import Cookie" is a heading; "imports" stems to the same token.
    ranked = bm25_search(db, "imports", 5, lib_id)
    assert db.get_chunks([ranked[0][0]])[ranked[0][0]]["url"] == "http://docs.test/cookies/"


def test_fts_stays_in_sync_on_reindex_and_delete(db):
    lib_id, indexer, _ = indexed(db)
    title, md, anchors = DOCS["http://docs.test/cors/"]
    changed = dict(DOCS)
    changed["http://docs.test/cors/"] = (title, md.replace("browsers", "zebras"), anchors)
    add_pages(db, docs=changed)
    indexer.index_library(lib_id)
    assert bm25_search(db, "zebras", 5, lib_id)
    assert not bm25_search(db, "browsers", 5, lib_id)
    db.delete_library("demo")  # FK cascade must also clear the FTS index
    assert bm25_search(db, "zebras", 5) == []
    assert db.conn.execute("SELECT COUNT(*) FROM chunk_vectors").fetchone()[0] == 0


def test_m2_database_gets_fts_backfilled(tmp_path):
    path = tmp_path / "old.db"
    db = Database(path)
    lib_id = add_pages(db)
    page = db.pages_for_index(lib_id)[0]
    db.replace_chunks(page, [{"text": "legacy chunk about zebras", "heading_path": ["Deps"],
                              "anchor": None, "token_count": 4}])
    # Turn it back into an M2-era database: no FTS table, no context.
    db.conn.executescript(
        "DROP TRIGGER chunks_fts_ai; DROP TRIGGER chunks_fts_ad; DROP TRIGGER chunks_fts_au;"
        "DROP TABLE chunks_fts; UPDATE chunks SET context='';"
    )
    db.close()

    db = Database(path)  # reopening migrates
    assert bm25_search(db, "zebras", 5)
    ctx = db.conn.execute("SELECT context FROM chunks").fetchone()[0]
    assert ctx == "Dependencies > Deps"
    db.close()


# ---------------------------------------------------------------- fusion + search modes
def test_rrf():
    fused = rrf([[(1, 0.9), (2, 0.8), (3, 0.1)], [(3, 12.0), (1, 4.0)]], k=60)
    assert [d for d, _ in fused] == [1, 3, 2]
    assert fused[0][1] == pytest.approx(1 / 61 + 1 / 62)
    assert rrf([]) == []


def test_search_modes(db):
    _, indexer, store = indexed(db)
    s = Searcher(db, indexer.embedder, store)
    for mode in ("vector", "bm25", "hybrid"):
        hits = s.search("cors middleware", "demo", k=2, mode=mode)
        assert hits[0].url == "http://docs.test/cors/", mode
    hybrid = s.search("cors middleware", "demo", k=1, mode="hybrid")[0]
    assert set(hybrid.scores) == {"vector", "bm25", "rrf"}
    assert hybrid.score == hybrid.scores["rrf"]
    with pytest.raises(ValueError):
        s.search("x", mode="fuzzy")


def test_bm25_mode_needs_no_embedder(db):
    indexed(db)
    hits = Searcher(db).search("cookie", "demo", mode="bm25")
    assert hits and hits[0].scores.keys() == {"bm25"}
    with pytest.raises(ValueError, match="embedder"):
        Searcher(db).search("cookie", "demo", mode="hybrid")


class ReverseReranker:
    """Fake cross-encoder that prefers whatever mentions CORS."""

    name = "fake"

    def __init__(self):
        self.calls = []

    def score(self, query, documents):
        self.calls.append(len(documents))
        return [10.0 if "CORS" in d else 0.0 for d in documents]


def test_rerank_reorders_candidates(db):
    _, indexer, store = indexed(db)
    rr = ReverseReranker()
    s = Searcher(db, indexer.embedder, store, rr, rerank_candidates=10)
    hits = s.search("cookie class", "demo", k=2, mode="hybrid", rerank=True)
    assert hits[0].url == "http://docs.test/cors/"
    assert hits[0].scores["rerank"] == 10.0 and "rrf" in hits[0].scores
    assert rr.calls == [3]  # every candidate scored once, at most rerank_candidates
    with pytest.raises(ValueError):
        Searcher(db, indexer.embedder, store).search("x", rerank=True)


# ---------------------------------------------------------------- eval harness
def hit(url, anchor=None):
    return SearchHit(0, 1.0, "demo", "latest", url, anchor, "T", [], "text")


def test_relevance_rules():
    assert is_relevant(hit("http://docs.test/a/"), ["http://DOCS.test/a/"])
    assert is_relevant(hit("http://docs.test/a/", "x"), ["http://docs.test/a/#x"])
    assert not is_relevant(hit("http://docs.test/a/", "y"), ["http://docs.test/a/#x"])
    assert not is_relevant(hit("http://docs.test/b/"), ["http://docs.test/a/"])
    hits = [hit("http://docs.test/b/"), hit("http://docs.test/a/")]
    assert first_relevant_rank(hits, ["http://docs.test/a/"]) == 2
    assert first_relevant_rank(hits, ["http://docs.test/z/"]) is None


def test_evaluate_metrics(tmp_path):
    path = tmp_path / "q.jsonl"
    path.write_text(
        "\n".join(json.dumps({"id": f"q{i}", "library": "demo", "question": f"q{i}",
                              "expected": [f"http://docs.test/{i}/"]}) for i in range(4)),
        encoding="utf-8",
    )
    questions = load_dataset(path)
    # q0 at rank 1, q1 at rank 2, q2 at rank 5, q3 never
    ranks = {"q0": 1, "q1": 2, "q2": 5, "q3": None}

    def fake_search(q, cfg, k):
        out = [hit(f"http://docs.test/other{j}/") for j in range(k)]
        if ranks[q.id]:
            out[ranks[q.id] - 1] = hit(q.expected[0])
        return out

    [rep] = evaluate(fake_search, questions, [RunConfig.parse("bm25")], k=10)
    assert rep.n == 4
    assert rep.recall == {1: 0.25, 3: 0.5, 5: 0.75, 10: 0.75}
    assert rep.mrr == pytest.approx((1 + 1 / 2 + 1 / 5) / 4)
    table = to_markdown([rep])
    assert "| bm25 | 0.250 | 0.500 | 0.750 | 0.750 | 0.425 |" in table


def test_dataset_validation(tmp_path):
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"id": "a", "library": "x", "question": "q", "expected": []}\n')
    with pytest.raises(ValueError, match="empty"):
        load_dataset(bad)
    with pytest.raises(ValueError):
        RunConfig.parse("vector+magic")
    assert RunConfig.parse("hybrid+rerank") == RunConfig("hybrid+rerank", "hybrid", True)


def test_bundled_dataset_is_well_formed():
    questions = load_dataset()
    assert 50 <= len(questions) <= 100
    assert {q.library for q in questions} == {"fastapi", "httpx", "typer"}
    assert all(e.startswith("https://") for q in questions for e in q.expected)


def test_missing_pages():
    qs = load_dataset()[:2]
    assert set(missing_pages(qs, set())) == {qs[0].id, qs[1].id}
    assert missing_pages(qs, {e for q in qs for e in q.expected}) == {}


def test_cli_search_modes_and_eval(tmp_path):
    from docurag.serve.cli import app

    cfg = tmp_path / "docurag.yaml"
    cfg.write_text(yaml.safe_dump({
        "data_dir": str(tmp_path / "data"),
        "embedder": {"provider": "hash", "dim": 128},
    }))
    db = Database(tmp_path / "data" / "docurag.db")
    lib_id = add_pages(db)
    emb = HashEmbedder(128)
    from docurag.config import ChunkConfig
    from docurag.index.pipeline import Indexer

    Indexer(db, emb, SQLiteVectorStore(db, emb.name), ChunkConfig(min_tokens=20)).index_library(
        lib_id
    )
    db.close()

    dataset = tmp_path / "q.jsonl"
    dataset.write_text(
        json.dumps({"id": "c", "library": "demo", "question": "cors middleware",
                    "expected": ["http://docs.test/cors/"]}) + "\n"
        + json.dumps({"id": "gone", "library": "demo", "question": "x",
                      "expected": ["http://docs.test/missing/"]}) + "\n",
        encoding="utf-8",
    )
    runner = CliRunner()
    for mode in ("bm25", "vector", "hybrid"):
        res = runner.invoke(app, ["-c", str(cfg), "search", "cors", "--lib", "demo",
                                  "--mode", mode, "--json"])
        assert res.exit_code == 0, res.output
        assert json.loads(res.output)[0]["url"] == "http://docs.test/cors/"
    res = runner.invoke(app, ["-c", str(cfg), "search", "cors", "--mode", "nope"])
    assert res.exit_code == 2

    out = tmp_path / "res.json"
    res = runner.invoke(app, ["-c", str(cfg), "eval", "--dataset", str(dataset),
                              "--configs", "bm25,hybrid", "--out", str(out)])
    assert res.exit_code == 0, res.output
    assert "| bm25 | 1.000 |" in res.output and "| hybrid | 1.000 |" in res.output
    data = json.loads(out.read_text())
    assert data["meta"]["skipped"] == ["gone"] and len(data["reports"]) == 2


def test_fts5_available():
    sqlite3.connect(":memory:").execute("CREATE VIRTUAL TABLE t USING fts5(x)")
