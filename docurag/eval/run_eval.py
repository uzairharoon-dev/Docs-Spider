"""Retrieval evaluation: Recall@k, MRR and latency per search configuration.

Dataset: JSON Lines, one question per line::

    {"id": "fa-01", "library": "fastapi", "question": "...", "expected": ["https://.../page/"]}

``expected`` lists every page that answers the question. A hit is relevant when its URL
equals an expected URL; an expected URL with a ``#fragment`` also requires that section.

Metrics, per configuration:

* Recall@k - share of questions with at least one relevant hit in the top k
  (the usual "hit rate" form for single-answer RAG retrieval).
* MRR@k - mean of 1 / rank of the first relevant hit (0 if none in the top k).
* p50 / p95 latency - wall time of one search call, query embedding included.
"""

from __future__ import annotations

import json
import statistics
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urldefrag

from docurag.index.search import SearchHit
from docurag.urls import normalize_url

DEFAULT_DATASET = Path(__file__).parent / "dataset" / "questions.jsonl"
RECALL_AT = (1, 3, 5, 10)


@dataclass
class Question:
    id: str
    library: str
    question: str
    expected: list[str]
    version: str | None = None


@dataclass(frozen=True)
class RunConfig:
    label: str
    mode: str
    rerank: bool = False

    @classmethod
    def parse(cls, spec: str) -> RunConfig:
        """'vector', 'bm25', 'hybrid' or any of them with '+rerank'."""
        mode, _, extra = spec.strip().partition("+")
        if mode not in ("vector", "bm25", "hybrid") or extra not in ("", "rerank"):
            raise ValueError(f"bad config {spec!r}; use vector, bm25, hybrid, hybrid+rerank")
        return cls(spec.strip(), mode, extra == "rerank")


@dataclass
class QuestionResult:
    id: str
    first_relevant_rank: int | None
    latency_ms: float
    top_urls: list[str]


@dataclass
class ConfigReport:
    label: str
    n: int
    k: int
    recall: dict[int, float]
    mrr: float
    p50_ms: float
    p95_ms: float
    results: list[QuestionResult] = field(default_factory=list)


def load_dataset(path: Path = DEFAULT_DATASET) -> list[Question]:
    questions = []
    for lineno, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("//"):
            continue
        try:
            q = Question(**json.loads(line))
        except (json.JSONDecodeError, TypeError) as exc:
            raise ValueError(f"{path}:{lineno}: {exc}") from exc
        if not q.expected:
            raise ValueError(f"{path}:{lineno}: 'expected' is empty")
        questions.append(q)
    ids = [q.id for q in questions]
    if len(ids) != len(set(ids)):
        raise ValueError(f"{path}: duplicate question ids")
    return questions


def _split(url: str) -> tuple[str, str]:
    base, frag = urldefrag(url)
    return normalize_url(base) or base, frag


def is_relevant(hit: SearchHit, expected: Iterable[str]) -> bool:
    hit_url = normalize_url(hit.url) or hit.url
    for exp in expected:
        url, frag = _split(exp)
        if hit_url == url and (not frag or hit.anchor == frag):
            return True
    return False


def first_relevant_rank(hits: list[SearchHit], expected: list[str]) -> int | None:
    for rank, hit in enumerate(hits, 1):
        if is_relevant(hit, expected):
            return rank
    return None


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    return statistics.quantiles(values, n=100, method="inclusive")[int(pct) - 1]


SearchFn = Callable[[Question, RunConfig, int], list[SearchHit]]


def evaluate(
    search: SearchFn,
    questions: list[Question],
    configs: list[RunConfig],
    k: int = 10,
    on_progress: Callable[[RunConfig, int, int], None] | None = None,
) -> list[ConfigReport]:
    reports = []
    for cfg in configs:
        if questions:  # warm-up: model loading and caches don't count as query latency
            search(questions[0], cfg, k)
        results = []
        for i, q in enumerate(questions, 1):
            started = time.perf_counter()
            hits = search(q, cfg, k)
            elapsed = (time.perf_counter() - started) * 1000
            results.append(
                QuestionResult(q.id, first_relevant_rank(hits, q.expected), elapsed,
                               [h.citation for h in hits[:3]])
            )
            if on_progress:
                on_progress(cfg, i, len(questions))
        n = len(results)
        ranks = [r.first_relevant_rank for r in results]
        recall = {
            at: sum(1 for r in ranks if r is not None and r <= at) / n if n else 0.0
            for at in RECALL_AT if at <= k
        }
        mrr = sum(1 / r for r in ranks if r is not None and r <= k) / n if n else 0.0
        lat = [r.latency_ms for r in results]
        reports.append(ConfigReport(cfg.label, n, k, recall, mrr, _percentile(lat, 50),
                                    _percentile(lat, 95), results))
    return reports


def missing_pages(questions: list[Question], known_urls: set[str]) -> dict[str, list[str]]:
    """Questions whose expected pages aren't indexed at all (corpus not built, site moved)."""
    out = {}
    for q in questions:
        missing = [e for e in q.expected if _split(e)[0] not in known_urls]
        if len(missing) == len(q.expected):
            out[q.id] = missing
    return out


def to_markdown(reports: list[ConfigReport]) -> str:
    ats = sorted({at for r in reports for at in r.recall})
    k = reports[0].k if reports else 10
    recalls = " | ".join(f"Recall@{a}" for a in ats)
    head = f"| Config | {recalls} | MRR@{k} | p50 ms | p95 ms |"
    sep = "| --- |" + " ---: |" * (len(ats) + 3)
    rows = [
        f"| {r.label} | " + " | ".join(f"{r.recall[a]:.3f}" for a in ats)
        + f" | {r.mrr:.3f} | {r.p50_ms:.0f} | {r.p95_ms:.0f} |"
        for r in reports
    ]
    return "\n".join([head, sep, *rows])


def to_json(reports: list[ConfigReport], meta: dict | None = None) -> str:
    return json.dumps({"meta": meta or {}, "reports": [asdict(r) for r in reports]}, indent=2)
