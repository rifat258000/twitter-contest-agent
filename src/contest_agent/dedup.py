"""SQLite-backed dedup store so we don't post the same tweet twice."""

from __future__ import annotations

import sqlite3
from pathlib import Path


class DedupStore:
    def __init__(self, db_path: str) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def _init(self) -> None:
        with self._conn() as c:
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS posted (
                    tweet_id TEXT PRIMARY KEY,
                    posted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    score REAL
                )
                """
            )

    def has_posted(self, tweet_id: str) -> bool:
        with self._conn() as c:
            cur = c.execute("SELECT 1 FROM posted WHERE tweet_id = ?", (tweet_id,))
            return cur.fetchone() is not None

    def mark_posted(self, tweet_id: str, score: float) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO posted (tweet_id, score) VALUES (?, ?)",
                (tweet_id, score),
            )
