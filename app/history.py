# SPDX-License-Identifier: GPL-3.0-or-later
"""Job-history storage backends.

History is not just an audit trail: it is the substrate for idempotency de-duplication
(:func:`HistoryStore.find_idempotent`) and ``/reprint`` (:func:`HistoryStore.get`). The chosen
backend therefore changes behaviour, not just durability — see ``docs/known-limitations.md``:

* ``file``     — durable SQLite file; dedup/reprint survive restarts.
* ``memory``   — in-process SQLite (``:memory:``); dedup/reprint reset when the process exits.
* ``disabled`` — :class:`NullHistoryStore`; no dedup, no reprint.

Records are stored as the full ``PrintJobRecord`` JSON in a ``record`` column, with ``job_id`` /
``idempotency_key`` / ``status`` mirrored into indexed columns for O(log n) lookup. Keeping the
pydantic model as the source of truth means new model fields need no schema migration — provided
they have defaults (see CONTRIBUTING.md). Each row also carries a ``format_version`` column
(:data:`HISTORY_FORMAT_VERSION`) so a breaking model change can be detected instead of exploding:
rows that fail validation or come from a newer format are skipped with a warning, never raised
into the request path (worst case for dedup: one duplicate print instead of a 500).
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from typing import Protocol, runtime_checkable

from pydantic import ValidationError

from app.config import Settings
from app.models import PrintJobRecord

log = logging.getLogger(__name__)

_IN_MEMORY = ":memory:"

HISTORY_FORMAT_VERSION = 1
"""Format version stamped on every persisted row.

Bump only for a breaking ``PrintJobRecord`` change (required field, rename, semantic change);
additive fields with defaults need no bump. Rows with a NULL version (written before the column
existed) are treated as version 1; rows newer than this constant are skipped on read.
"""


def _load_row(job_id: str, record_json: str, format_version: int | None) -> PrintJobRecord | None:
    """Deserialize one history row, returning ``None`` (with one WARNING) if it is unreadable.

    Tolerant by design: history reads sit on request paths (``/reprint``, ``/history``, keyed
    ``/print``), so a row from a newer format (downgrade) or one that fails validation (upgrade
    that changed the model) must degrade to a miss, not a 500.
    """
    version = format_version if format_version is not None else 1
    if version > HISTORY_FORMAT_VERSION:
        log.warning(
            "Skipping history row for job %s: format_version %d is newer than supported %d "
            "(written by a newer labelito?)",
            job_id,
            version,
            HISTORY_FORMAT_VERSION,
        )
        return None
    try:
        return PrintJobRecord.model_validate_json(record_json)
    except ValidationError as exc:
        log.warning("Skipping unreadable history row for job %s: %s", job_id, exc)
        return None


@runtime_checkable
class HistoryStore(Protocol):
    """Pluggable job-history backend (see module docstring)."""

    def save(self, record: PrintJobRecord) -> None:
        """Persist a job record. May raise ``OSError``/``sqlite3.Error`` on I/O failure."""
        ...

    def get(self, job_id: str) -> PrintJobRecord | None:
        """Return the most recent record for ``job_id`` (for ``/reprint``), or ``None``."""
        ...

    def find_idempotent(self, key: str) -> PrintJobRecord | None:
        """Return the most recent non-failed record under ``key`` (retry de-dup), or ``None``."""
        ...

    def recent(self, limit: int) -> list[PrintJobRecord]:
        """Return up to ``limit`` records, newest first."""
        ...

    def page(self, *, offset: int, limit: int) -> list[PrintJobRecord]:
        """Return a newest-first slice of history for the paginated browse UI."""
        ...

    def count(self) -> int:
        """Total records retained (drives the browse UI's pagination controls)."""
        ...

    def delete(self, job_id: str) -> bool:
        """Delete every row for ``job_id``. Return ``True`` if any row was removed.

        This is a deliberate, user-initiated exception to the otherwise append-only-until-prune
        model: because the same rows back ``get`` (reprint) and ``find_idempotent`` (dedup),
        deleting an entry makes that job unreprintable and, if it carried an ``idempotency_key``,
        lets a later retry under that key print again. Acceptable for the single-user browse UI
        where the operator is the one removing the entry; not an audit-log guarantee.
        """
        ...

    def close(self) -> None:
        """Release any underlying resources."""
        ...


class SqliteHistoryStore:
    """SQLite-backed history, usable as a file or an in-memory (``:memory:``) database.

    A single connection is held for the store's lifetime — mandatory for ``:memory:`` (a new
    connection would see a fresh, empty database) and fine for a file under the single-worker
    assumption. ``check_same_thread=False`` plus an internal lock make it safe across the event
    loop thread (reads) and ``run_in_threadpool`` worker threads (writes).
    """

    def __init__(self, database: str, *, keep: int, prune_at: int) -> None:
        if keep <= 0:
            raise ValueError(f"history keep entries must be > 0, got {keep}")
        if prune_at <= keep:
            raise ValueError(
                f"history prune-at entries ({prune_at}) must be greater than keep ({keep})"
            )
        self._keep = keep
        self._prune_at = prune_at
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(database, check_same_thread=False)
        if database != _IN_MEMORY:
            # WAL improves read/write concurrency and crash durability for the on-disk file.
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id          TEXT NOT NULL,
                idempotency_key TEXT,
                status          TEXT NOT NULL,
                record          TEXT NOT NULL,
                format_version  INTEGER
            )
            """
        )
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(jobs)")}
        if "format_version" not in columns:
            # Legacy DB created before versioning: ADD COLUMN is cheap in SQLite and leaves
            # existing rows NULL, which reads treat as version 1.
            self._conn.execute("ALTER TABLE jobs ADD COLUMN format_version INTEGER")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_job_id ON jobs(job_id)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_idem ON jobs(idempotency_key)")
        self._conn.commit()

    def save(self, record: PrintJobRecord) -> None:
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO jobs (job_id, idempotency_key, status, record, format_version) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        record.job_id,
                        record.idempotency_key,
                        record.status,
                        record.model_dump_json(),
                        HISTORY_FORMAT_VERSION,
                    ),
                )
                self._prune()
                self._conn.commit()
            except (OSError, sqlite3.Error):
                # Roll back so a failed mutation never lingers as an open transaction on the
                # shared connection, where a later commit could flush it. Re-raise for the caller.
                self._conn.rollback()
                raise

    def _prune(self) -> None:
        """Drop the oldest rows once the table exceeds the high-water mark.

        Pruning only when over ``prune_at`` (down to ``keep``) gives hysteresis — the delete runs
        once per ``prune_at - keep`` inserts, not on every insert. Ordering is by ``id``
        (insertion order), which is monotonic because ``AUTOINCREMENT`` never reuses ids.
        """
        (count,) = self._conn.execute("SELECT COUNT(*) FROM jobs").fetchone()
        if count <= self._prune_at:
            return
        self._conn.execute(
            "DELETE FROM jobs WHERE id NOT IN (SELECT id FROM jobs ORDER BY id DESC LIMIT ?)",
            (self._keep,),
        )

    def get(self, job_id: str) -> PrintJobRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT job_id, record, format_version FROM jobs "
                "WHERE job_id = ? ORDER BY id DESC LIMIT 1",
                (job_id,),
            ).fetchone()
        return _load_row(*row) if row else None

    def find_idempotent(self, key: str) -> PrintJobRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT job_id, record, format_version FROM jobs "
                "WHERE idempotency_key = ? AND status != 'failed' ORDER BY id DESC LIMIT 1",
                (key,),
            ).fetchone()
        # An unreadable row degrades to a dedup miss: a duplicate print beats a 500.
        return _load_row(*row) if row else None

    def recent(self, limit: int) -> list[PrintJobRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT job_id, record, format_version FROM jobs ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [record for r in rows if (record := _load_row(*r)) is not None]

    def page(self, *, offset: int, limit: int) -> list[PrintJobRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT job_id, record, format_version FROM jobs ORDER BY id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [record for r in rows if (record := _load_row(*r)) is not None]

    def count(self) -> int:
        with self._lock:
            (count,) = self._conn.execute("SELECT COUNT(*) FROM jobs").fetchone()
        return int(count)

    def delete(self, job_id: str) -> bool:
        with self._lock:
            try:
                cursor = self._conn.execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))
                self._conn.commit()
            except (OSError, sqlite3.Error):
                # Roll back so a commit failure on this privacy-facing delete cannot leave a
                # pending DELETE on the shared connection that a later commit would apply.
                self._conn.rollback()
                raise
        return cursor.rowcount > 0

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class NullHistoryStore:
    """Disabled history: writes are dropped, lookups always miss.

    Consequence: idempotency de-duplication is off (keyed retries reprint) and ``/reprint`` always
    404s. This is the explicit ``HISTORY_MODE=disabled`` opt-out.
    """

    def save(self, record: PrintJobRecord) -> None:
        return None

    def get(self, job_id: str) -> PrintJobRecord | None:
        return None

    def find_idempotent(self, key: str) -> PrintJobRecord | None:
        return None

    def recent(self, limit: int) -> list[PrintJobRecord]:
        return []

    def page(self, *, offset: int, limit: int) -> list[PrintJobRecord]:
        return []

    def count(self) -> int:
        return 0

    def delete(self, job_id: str) -> bool:
        return False

    def close(self) -> None:
        return None


def build_history_store(settings: Settings) -> HistoryStore:
    """Construct the history backend selected by ``settings.history_mode`` (fail-fast on error)."""
    mode = settings.history_mode
    if mode == "disabled":
        return NullHistoryStore()
    keep = settings.history_keep_entries
    prune_at = settings.history_prune_at_entries
    if mode == "memory":
        return SqliteHistoryStore(_IN_MEMORY, keep=keep, prune_at=prune_at)
    if mode == "file":
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        db_path = settings.data_dir / "history.db"
        return SqliteHistoryStore(str(db_path), keep=keep, prune_at=prune_at)
    raise ValueError(f"Unknown history_mode {mode!r}; expected file, memory, or disabled")


__all__ = [
    "HISTORY_FORMAT_VERSION",
    "HistoryStore",
    "NullHistoryStore",
    "SqliteHistoryStore",
    "build_history_store",
]
