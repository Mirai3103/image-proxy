import sqlite3
from pathlib import Path

from image_proxy.cache import CacheStore
from image_proxy.config import CacheConfig


class Clock:
    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def store(tmp_path: Path, clock: Clock, ttl: int = 60) -> CacheStore:
    cache = CacheStore(
        CacheConfig(tmp_path, ttl, 10_000, 0.9, 600, 2), clock=clock
    )
    cache.initialize()
    return cache


def test_put_get_updates_access_without_extending_absolute_ttl(tmp_path: Path) -> None:
    clock = Clock()
    cache = store(tmp_path, clock)
    key = "a" * 64
    cache.put(
        key,
        "https://cdn.test/a.jpg",
        "fp",
        "image/jpeg",
        {"Cache-Control": "max-age=60"},
        b"processed",
    )

    clock.now = 1_030.0
    assert cache.get(key).data == b"processed"
    with sqlite3.connect(tmp_path / "cache.sqlite3") as database:
        row = database.execute(
            "SELECT created_at, expires_at, last_accessed_at FROM entries WHERE cache_key = ?",
            (key,),
        ).fetchone()
    assert tuple(row) == (1_000.0, 1_060.0, 1_030.0)

    clock.now = 1_061.0
    assert cache.get(key) is None
    assert not list((tmp_path / "artifacts").rglob("*.img"))


def test_missing_artifact_removes_broken_metadata(tmp_path: Path) -> None:
    clock = Clock()
    cache = store(tmp_path, clock)
    key = "b" * 64
    cache.put(key, "https://cdn.test/b.webp", "fp", "image/webp", {}, b"processed")
    next((tmp_path / "artifacts").rglob("*.img")).unlink()

    assert cache.get(key) is None

    with sqlite3.connect(tmp_path / "cache.sqlite3") as database:
        count = database.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
    assert count == 0
