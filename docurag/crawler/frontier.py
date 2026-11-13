"""Persistent crawl frontier (F6): a crashed or interrupted crawl resumes where it stopped."""

from __future__ import annotations

from collections.abc import Iterable

from docurag.db import Database


class Frontier:
    """BFS queue of URLs for one crawl job, stored in SQLite.

    Every URL is inserted at most once per job, so the table doubles as the
    "seen" set. Claiming flips a row to ``in_progress``; on resume those rows
    go back to ``pending``.
    """

    def __init__(self, db: Database, job_id: int):
        self.db = db
        self.job_id = job_id

    def reset_in_progress(self) -> int:
        return self.db.conn.execute(
            "UPDATE frontier SET status='pending' WHERE job_id=? AND status='in_progress'",
            (self.job_id,),
        ).rowcount

    def add(self, items: Iterable[tuple]) -> int:
        """Insert ``(url, depth)`` or ``(url, depth, lastmod)`` items not seen before.

        Returns how many were new.
        """
        rows = [(self.job_id, it[0], it[1], it[2] if len(it) > 2 else None) for it in items]
        if not rows:
            return 0
        with self.db.transaction() as conn:
            before = conn.total_changes
            conn.executemany(
                "INSERT OR IGNORE INTO frontier (job_id, url, depth, lastmod) VALUES (?, ?, ?, ?)",
                rows,
            )
            return conn.total_changes - before

    def mark_seen(self, url: str, depth: int, status: str = "done") -> bool:
        """Record ``url`` as already handled. False if it was in the frontier already."""
        cur = self.db.conn.execute(
            "INSERT OR IGNORE INTO frontier (job_id, url, depth, status) VALUES (?, ?, ?, ?)",
            (self.job_id, url, depth, status),
        )
        return cur.rowcount > 0

    def claim(self) -> tuple[str, int, str | None] | None:
        row = self.db.conn.execute(
            """UPDATE frontier SET status='in_progress', attempts=attempts+1
               WHERE rowid = (SELECT rowid FROM frontier
                              WHERE job_id=? AND status='pending'
                              ORDER BY depth, rowid LIMIT 1)
               RETURNING url, depth, lastmod""",
            (self.job_id,),
        ).fetchone()
        return (row[0], row[1], row[2]) if row else None

    def complete(self, url: str, status: str = "done", note: str | None = None) -> None:
        self.db.conn.execute(
            "UPDATE frontier SET status=?, note=? WHERE job_id=? AND url=?",
            (status, note, self.job_id, url),
        )

    def counts(self) -> dict[str, int]:
        return {
            r[0]: r[1]
            for r in self.db.conn.execute(
                "SELECT status, COUNT(*) FROM frontier WHERE job_id=? GROUP BY status",
                (self.job_id,),
            )
        }

    def pending(self) -> int:
        return self.db.conn.execute(
            "SELECT COUNT(*) FROM frontier WHERE job_id=? AND status='pending'", (self.job_id,)
        ).fetchone()[0]
