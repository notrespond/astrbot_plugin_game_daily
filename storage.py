"""SQLite persistence for Game Daily.

All methods are synchronous and guarded by a re-entrant lock.  Callers in async
code should run them via ``asyncio.to_thread`` (the service/scheduler does this
through small helpers) so the bot event loop is never blocked by disk I/O.

The database stores:
* de-duplication keys for news that was actually pushed;
* run history for ``/gamedaily_status``;
* source health;
* small key/value metadata (scheduler job id, etc.);
* a crash-safe lease used to prevent concurrent scheduled/manual runs.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from .models import NewsItem, ReportResult, SourceReport, utcnow

SCHEMA_VERSION = 1


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


class Storage:
    def __init__(self, db_path: str | Path, logger=None) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        self.logger = logger
        self._open()

    # ------------------------------------------------------------------
    # Connection and schema
    # ------------------------------------------------------------------
    def _open(self) -> None:
        self._conn = sqlite3.connect(
            str(self.db_path),
            timeout=30.0,
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout = 30000")
        try:
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA synchronous = NORMAL")
        except sqlite3.DatabaseError:
            pass
        self._create_schema()

    def _create_schema(self) -> None:
        with self._lock:
            assert self._conn is not None
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sent_news (
                    key TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    news_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    url TEXT NOT NULL,
                    source TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    last_sent_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_sent_news_last_sent
                    ON sent_news(last_sent_at);

                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL,
                    total_collected INTEGER NOT NULL DEFAULT 0,
                    total_in_window INTEGER NOT NULL DEFAULT 0,
                    total_after_dedupe INTEGER NOT NULL DEFAULT 0,
                    total_selected INTEGER NOT NULL DEFAULT 0,
                    ai_used INTEGER NOT NULL DEFAULT 0,
                    sent_targets TEXT NOT NULL DEFAULT '[]',
                    failed_targets TEXT NOT NULL DEFAULT '[]',
                    source_reports TEXT NOT NULL DEFAULT '[]',
                    errors TEXT NOT NULL DEFAULT '[]',
                    items TEXT NOT NULL DEFAULT '[]',
                    poster_path TEXT,
                    caption TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_runs_started_at
                    ON runs(started_at);

                CREATE TABLE IF NOT EXISTS source_health (
                    source_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    last_ok_at TEXT,
                    last_error_at TEXT,
                    last_error TEXT NOT NULL DEFAULT '',
                    consecutive_failures INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS kv (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS locks (
                    name TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );
                """
            )
            self._conn.execute(
                "INSERT OR IGNORE INTO kv(key, value, updated_at) VALUES (?, ?, ?)",
                ("schema_version", str(SCHEMA_VERSION), _iso(utcnow())),
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                finally:
                    self._conn = None

    # ------------------------------------------------------------------
    # De-duplication
    # ------------------------------------------------------------------
    def find_sent_keys(self, keys: Iterable[str]) -> set[str]:
        keys = [key for key in dict.fromkeys(keys) if key]
        if not keys:
            return set()
        found: set[str] = set()
        with self._lock:
            assert self._conn is not None
            for start in range(0, len(keys), 400):
                chunk = keys[start : start + 400]
                placeholders = ",".join("?" for _ in chunk)
                rows = self._conn.execute(
                    f"SELECT key FROM sent_news WHERE key IN ({placeholders})",
                    chunk,
                ).fetchall()
                found.update(row["key"] for row in rows)
        return found

    def filter_unsent(self, items: list[NewsItem]) -> tuple[list[NewsItem], int]:
        """Return items that have no sent key, and the number dropped."""

        all_keys: list[str] = []
        item_keys: list[list[str]] = []
        for item in items:
            keys = item.dedupe_keys()
            item_keys.append(keys)
            all_keys.extend(keys)
        sent = self.find_sent_keys(all_keys)
        kept: list[NewsItem] = []
        dropped = 0
        for item, keys in zip(items, item_keys):
            if any(key in sent for key in keys):
                dropped += 1
            else:
                kept.append(item)
        return kept, dropped

    def mark_sent(self, items: list[NewsItem], now: datetime | None = None) -> None:
        if not items:
            return
        timestamp = _iso(now or utcnow())
        rows: list[tuple[str, str, str, str, str, str, str, str]] = []
        for item in items:
            for key in item.dedupe_keys():
                kind = key.split(":", 1)[0]
                rows.append(
                    (
                        key,
                        kind,
                        item.fingerprint,
                        item.raw_title[:500],
                        item.link[:1000],
                        item.source[:200],
                        timestamp,
                        timestamp,
                    )
                )
        if not rows:
            return
        with self._lock:
            assert self._conn is not None
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                self._conn.executemany(
                    """
                    INSERT INTO sent_news(
                        key, kind, news_id, title, url, source,
                        first_seen_at, last_sent_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        last_sent_at = excluded.last_sent_at,
                        title = excluded.title,
                        url = excluded.url,
                        source = excluded.source
                    """,
                    rows,
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def purge_sent(self, retention_days: int) -> int:
        cutoff = _iso(utcnow() - timedelta(days=max(1, retention_days)))
        with self._lock:
            assert self._conn is not None
            cursor = self._conn.execute(
                "DELETE FROM sent_news WHERE last_sent_at < ?", (cutoff,)
            )
            self._conn.commit()
            return int(cursor.rowcount or 0)

    # ------------------------------------------------------------------
    # Run history
    # ------------------------------------------------------------------
    def record_run(self, result: ReportResult, mode: str) -> None:
        items_payload = [
            {
                "title": item.display_title,
                "source": item.display_source,
                "category": item.category,
                "link": item.link,
                "importance": round(float(item.importance), 2),
            }
            for item in result.items[:12]
        ]
        source_payload = [
            {
                "source_id": report.source_id,
                "name": report.name,
                "ok": report.ok,
                "items": report.item_count,
                "error": report.error,
                "warnings": report.warnings,
            }
            for report in result.source_reports
        ]
        with self._lock:
            assert self._conn is not None
            self._conn.execute(
                """
                INSERT INTO runs(
                    mode, status, started_at, finished_at,
                    total_collected, total_in_window, total_after_dedupe,
                    total_selected, ai_used, sent_targets, failed_targets,
                    source_reports, errors, items, poster_path, caption
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    mode,
                    result.status,
                    _iso(result.started_at),
                    _iso(result.finished_at),
                    result.stats.total_collected,
                    result.stats.total_in_window,
                    result.stats.total_after_local_dedupe,
                    result.stats.total_selected,
                    1 if result.ai_used else 0,
                    json.dumps(result.sent_targets, ensure_ascii=False),
                    json.dumps(result.failed_targets, ensure_ascii=False),
                    json.dumps(source_payload, ensure_ascii=False),
                    json.dumps(result.errors, ensure_ascii=False),
                    json.dumps(items_payload, ensure_ascii=False),
                    result.poster_path,
                    result.caption,
                ),
            )
            self._conn.commit()

    def latest_run(self, modes: tuple[str, ...] = ("scheduled", "manual")) -> dict | None:
        if not modes:
            return None
        placeholders = ",".join("?" for _ in modes)
        with self._lock:
            assert self._conn is not None
            row = self._conn.execute(
                f"SELECT * FROM runs WHERE mode IN ({placeholders}) "
                "ORDER BY id DESC LIMIT 1",
                list(modes),
            ).fetchone()
        if not row:
            return None
        result = dict(row)
        for key in (
            "sent_targets",
            "failed_targets",
            "source_reports",
            "errors",
            "items",
        ):
            try:
                result[key] = json.loads(result.get(key) or "[]")
            except (TypeError, json.JSONDecodeError):
                result[key] = []
        return result

    def recent_runs(self, limit: int = 5) -> list[dict]:
        with self._lock:
            assert self._conn is not None
            rows = self._conn.execute(
                "SELECT id, mode, status, started_at, total_collected, "
                "total_selected FROM runs ORDER BY id DESC LIMIT ?",
                (max(1, min(50, limit)),),
            ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Source health
    # ------------------------------------------------------------------
    def record_source_health(self, reports: list[SourceReport]) -> None:
        if not reports:
            return
        now = _iso(utcnow())
        with self._lock:
            assert self._conn is not None
            for report in reports:
                if report.ok:
                    self._conn.execute(
                        """
                        INSERT INTO source_health(
                            source_id, name, last_ok_at, last_error_at,
                            last_error, consecutive_failures, updated_at
                        ) VALUES (?, ?, ?, NULL, '', 0, ?)
                        ON CONFLICT(source_id) DO UPDATE SET
                            name = excluded.name,
                            last_ok_at = excluded.last_ok_at,
                            consecutive_failures = 0,
                            updated_at = excluded.updated_at
                        """,
                        (report.source_id, report.name, now, now),
                    )
                else:
                    self._conn.execute(
                        """
                        INSERT INTO source_health(
                            source_id, name, last_ok_at, last_error_at,
                            last_error, consecutive_failures, updated_at
                        ) VALUES (?, ?, NULL, ?, ?, 1, ?)
                        ON CONFLICT(source_id) DO UPDATE SET
                            name = excluded.name,
                            last_error_at = excluded.last_error_at,
                            last_error = excluded.last_error,
                            consecutive_failures = source_health.consecutive_failures + 1,
                            updated_at = excluded.updated_at
                        """,
                        (report.source_id, report.name, now, report.error[:500], now),
                    )
            self._conn.commit()

    def source_health(self) -> list[dict]:
        with self._lock:
            assert self._conn is not None
            rows = self._conn.execute(
                "SELECT * FROM source_health ORDER BY name"
            ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Key/value metadata
    # ------------------------------------------------------------------
    def kv_get(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            assert self._conn is not None
            row = self._conn.execute(
                "SELECT value FROM kv WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row else default

    def kv_set(self, key: str, value: str) -> None:
        with self._lock:
            assert self._conn is not None
            self._conn.execute(
                """
                INSERT INTO kv(key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (key, value, _iso(utcnow())),
            )
            self._conn.commit()

    def kv_delete(self, key: str) -> None:
        with self._lock:
            assert self._conn is not None
            self._conn.execute("DELETE FROM kv WHERE key = ?", (key,))
            self._conn.commit()

    # ------------------------------------------------------------------
    # Cross-process run lease
    # ------------------------------------------------------------------
    def try_acquire_lock(self, name: str, owner: str, ttl_seconds: int = 1800) -> bool:
        now = utcnow()
        expires = now + timedelta(seconds=max(30, ttl_seconds))
        with self._lock:
            assert self._conn is not None
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute(
                    "SELECT owner, expires_at FROM locks WHERE name = ?", (name,)
                ).fetchone()
                if row is not None:
                    try:
                        current_expiry = datetime.fromisoformat(row["expires_at"])
                        if current_expiry.tzinfo is None:
                            current_expiry = current_expiry.replace(tzinfo=timezone.utc)
                    except ValueError:
                        current_expiry = now - timedelta(seconds=1)
                    if current_expiry > now:
                        self._conn.rollback()
                        return False
                self._conn.execute(
                    """
                    INSERT INTO locks(name, owner, expires_at) VALUES (?, ?, ?)
                    ON CONFLICT(name) DO UPDATE SET
                        owner = excluded.owner,
                        expires_at = excluded.expires_at
                    """,
                    (name, owner, _iso(expires)),
                )
                self._conn.commit()
                return True
            except Exception:
                self._conn.rollback()
                raise

    def release_lock(self, name: str, owner: str) -> None:
        with self._lock:
            assert self._conn is not None
            self._conn.execute(
                "DELETE FROM locks WHERE name = ? AND owner = ?", (name, owner)
            )
            self._conn.commit()

    def stats(self) -> dict:
        with self._lock:
            assert self._conn is not None
            sent = self._conn.execute(
                "SELECT COUNT(*) AS c FROM sent_news"
            ).fetchone()["c"]
            runs = self._conn.execute("SELECT COUNT(*) AS c FROM runs").fetchone()["c"]
        return {
            "db_path": str(self.db_path),
            "sent_keys": int(sent),
            "runs": int(runs),
        }
