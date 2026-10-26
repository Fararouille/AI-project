from __future__ import annotations

import time

from cite.ratelimit import RateLimiter, SQLiteRateLimiter, build_limiter


def test_memory_limiter_allows_capacity_then_denies() -> None:
    limiter = RateLimiter(2)
    assert limiter.consume("key:a") == (True, 0)
    assert limiter.consume("key:a") == (True, 0)
    allowed, retry_after = limiter.consume("key:a")
    assert allowed is False
    assert retry_after >= 1
    assert limiter.consume("key:b") == (True, 0)


def test_sqlite_limiters_share_a_budget_across_instances(tmp_path) -> None:
    path = tmp_path / "rate-limit.db"
    first = SQLiteRateLimiter(2, path)
    second = SQLiteRateLimiter(2, path)

    assert first.consume("key:a")[0] is True
    assert second.consume("key:a")[0] is True
    assert second.consume("key:a")[0] is False
    assert first.consume("key:a")[0] is False
    # Identity is part of the key, so another caller gets a fresh bucket.
    assert first.consume("key:b")[0] is True


def test_sqlite_limiter_refills_with_wall_clock(monkeypatch, tmp_path) -> None:
    now = 1_000.0
    monkeypatch.setattr(time, "time", lambda: now)
    limiter = SQLiteRateLimiter(1, tmp_path / "rate-limit.db")
    assert limiter.consume("key:a") == (True, 0)
    assert limiter.consume("key:a")[0] is False

    now += 61.0
    assert limiter.consume("key:a")[0] is True


def test_zero_per_minute_disables_limiting(tmp_path) -> None:
    limiter = SQLiteRateLimiter(0, tmp_path / "rate-limit.db")
    for _ in range(50):
        assert limiter.consume("anything") == (True, 0)


def test_build_limiter_falls_back_to_memory_when_sqlite_cannot_open(tmp_path) -> None:
    directory = tmp_path / "not-a-database"
    directory.mkdir()
    limiter = build_limiter(5, "sqlite", directory)
    assert isinstance(limiter, RateLimiter)
    assert limiter.consume("key:a")[0] is True


def test_build_limiter_honours_memory_backend() -> None:
    limiter = build_limiter(5, "memory")
    assert isinstance(limiter, RateLimiter)
