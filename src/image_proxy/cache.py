"""Durable, time-limited storage for processed image artifacts."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import threading
import time

from image_proxy.config import CacheConfig


_CACHE_KEY = re.compile(r"[0-9a-f]{64}")
_PERSISTED_HEADERS = (
    "Cache-Control",
    "Expires",
    "Access-Control-Allow-Origin",
    "Access-Control-Allow-Credentials",
    "Cross-Origin-Resource-Policy",
    "Content-Disposition",
)
_SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
  cache_key TEXT PRIMARY KEY,
  source_url TEXT NOT NULL,
  processor_fingerprint TEXT NOT NULL,
  mime_type TEXT NOT NULL,
  artifact_path TEXT NOT NULL UNIQUE,
  response_headers_json TEXT NOT NULL,
  size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
  created_at REAL NOT NULL,
  expires_at REAL NOT NULL,
  last_accessed_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS entries_lru ON entries(last_accessed_at);
CREATE INDEX IF NOT EXISTS entries_expiry ON entries(expires_at);
"""


class CacheError(RuntimeError):
    """Raised when cache persistence cannot complete safely."""


@dataclass(frozen=True)
class CacheHit:
    data: bytes
    mime_type: str
    headers: dict[str, str]


@dataclass(frozen=True)
class CleanupReport:
    expired_count: int
    lru_count: int
    orphan_count: int
    bytes_freed: int


class CacheStore:
    """Stores cache metadata in SQLite and payloads as atomic disk artifacts."""

    def __init__(
        self, config: CacheConfig, *, clock: Callable[[], float] = time.time
    ) -> None:
        self._config = config
        self._clock = clock
        self._connection: sqlite3.Connection | None = None
        self._lock = threading.RLock()

    def initialize(self) -> None:
        """Create the cache directories, schema, and SQLite connection."""
        with self._lock:
            if self._connection is not None:
                return
            try:
                self._config.directory.mkdir(parents=True, exist_ok=True)
                self._artifacts_directory.mkdir(parents=True, exist_ok=True)
                connection = sqlite3.connect(
                    self._database_path, check_same_thread=False
                )
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA journal_mode=WAL")
                connection.executescript(_SCHEMA)
                connection.commit()
            except (OSError, sqlite3.Error) as exc:
                try:
                    connection.close()
                except (UnboundLocalError, sqlite3.Error):
                    pass
                raise CacheError("could not initialize cache storage") from exc
            self._connection = connection

    def get(self, key: str) -> CacheHit | None:
        """Return a valid cached artifact and update its last access time."""
        self._validate_key(key)
        with self._lock:
            connection = self._require_connection()
            try:
                row = connection.execute(
                    "SELECT * FROM entries WHERE cache_key = ?", (key,)
                ).fetchone()
                if row is None:
                    return None

                if self._now() >= row["expires_at"]:
                    self._remove_entry(connection, row)
                    return None

                artifact = self._artifact_absolute_path(row["artifact_path"])
                try:
                    data = artifact.read_bytes()
                except FileNotFoundError:
                    self._remove_entry(connection, row)
                    return None

                headers = json.loads(row["response_headers_json"])
                if not isinstance(headers, dict):
                    raise ValueError("stored headers are not an object")
                accessed_at = self._now()
                with connection:
                    connection.execute(
                        "UPDATE entries SET last_accessed_at = ? WHERE cache_key = ?",
                        (accessed_at, key),
                    )
                return CacheHit(
                    data=data,
                    mime_type=row["mime_type"],
                    headers=dict(headers),
                )
            except CacheError:
                raise
            except (OSError, sqlite3.Error, ValueError, json.JSONDecodeError) as exc:
                raise CacheError("could not read cached artifact") from exc

    def put(
        self,
        key: str,
        source_url: str,
        processor_fingerprint: str,
        mime_type: str,
        headers: Mapping[str, str],
        data: bytes,
    ) -> None:
        """Atomically write an artifact before committing its metadata."""
        self._validate_key(key)
        artifact_relative_path = self._artifact_relative_path(key)
        artifact = self._artifact_absolute_path(artifact_relative_path)
        persisted_headers = {
            name: headers[name] for name in _PERSISTED_HEADERS if name in headers
        }
        try:
            headers_json = json.dumps(persisted_headers, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise CacheError("could not serialize cache response headers") from exc

        with self._lock:
            connection = self._require_connection()
            temporary_path: Path | None = None
            artifact_replaced = False
            try:
                artifact.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(
                    mode="wb", dir=artifact.parent, prefix=".cache-", delete=False
                ) as temporary:
                    temporary_path = Path(temporary.name)
                    temporary.write(data)
                    temporary.flush()
                    os.fsync(temporary.fileno())
                os.replace(temporary_path, artifact)
                temporary_path = None
                artifact_replaced = True

                created_at = self._now()
                expires_at = created_at + self._config.ttl_seconds
                with connection:
                    connection.execute(
                        """
                        INSERT INTO entries (
                            cache_key, source_url, processor_fingerprint, mime_type,
                            artifact_path, response_headers_json, size_bytes, created_at,
                            expires_at, last_accessed_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(cache_key) DO UPDATE SET
                            source_url = excluded.source_url,
                            processor_fingerprint = excluded.processor_fingerprint,
                            mime_type = excluded.mime_type,
                            artifact_path = excluded.artifact_path,
                            response_headers_json = excluded.response_headers_json,
                            size_bytes = excluded.size_bytes,
                            created_at = excluded.created_at,
                            expires_at = excluded.expires_at,
                            last_accessed_at = excluded.last_accessed_at
                        """,
                        (
                            key,
                            source_url,
                            processor_fingerprint,
                            mime_type,
                            str(artifact_relative_path),
                            headers_json,
                            len(data),
                            created_at,
                            expires_at,
                            created_at,
                        ),
                    )
            except (OSError, sqlite3.Error) as exc:
                if temporary_path is not None:
                    try:
                        temporary_path.unlink(missing_ok=True)
                    except OSError:
                        pass
                if artifact_replaced:
                    try:
                        artifact.unlink(missing_ok=True)
                    except OSError:
                        pass
                raise CacheError("could not write cache artifact") from exc

            try:
                exceeds_maximum = (
                    self._total_size_bytes(connection) > self._config.max_size_bytes
                )
            except sqlite3.Error as exc:
                raise CacheError("could not calculate cache size") from exc
            if exceeds_maximum:
                self._cleanup_locked(connection)

    def cleanup(self) -> CleanupReport:
        """Remove expired, orphaned, and least-recently-used cache entries."""
        with self._lock:
            connection = self._require_connection()
            try:
                return self._cleanup_locked(connection)
            except CacheError:
                raise
            except (OSError, sqlite3.Error) as exc:
                raise CacheError("could not clean up cache storage") from exc

    def total_size_bytes(self) -> int:
        """Return the total size recorded by cache metadata."""
        with self._lock:
            connection = self._require_connection()
            try:
                return self._total_size_bytes(connection)
            except sqlite3.Error as exc:
                raise CacheError("could not calculate cache size") from exc

    def close(self) -> None:
        """Close the SQLite connection."""
        with self._lock:
            connection = self._connection
            if connection is None:
                return
            try:
                connection.close()
            except sqlite3.Error as exc:
                raise CacheError("could not close cache storage") from exc
            self._connection = None

    @property
    def _database_path(self) -> Path:
        return self._config.directory / "cache.sqlite3"

    @property
    def _artifacts_directory(self) -> Path:
        return self._config.directory / "artifacts"

    def _artifact_relative_path(self, key: str) -> Path:
        return Path("artifacts") / key[:2] / f"{key}.img"

    def _artifact_absolute_path(self, artifact_path: str | Path) -> Path:
        return self._config.directory / artifact_path

    def _remove_entry(self, connection: sqlite3.Connection, row: sqlite3.Row) -> None:
        with connection:
            connection.execute("DELETE FROM entries WHERE cache_key = ?", (row["cache_key"],))
        self._artifact_absolute_path(row["artifact_path"]).unlink(missing_ok=True)

    def _cleanup_locked(self, connection: sqlite3.Connection) -> CleanupReport:
        expired_count = 0
        lru_count = 0
        orphan_count = 0
        bytes_freed = 0

        try:
            expired_rows = connection.execute(
                "SELECT * FROM entries WHERE expires_at <= ?", (self._now(),)
            ).fetchall()
            for row in expired_rows:
                self._remove_entry(connection, row)
                expired_count += 1
                bytes_freed += row["size_bytes"]

            tracked_artifacts = {
                row["artifact_path"]
                for row in connection.execute("SELECT artifact_path FROM entries")
            }
            for artifact in self._artifacts_directory.rglob("*.img"):
                relative_path = str(artifact.relative_to(self._config.directory))
                if relative_path in tracked_artifacts:
                    continue
                try:
                    size_bytes = artifact.stat().st_size
                    artifact.unlink()
                except FileNotFoundError:
                    continue
                orphan_count += 1
                bytes_freed += size_bytes

            total_size = self._total_size_bytes(connection)
            if total_size > self._config.max_size_bytes:
                low_watermark = int(
                    self._config.max_size_bytes * self._config.low_watermark_ratio
                )
                while total_size > low_watermark:
                    lru_rows = connection.execute(
                        """
                        SELECT * FROM entries
                        ORDER BY last_accessed_at ASC, cache_key ASC
                        LIMIT ?
                        """,
                        (self._config.eviction_batch_size,),
                    ).fetchall()
                    if not lru_rows:
                        break

                    with connection:
                        connection.executemany(
                            "DELETE FROM entries WHERE cache_key = ?",
                            ((row["cache_key"],) for row in lru_rows),
                        )
                    for row in lru_rows:
                        self._artifact_absolute_path(row["artifact_path"]).unlink(
                            missing_ok=True
                        )

                    batch_size = sum(row["size_bytes"] for row in lru_rows)
                    total_size -= batch_size
                    lru_count += len(lru_rows)
                    bytes_freed += batch_size
        except (OSError, sqlite3.Error) as exc:
            raise CacheError("could not clean up cache storage") from exc

        return CleanupReport(expired_count, lru_count, orphan_count, bytes_freed)

    @staticmethod
    def _total_size_bytes(connection: sqlite3.Connection) -> int:
        total_size = connection.execute(
            "SELECT COALESCE(SUM(size_bytes), 0) FROM entries"
        ).fetchone()[0]
        return int(total_size)

    def _require_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise CacheError("cache storage is not initialized")
        return self._connection

    def _now(self) -> float:
        return float(self._clock())

    @staticmethod
    def _validate_key(key: str) -> None:
        if not isinstance(key, str) or _CACHE_KEY.fullmatch(key) is None:
            raise ValueError("cache key must be a lowercase 64-character hexadecimal string")
