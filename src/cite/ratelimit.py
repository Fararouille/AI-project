"""Token buckets with a process-local and a SQLite-backed implementation.

The in-memory limiter is enough for a laptop and a single worker. The SQLite
limiter gives several workers on one host a single budget without adding Redis:
a row per identity, updated inside ``BEGIN IMMEDIATE`` so two processes cannot
both spend the last token.

Both implement the same one-method interface, so swapping in Redis later means
one more class rather than changes at the call site.
"""

from __future__ import annotations

import math
import os
import random
import sqlite3
import threading
import time
from pathlib import Path
from typing import Protocol


class Limiter(Protocol):
    per_minute: int

    def consume(self, identity: str) -> tuple[bool, int]: ...


class TokenBucket:
    __slots__ = ("capacity", "rate", "tokens", "updated")

    def __init__(self, per_minute: int, now: float | None = None) -> None:
        self.capacity = float(per_minute)
        self.rate = per_minute / 60.0
        self.tokens = float(per_minute)
        self.updated = time.monotonic() if now is None else now

    def consume(self, now: float) -> tuple[bool, float]:
        elapsed = max(0.0, now - self.updated)
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
        self.updated = now
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True, 0.0
        wait = (1.0 - self.tokens) / self.rate if self.rate else 0.0
        return False, wait


class RateLimiter:
    """Per-identity token bucket. Memory-only: one process, no Redis."""

    def __init__(self, per_minute: int) -> None:
        self.per_minute = per_minute
        self._buckets: dict[str, TokenBucket] = {}
        self._lock = threading.Lock()

    def consume(self, identity: str) -> tuple[bool, int]:
        if self.per_minute <= 0:
            return True, 0
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            bucket = self._buckets.get(identity)
            if bucket is None:
                bucket = TokenBucket(self.per_minute, now)
                self._buckets[identity] = bucket
            allowed, wait = bucket.consume(now)
        return allowed, max(0, math.ceil(wait))

    def _prune(self, now: float) -> None:
        if len(self._buckets) < 4096:
            return
        stale = [key for key, bucket in self._buckets.items() if now - bucket.updated > 3600]
        for key in stale:
            del self._buckets[key]


class SQLiteRateLimiter:
    """Shared token bucket for multiple workers on one host.

    Wall-clock time is used instead of ``time.monotonic()`` because monotonic
    origins differ between processes. Clock jumps move every bucket together, so
    this is correct enough for abuse prevention and wrong as an accounting system.
    """

    def __init__(self, per_minute: int, path: str | Path) -> None:
        self.per_minute = per_minute
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        self.path = destination
        self._lock = threading.Lock()
        connection = self._connect()
        try:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS buckets (
                    identity TEXT PRIMARY KEY,
                    tokens REAL NOT NULL,
                    updated REAL NOT NULL
                )
                """
            )
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            str(self.path), timeout=30.0, isolation_level=None, check_same_thread=False
        )
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def consume(self, identity: str) -> tuple[bool, int]:
        if self.per_minute <= 0:
            return True, 0
        now = time.time()
        capacity = float(self.per_minute)
        rate = self.per_minute / 60.0

        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT tokens, updated FROM buckets WHERE identity = ?", (identity,)
                ).fetchone()
                if row is None:
                    tokens = capacity
                else:
                    elapsed = max(0.0, now - float(row[1]))
                    tokens = min(capacity, float(row[0]) + elapsed * rate)
                if tokens >= 1.0:
                    tokens -= 1.0
                    allowed, wait = True, 0.0
                else:
                    allowed, wait = False, (1.0 - tokens) / rate
                connection.execute(
                    """
                    INSERT INTO buckets (identity, tokens, updated) VALUES (?, ?, ?)
                    ON CONFLICT(identity) DO UPDATE SET
                        tokens = excluded.tokens, updated = excluded.updated
                    """,
                    (identity, tokens, now),
                )
                # Prune opportunistically rather than growing a row per IP forever.
                if random.random() < 0.01:
                    connection.execute(
                        "DELETE FROM buckets WHERE updated < ?", (now - 3600.0,)
                    )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
            finally:
                connection.close()
        return allowed, max(0, math.ceil(wait))


def build_limiter(
    per_minute: int, backend: str = "sqlite", path: str | Path | None = None
) -> Limiter:
    """Pick the configured limiter, falling back to memory if SQLite cannot open.

    A broken rate-limit database must not take the whole API down. The fallback is
    printed once at construction so the operator can see it in the container log.
    """
    if per_minute <= 0:
        return RateLimiter(0)
    if backend == "sqlite":
        destination = path or os.getenv("CITE_RATE_LIMIT_DB", "data/rate-limit.db")
        try:
            return SQLiteRateLimiter(per_minute, destination)
        except (sqlite3.Error, OSError) as exc:
            print(f"[cite] shared rate limiter unavailable ({exc}); using memory")
    return RateLimiter(per_minute)
