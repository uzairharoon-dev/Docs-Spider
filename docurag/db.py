"""SQLite metadata store: libraries, pages, crawl jobs and the crawl frontier."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS libraries (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    base_url    TEXT NOT NULL,
    config_json TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pages (
    id            INTEGER PRIMARY KEY,
    library_id    INTEGER NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
    version       TEXT NOT NULL,
    url           TEXT NOT NULL,
    title         TEXT,
    content_hash  TEXT NOT NULL,
    simhash       INTEGER NOT NULL,
    etag          TEXT,
    last_modified TEXT,
    fetched_at    TEXT NOT NULL,
    markdown      TEXT NOT NULL,
    links_json    TEXT NOT NULL DEFAULT '[]',
    anchors_json  TEXT NOT NULL DEFAULT '[]',  -- [[heading text, html id], ...]
    indexed_hash  TEXT,                        -- content_hash when last chunked + embedded
    indexed_model TEXT,                        -- embedder that produced the vectors
    UNIQUE (library_id, version, url)
);
CREATE INDEX IF NOT EXISTS pages_url ON pages(url);

CREATE TABLE IF NOT EXISTS crawl_jobs (
    id            INTEGER PRIMARY KEY,
    library_id    INTEGER NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
    status        TEXT NOT NULL,          -- running | completed | interrupted | failed
    pages_done    INTEGER NOT NULL DEFAULT 0,
    stats_json    TEXT NOT NULL DEFAULT '{}',
    started_at    TEXT NOT NULL,
    finished_at   TEXT
);

CREATE TABLE IF NOT EXISTS crawl_errors (
    id      INTEGER PRIMARY KEY,
    job_id  INTEGER NOT NULL REFERENCES crawl_jobs(id) ON DELETE CASCADE,
    url     TEXT NOT NULL,
    error   TEXT NOT NULL,
    at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS frontier (
    job_id     INTEGER NOT NULL REFERENCES crawl_jobs(id) ON DELETE CASCADE,
    url        TEXT NOT NULL,
    depth      INTEGER NOT NULL,
    status     TEXT NOT NULL DEFAULT 'pending',  -- pending | in_progress | done | skipped | failed
    attempts   INTEGER NOT NULL DEFAULT 0,
    note       TEXT,
    PRIMARY KEY (job_id, url)
);
CREATE INDEX IF NOT EXISTS frontier_claim ON frontier(job_id, status, depth);

CREATE TABLE IF NOT EXISTS chunks (
    id            INTEGER PRIMARY KEY,
    page_id       INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
    library_id    INTEGER NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
    version       TEXT NOT NULL,
    position      INTEGER NOT NULL,
    heading_path  TEXT NOT NULL,      -- JSON list, e.g. ["Guide", "Auth", "OAuth2"]
    anchor        TEXT,               -- HTML id of the section heading, if known
    context       TEXT NOT NULL DEFAULT '',  -- "Title > Section", searched by BM25
    text          TEXT NOT NULL,
    token_count   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS chunks_page ON chunks(page_id);
CREATE INDEX IF NOT EXISTS chunks_lib ON chunks(library_id, version);

-- Used by the built-in "sqlite" vector store.
CREATE TABLE IF NOT EXISTS chunk_vectors (
    chunk_id   INTEGER PRIMARY KEY,   -- no FK: stores own their cleanup, like Qdrant
    model      TEXT NOT NULL,
    library_id INTEGER NOT NULL,
    version    TEXT NOT NULL,
    seq        INTEGER NOT NULL,       -- write sequence; lets readers detect replaced rows
    vector     BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS chunk_vectors_lib ON chunk_vectors(model, library_id, version);

-- BM25 keyword index over chunks (F14). External content: text lives once, in `chunks`;
-- the triggers keep the index in sync, including FK cascade deletes.
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text, context, content='chunks', content_rowid='id', tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS chunks_fts_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, text, context) VALUES (new.id, new.text, new.context);
END;
CREATE TRIGGER IF NOT EXISTS chunks_fts_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text, context)
    VALUES ('delete', old.id, old.text, old.context);
END;
CREATE TRIGGER IF NOT EXISTS chunks_fts_au AFTER UPDATE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text, context)
    VALUES ('delete', old.id, old.text, old.context);
    INSERT INTO chunks_fts(rowid, text, context) VALUES (new.id, new.text, new.context);
END;
"""

# Columns added after M1; created on open for databases made by older versions.
MIGRATIONS = {
    "pages": {
        "anchors_json": "TEXT NOT NULL DEFAULT '[]'",
        "indexed_hash": "TEXT",
        "indexed_model": "TEXT",
    },
    "chunks": {
        "context": "TEXT NOT NULL DEFAULT ''",
    },
}

RESUMABLE_STATUSES = ("running", "interrupted", "failed")


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def to_signed64(value: int) -> int:
    return value - (1 << 64) if value >= (1 << 63) else value


def to_unsigned64(value: int) -> int:
    return value + (1 << 64) if value < 0 else value


class Database:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._migrate_columns()
        upgrading = self._table_exists("chunks") and not self._table_exists("chunks_fts")
        if upgrading:
            # Before SCHEMA creates the FTS triggers: an UPDATE trigger firing against
            # an empty external-content index would corrupt it.
            self._backfill_context()
        self.conn.executescript(SCHEMA)
        if upgrading:
            self.conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES ('rebuild')")

    def _table_exists(self, name: str) -> bool:
        sql = "SELECT 1 FROM sqlite_master WHERE name=?"
        return self.conn.execute(sql, (name,)).fetchone() is not None

    def _backfill_context(self) -> None:
        """Chunks indexed before M3 have no "Title > Section" context for BM25."""
        from docurag.index.chunker import context_line

        rows = self.conn.execute(
            """SELECT c.id, c.heading_path, p.title FROM chunks c
               JOIN pages p ON p.id = c.page_id WHERE c.context = ''"""
        ).fetchall()
        with self.transaction() as conn:
            conn.executemany(
                "UPDATE chunks SET context=? WHERE id=?",
                [(context_line(r[2], json.loads(r[1])), r[0]) for r in rows],
            )

    def _migrate_columns(self) -> None:
        for table, columns in MIGRATIONS.items():
            existing = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if not existing:  # fresh database: SCHEMA creates the table
                continue
            for name, decl in columns.items():
                if name not in existing:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")

    # ---- libraries -------------------------------------------------------
    def upsert_library(self, name: str, base_url: str, config: dict[str, Any]) -> int:
        self.conn.execute(
            """INSERT INTO libraries (name, base_url, config_json, created_at) VALUES (?, ?, ?, ?)
               ON CONFLICT(name) DO UPDATE SET base_url=excluded.base_url,
                                              config_json=excluded.config_json""",
            (name, base_url, json.dumps(config), now()),
        )
        return self.conn.execute("SELECT id FROM libraries WHERE name=?", (name,)).fetchone()[0]

    def get_library(self, name: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM libraries WHERE name=?", (name,)).fetchone()

    def list_libraries(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """SELECT l.id, l.name, l.base_url,
                      COUNT(p.id) AS pages,
                      COALESCE(SUM(p.indexed_hash = p.content_hash), 0) AS pages_indexed,
                      (SELECT COUNT(*) FROM chunks c WHERE c.library_id = l.id) AS chunks,
                      GROUP_CONCAT(DISTINCT p.version) AS versions,
                      MAX(p.fetched_at) AS last_fetched
               FROM libraries l LEFT JOIN pages p ON p.library_id = l.id
               GROUP BY l.id ORDER BY l.name"""
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["versions"] = sorted(d["versions"].split(",")) if d["versions"] else []
            out.append(d)
        return out

    def delete_library(self, name: str) -> bool:
        row = self.get_library(name)
        if row is None:
            return False
        with self.transaction() as conn:
            conn.execute("DELETE FROM chunk_vectors WHERE library_id=?", (row["id"],))
            conn.execute("DELETE FROM libraries WHERE id=?", (row["id"],))
        return True

    # ---- jobs ------------------------------------------------------------
    def create_job(self, library_id: int) -> int:
        cur = self.conn.execute(
            "INSERT INTO crawl_jobs (library_id, status, started_at) VALUES (?, 'running', ?)",
            (library_id, now()),
        )
        return cur.lastrowid  # type: ignore[return-value]

    def get_job(self, job_id: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM crawl_jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            return None
        job = dict(row)
        job["stats"] = json.loads(job.pop("stats_json"))
        job["errors"] = [
            dict(e)
            for e in self.conn.execute(
                "SELECT url, error, at FROM crawl_errors WHERE job_id=? ORDER BY id", (job_id,)
            )
        ]
        return job

    def list_jobs(self, library_id: int | None = None, limit: int = 20) -> list[dict[str, Any]]:
        sql = """SELECT j.*, l.name AS library FROM crawl_jobs j
                 JOIN libraries l ON l.id = j.library_id"""
        params: tuple[Any, ...] = ()
        if library_id is not None:
            sql += " WHERE j.library_id=?"
            params = (library_id,)
        sql += " ORDER BY j.id DESC LIMIT ?"
        return [dict(r) for r in self.conn.execute(sql, (*params, limit))]

    def resumable_job(self, library_id: int) -> sqlite3.Row | None:
        marks = ",".join("?" * len(RESUMABLE_STATUSES))
        return self.conn.execute(
            f"SELECT * FROM crawl_jobs WHERE library_id=? AND status IN ({marks})"
            " ORDER BY id DESC LIMIT 1",
            (library_id, *RESUMABLE_STATUSES),
        ).fetchone()

    def finish_job(self, job_id: int, status: str, pages_done: int, stats: dict[str, int]) -> None:
        self.conn.execute(
            "UPDATE crawl_jobs SET status=?, pages_done=?, stats_json=?, finished_at=? WHERE id=?",
            (status, pages_done, json.dumps(stats), now(), job_id),
        )

    def save_job_progress(self, job_id: int, pages_done: int, stats: dict[str, int]) -> None:
        self.conn.execute(
            "UPDATE crawl_jobs SET pages_done=?, stats_json=? WHERE id=?",
            (pages_done, json.dumps(stats), job_id),
        )

    def job_stats(self, job_id: int) -> dict[str, int]:
        sql = "SELECT stats_json FROM crawl_jobs WHERE id=?"
        row = self.conn.execute(sql, (job_id,)).fetchone()
        return json.loads(row[0]) if row else {}

    def add_job_error(self, job_id: int, url: str, error: str) -> None:
        self.conn.execute(
            "INSERT INTO crawl_errors (job_id, url, error, at) VALUES (?, ?, ?, ?)",
            (job_id, url, error[:1000], now()),
        )

    # ---- pages -----------------------------------------------------------
    def get_page(self, library_id: int, version: str, url: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM pages WHERE library_id=? AND version=? AND url=?",
            (library_id, version, url),
        ).fetchone()

    def find_pages_by_url(self, url: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT p.*, l.name AS library FROM pages p JOIN libraries l ON l.id = p.library_id
               WHERE p.url=? ORDER BY p.fetched_at DESC""",
            (url,),
        ).fetchall()

    def page_urls(self, library_id: int, version: str) -> list[str]:
        return [
            r[0]
            for r in self.conn.execute(
                "SELECT url FROM pages WHERE library_id=? AND version=?", (library_id, version)
            )
        ]

    def page_fingerprints(self, library_id: int, version: str) -> Iterator[tuple[str, str, int]]:
        for r in self.conn.execute(
            "SELECT url, content_hash, simhash FROM pages WHERE library_id=? AND version=?",
            (library_id, version),
        ):
            yield r[0], r[1], to_unsigned64(r[2])

    def upsert_page(
        self,
        *,
        library_id: int,
        version: str,
        url: str,
        title: str | None,
        markdown: str,
        content_hash: str,
        simhash: int,
        etag: str | None,
        last_modified: str | None,
        links: list[str],
        anchors: list[tuple[str, str]] | None = None,
    ) -> None:
        self.conn.execute(
            """INSERT INTO pages (library_id, version, url, title, content_hash, simhash, etag,
                                  last_modified, fetched_at, markdown, links_json, anchors_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(library_id, version, url) DO UPDATE SET
                   title=excluded.title, content_hash=excluded.content_hash,
                   simhash=excluded.simhash, etag=excluded.etag,
                   last_modified=excluded.last_modified, fetched_at=excluded.fetched_at,
                   markdown=excluded.markdown, links_json=excluded.links_json,
                   anchors_json=excluded.anchors_json""",
            (
                library_id, version, url, title, content_hash, to_signed64(simhash), etag,
                last_modified, now(), markdown, json.dumps(links), json.dumps(anchors or []),
            ),
        )

    def touch_page(
        self, page_id: int, etag: str | None, last_modified: str | None, links: list[str] | None
    ) -> None:
        if links is None:
            self.conn.execute(
                "UPDATE pages SET fetched_at=?, etag=COALESCE(?, etag),"
                " last_modified=COALESCE(?, last_modified) WHERE id=?",
                (now(), etag, last_modified, page_id),
            )
        else:
            self.conn.execute(
                "UPDATE pages SET fetched_at=?, etag=COALESCE(?, etag),"
                " last_modified=COALESCE(?, last_modified), links_json=? WHERE id=?",
                (now(), etag, last_modified, json.dumps(links), page_id),
            )

    # ---- chunks (M2) -----------------------------------------------------
    def pages_for_index(self, library_id: int, version: str | None = None) -> list[sqlite3.Row]:
        sql = """SELECT id, library_id, version, url, title, markdown, content_hash,
                        anchors_json, indexed_hash, indexed_model
                 FROM pages WHERE library_id=?"""
        params: tuple[Any, ...] = (library_id,)
        if version is not None:
            sql += " AND version=?"
            params += (version,)
        return self.conn.execute(sql + " ORDER BY id", params).fetchall()

    def chunk_ids_for_page(self, page_id: int) -> list[int]:
        sql = "SELECT id FROM chunks WHERE page_id=?"
        return [r[0] for r in self.conn.execute(sql, (page_id,))]

    def chunk_ids_for_library(self, library_id: int) -> list[int]:
        sql = "SELECT id FROM chunks WHERE library_id=?"
        return [r[0] for r in self.conn.execute(sql, (library_id,))]

    def replace_chunks(self, page: sqlite3.Row, chunks: list[dict[str, Any]]) -> list[int]:
        """Delete a page's chunks and insert new ones. Returns the new chunk ids in order."""
        self.conn.execute("DELETE FROM chunks WHERE page_id=?", (page["id"],))
        ids = []
        for pos, ch in enumerate(chunks):
            cur = self.conn.execute(
                """INSERT INTO chunks (page_id, library_id, version, position, heading_path,
                                       anchor, context, text, token_count)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    page["id"], page["library_id"], page["version"], pos,
                    json.dumps(ch["heading_path"]), ch["anchor"], ch.get("context", ""),
                    ch["text"], ch["token_count"],
                ),
            )
            ids.append(cur.lastrowid)
        return ids

    def mark_indexed(self, page_id: int, content_hash: str | None, model: str | None) -> None:
        self.conn.execute(
            "UPDATE pages SET indexed_hash=?, indexed_model=? WHERE id=?",
            (content_hash, model, page_id),
        )

    def get_chunks(self, chunk_ids: list[int]) -> dict[int, dict[str, Any]]:
        out: dict[int, dict[str, Any]] = {}
        for start in range(0, len(chunk_ids), 500):
            part = chunk_ids[start : start + 500]
            marks = ",".join("?" * len(part))
            for r in self.conn.execute(
                f"""SELECT c.id, c.heading_path, c.anchor, c.context, c.text, c.token_count,
                           c.version,
                           p.url, p.title, l.name AS library
                    FROM chunks c JOIN pages p ON p.id = c.page_id
                    JOIN libraries l ON l.id = c.library_id
                    WHERE c.id IN ({marks})""",
                part,
            ):
                d = dict(r)
                d["heading_path"] = json.loads(d["heading_path"])
                out[d["id"]] = d
        return out
