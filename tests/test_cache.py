import sqlite3
from pathlib import Path

import pytest

from image_proxy.cache import CacheError, CacheStore
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


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("response_headers_json", "{"),
        ("response_headers_json", '{"Cache-Control":7}'),
        ("size_bytes", "wrong-size"),
        ("expires_at", "not-a-timestamp"),
    ],
)
def test_get_lazily_purges_corrupt_stored_metadata(
    tmp_path: Path, column: str, value: object
) -> None:
    clock = Clock()
    cache = store(tmp_path, clock)
    key = "e" * 64
    cache.put(key, "https://cdn.test/e.jpg", "fp", "image/jpeg", {}, b"processed")
    artifact = tmp_path / "artifacts" / "ee" / (key + ".img")
    connection = cache._connection
    assert connection is not None
    with connection:
        connection.execute(
            f"UPDATE entries SET {column} = ? WHERE cache_key = ?", (value, key)
        )

    assert cache.get(key) is None
    assert not artifact.exists()
    with sqlite3.connect(tmp_path / "cache.sqlite3") as database:
        count = database.execute(
            "SELECT COUNT(*) FROM entries WHERE cache_key = ?", (key,)
        ).fetchone()[0]
    assert count == 0


def test_get_lazily_purges_artifact_with_wrong_recorded_size(
    tmp_path: Path,
) -> None:
    clock = Clock()
    cache = store(tmp_path, clock)
    key = "f" * 64
    cache.put(key, "https://cdn.test/f.jpg", "fp", "image/jpeg", {}, b"processed")
    artifact = tmp_path / "artifacts" / "ff" / (key + ".img")
    artifact.write_bytes(b"tampered-artifact")

    assert cache.get(key) is None
    assert not artifact.exists()
    with sqlite3.connect(tmp_path / "cache.sqlite3") as database:
        count = database.execute(
            "SELECT COUNT(*) FROM entries WHERE cache_key = ?", (key,)
        ).fetchone()[0]
    assert count == 0


def test_get_removes_canonical_artifact_directory_without_following_symlinks(
    tmp_path: Path,
) -> None:
    clock = Clock()
    cache = store(tmp_path, clock)
    key = "2" * 64
    cache.put(key, "https://cdn.test/two.jpg", "fp", "image/jpeg", {}, b"old")
    artifact = tmp_path / "artifacts" / "22" / (key + ".img")
    artifact.unlink()
    artifact.mkdir()
    (artifact / "nested").mkdir()
    (artifact / "nested" / "poison").write_bytes(b"poison")
    outside_sentinel = tmp_path / "outside-sentinel"
    outside_sentinel.write_bytes(b"outside-must-survive")
    (artifact / "outside-link").symlink_to(outside_sentinel)

    assert cache.get(key) is None
    assert not artifact.exists()
    assert outside_sentinel.read_bytes() == b"outside-must-survive"
    assert cache.get(key) is None

    cache.put(key, "https://cdn.test/two.jpg", "fp", "image/jpeg", {}, b"new")
    hit = cache.get(key)
    assert hit is not None
    assert hit.data == b"new"


def test_get_rejects_invalid_artifact_layout_without_deleting_outside_tree(
    tmp_path: Path,
) -> None:
    clock = Clock()
    cache = store(tmp_path, clock)
    key = "1" * 64
    cache.put(key, "https://cdn.test/one.jpg", "fp", "image/jpeg", {}, b"processed")
    expected_artifact = tmp_path / "artifacts" / "11" / (key + ".img")
    outside_artifact = tmp_path / "outside.img"
    outside_artifact.write_bytes(b"outside-must-survive")
    connection = cache._connection
    assert connection is not None
    with connection:
        connection.execute(
            "UPDATE entries SET artifact_path = ? WHERE cache_key = ?",
            ("artifacts/../outside.img", key),
        )

    assert cache.get(key) is None
    assert outside_artifact.read_bytes() == b"outside-must-survive"
    assert not expected_artifact.exists()
    with sqlite3.connect(tmp_path / "cache.sqlite3") as database:
        count = database.execute(
            "SELECT COUNT(*) FROM entries WHERE cache_key = ?", (key,)
        ).fetchone()[0]
    assert count == 0


def test_failed_metadata_write_after_replacement_never_serves_stale_metadata(
    tmp_path: Path,
) -> None:
    clock = Clock()
    cache = store(tmp_path, clock)
    key = "c" * 64
    cache.put(
        key,
        "https://cdn.test/old.jpg",
        "old-fingerprint",
        "image/jpeg",
        {"Cache-Control": "max-age=60"},
        b"old-bytes",
    )

    connection = cache._connection
    assert connection is not None
    deny_metadata_writes = True

    def deny_entry_insert(
        action: int,
        first_argument: str | None,
        second_argument: str | None,
        database_name: str | None,
        trigger_name: str | None,
    ) -> int:
        if (
            deny_metadata_writes
            and action == sqlite3.SQLITE_INSERT
            and first_argument == "entries"
        ):
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    connection.set_authorizer(deny_entry_insert)
    with pytest.raises(CacheError, match="could not write cache artifact"):
        cache.put(
            key,
            "https://cdn.test/new.webp",
            "new-fingerprint",
            "image/webp",
            {"Content-Disposition": "inline"},
            b"new-bytes",
        )
    deny_metadata_writes = False

    assert cache.get(key) is None
    assert not list((tmp_path / "artifacts").rglob("*.img"))
    with sqlite3.connect(tmp_path / "cache.sqlite3") as database:
        count = database.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
    assert count == 0


def test_cleanup_evicts_oldest_in_batches_to_low_watermark(tmp_path: Path) -> None:
    clock = Clock()
    writer = CacheStore(CacheConfig(tmp_path, 600, 100, 0.6, 600, 1), clock=clock)
    writer.initialize()
    for index, key_char in enumerate(("a", "b", "c", "d")):
        clock.now = 1_000 + index
        writer.put(key_char * 64, f"https://cdn/{key_char}", "fp", "image/jpeg", {}, b"xxxx")
    writer.close()

    cache = CacheStore(CacheConfig(tmp_path, 600, 10, 0.6, 600, 1), clock=clock)
    cache.initialize()

    report = cache.cleanup()

    assert report.lru_count == 3
    assert cache.total_size_bytes() == 4
    assert cache.get("d" * 64) is not None
    assert cache.get("a" * 64) is None


def test_put_triggers_cleanup_after_crossing_maximum(tmp_path: Path) -> None:
    clock = Clock()
    cache = CacheStore(CacheConfig(tmp_path, 600, 10, 0.6, 600, 1), clock=clock)
    cache.initialize()
    for key_char in ("a", "b", "c"):
        clock.now += 1
        cache.put(key_char * 64, f"https://cdn/{key_char}", "fp", "image/jpeg", {}, b"xxxx")
    assert cache.total_size_bytes() == 4
    assert cache.get("c" * 64) is not None
    assert cache.get("a" * 64) is None


def test_cleanup_removes_expired_rows_and_orphan_files(tmp_path: Path) -> None:
    clock = Clock()
    cache = store(tmp_path, clock, ttl=10)
    cache.put("a" * 64, "https://cdn/a", "fp", "image/jpeg", {}, b"old")
    orphan = tmp_path / "artifacts" / "ff" / ("f" * 64 + ".img")
    orphan.parent.mkdir(parents=True)
    orphan.write_bytes(b"orphan")
    clock.now += 11

    report = cache.cleanup()

    assert report.expired_count == 1
    assert report.orphan_count == 1
    assert cache.total_size_bytes() == 0
    assert not orphan.exists()


def test_cleanup_removes_crash_temps_and_counts_all_orphan_bytes(
    tmp_path: Path,
) -> None:
    clock = Clock()
    cache = store(tmp_path, clock)
    key = "a" * 64
    cache.put(key, "https://cdn/a", "fp", "image/jpeg", {}, b"tracked")
    artifacts = tmp_path / "artifacts"
    first_temp = artifacts / "aa" / ".cache-crash-one"
    second_temp = artifacts / "bb" / ".cache-crash-two"
    orphan = artifacts / "cc" / ("c" * 64 + ".img")
    for path, data in (
        (first_temp, b"temporary-one"),
        (second_temp, b"two"),
        (orphan, b"orphan"),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    report = cache.cleanup()

    assert report.orphan_count == 3
    assert report.bytes_freed == (
        len(b"temporary-one") + len(b"two") + len(b"orphan")
    )
    assert not first_temp.exists()
    assert not second_temp.exists()
    assert not orphan.exists()
    assert cache.get(key).data == b"tracked"


def test_cleanup_removes_canonical_artifact_directory_without_following_symlinks(
    tmp_path: Path,
) -> None:
    clock = Clock()
    cache = store(tmp_path, clock)
    key = "3" * 64
    cache.put(key, "https://cdn.test/three.jpg", "fp", "image/jpeg", {}, b"old")
    artifact = tmp_path / "artifacts" / "33" / (key + ".img")
    artifact.unlink()
    artifact.mkdir()
    (artifact / "nested").mkdir()
    (artifact / "nested" / "poison").write_bytes(b"poison")
    outside_sentinel = tmp_path / "outside-sentinel"
    outside_sentinel.write_bytes(b"outside-must-survive")
    (artifact / "outside-link").symlink_to(outside_sentinel)

    report = cache.cleanup()

    assert report.orphan_count == 1
    assert not artifact.exists()
    assert outside_sentinel.read_bytes() == b"outside-must-survive"
    assert cache.get(key) is None
    assert cache.cleanup().orphan_count == 0


def test_cleanup_preserves_expired_artifact_when_metadata_delete_fails(
    tmp_path: Path,
) -> None:
    clock = Clock()
    cache = store(tmp_path, clock, ttl=10)
    key = "a" * 64
    cache.put(key, "https://cdn/a", "fp", "image/jpeg", {}, b"old")
    artifact = tmp_path / "artifacts" / "aa" / (key + ".img")
    clock.now += 11

    connection = cache._connection
    assert connection is not None

    def deny_entry_delete(
        action: int,
        first_argument: str | None,
        second_argument: str | None,
        database_name: str | None,
        trigger_name: str | None,
    ) -> int:
        if action == sqlite3.SQLITE_DELETE and first_argument == "entries":
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    connection.set_authorizer(deny_entry_delete)
    with pytest.raises(CacheError, match="could not clean up cache storage"):
        cache.cleanup()
    connection.set_authorizer(None)

    with sqlite3.connect(tmp_path / "cache.sqlite3") as database:
        count = database.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
    assert count == 1
    assert artifact.read_bytes() == b"old"


def test_cleanup_commits_lru_batch_before_failed_artifact_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = Clock()
    writer = CacheStore(CacheConfig(tmp_path, 600, 100, 0.6, 600, 2), clock=clock)
    writer.initialize()
    for index, key_char in enumerate(("a", "b", "c")):
        clock.now += 1
        writer.put(key_char * 64, f"https://cdn/{key_char}", "fp", "image/jpeg", {}, b"xxxx")
    writer.close()

    cache = CacheStore(CacheConfig(tmp_path, 600, 10, 0.6, 600, 2), clock=clock)
    cache.initialize()
    failed_artifact = tmp_path / "artifacts" / "bb" / ("b" * 64 + ".img")
    original_unlink = Path.unlink

    def fail_selected_unlink(path: Path, *args: object, **kwargs: object) -> None:
        if path == failed_artifact:
            raise OSError("simulated artifact deletion failure")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_selected_unlink)
    with pytest.raises(CacheError, match="could not clean up cache storage"):
        cache.cleanup()
    monkeypatch.setattr(Path, "unlink", original_unlink)

    with sqlite3.connect(tmp_path / "cache.sqlite3") as database:
        remaining_keys = {
            row[0] for row in database.execute("SELECT cache_key FROM entries")
        }
    assert remaining_keys == {"c" * 64}
    assert failed_artifact.read_bytes() == b"xxxx"
    assert not (tmp_path / "artifacts" / "aa" / ("a" * 64 + ".img")).exists()

    report = cache.cleanup()
    assert report.expired_count == 0
    assert report.lru_count == 0
    assert report.orphan_count == 1
    assert report.bytes_freed == 4


def test_put_translates_post_commit_size_query_failures_without_removing_entry(
    tmp_path: Path,
) -> None:
    clock = Clock()
    cache = store(tmp_path, clock)
    key = "d" * 64
    connection = cache._connection
    assert connection is not None

    def deny_sum(
        action: int,
        first_argument: str | None,
        second_argument: str | None,
        database_name: str | None,
        trigger_name: str | None,
    ) -> int:
        if action == sqlite3.SQLITE_FUNCTION and second_argument == "sum":
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    connection.set_authorizer(deny_sum)
    with pytest.raises(CacheError, match="could not calculate cache size"):
        cache.put(key, "https://cdn/d", "fp", "image/jpeg", {}, b"saved")
    connection.set_authorizer(None)

    artifact = tmp_path / "artifacts" / "dd" / (key + ".img")
    assert artifact.read_bytes() == b"saved"
    with sqlite3.connect(tmp_path / "cache.sqlite3") as database:
        row = database.execute(
            "SELECT cache_key, size_bytes FROM entries WHERE cache_key = ?", (key,)
        ).fetchone()
    assert row == (key, 5)
