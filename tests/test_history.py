# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the job-history storage backends."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.history import (
    HISTORY_FORMAT_VERSION,
    NullHistoryStore,
    SqliteHistoryStore,
    build_history_store,
)
from app.models import PrintJobRecord


class _CommitFailsConnection:
    """Wraps a real sqlite3 connection but raises on commit, to exercise rollback-on-error.

    sqlite3.Connection forbids attribute assignment, so a commit failure can't be monkeypatched
    directly; this proxy delegates everything except commit (which fails) and records rollback.
    """

    def __init__(self, real: sqlite3.Connection) -> None:
        self._real = real
        self.rolled_back = False

    def execute(self, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
        return self._real.execute(*args, **kwargs)

    def commit(self) -> None:
        raise sqlite3.OperationalError("commit boom")

    def rollback(self) -> None:
        self.rolled_back = True
        self._real.rollback()

    def close(self) -> None:
        self._real.close()


def _record(job_id: str, *, key: str | None = None, status: str = "printed") -> PrintJobRecord:
    return PrintJobRecord(
        job_id=job_id,
        template="simple",
        fields={"title": "X"},
        copies=1,
        dry_run=False,
        timestamp="2026-06-24T00:00:00",
        idempotency_key=key,
        status=status,
    )


@pytest.fixture
def store() -> Iterator[SqliteHistoryStore]:
    s = SqliteHistoryStore(":memory:", keep=1000, prune_at=1500)
    yield s
    s.close()


def test_save_and_get_roundtrip(store: SqliteHistoryStore) -> None:
    store.save(_record("job-1"))
    got = store.get("job-1")
    assert got is not None and got.job_id == "job-1" and got.template == "simple"


def test_get_missing_returns_none(store: SqliteHistoryStore) -> None:
    assert store.get("nope") is None


def test_file_backend_roundtrip(tmp_path: Path) -> None:
    db = tmp_path / "history.db"
    s = SqliteHistoryStore(str(db), keep=1000, prune_at=1500)
    try:
        s.save(_record("job-file"))
        assert s.get("job-file") is not None
        assert db.exists()
    finally:
        s.close()


def test_find_idempotent_returns_most_recent_nonfailed(store: SqliteHistoryStore) -> None:
    store.save(_record("job-1", key="k"))
    store.save(_record("job-2", key="k"))
    found = store.find_idempotent("k")
    assert found is not None and found.job_id == "job-2"  # most recent wins


def test_find_idempotent_skips_failed(store: SqliteHistoryStore) -> None:
    store.save(_record("job-ok", key="k"))
    store.save(_record("job-bad", key="k", status="failed"))
    found = store.find_idempotent("k")
    assert found is not None and found.job_id == "job-ok"  # failed row is ignored


def test_find_idempotent_unknown_key(store: SqliteHistoryStore) -> None:
    assert store.find_idempotent("absent") is None


def test_recent_is_newest_first(store: SqliteHistoryStore) -> None:
    for i in range(5):
        store.save(_record(f"job-{i}"))
    recent = store.recent(3)
    assert [r.job_id for r in recent] == ["job-4", "job-3", "job-2"]


def test_page_is_newest_first_and_slices(store: SqliteHistoryStore) -> None:
    for i in range(5):
        store.save(_record(f"job-{i}"))
    first = store.page(offset=0, limit=2)
    second = store.page(offset=2, limit=2)
    assert [r.job_id for r in first] == ["job-4", "job-3"]
    assert [r.job_id for r in second] == ["job-2", "job-1"]


def test_page_offset_past_end_is_empty(store: SqliteHistoryStore) -> None:
    store.save(_record("job-0"))
    assert store.page(offset=10, limit=20) == []


def test_count_reflects_inserts_and_prune() -> None:
    s = SqliteHistoryStore(":memory:", keep=2, prune_at=4)
    try:
        for i in range(4):
            s.save(_record(f"job-{i}"))
        assert s.count() == 4  # at the high-water mark, not yet over
        s.save(_record("job-4"))  # 5 > prune_at(4) → prune down to keep(2)
        assert s.count() == 2
    finally:
        s.close()


def test_delete_removes_row_and_reports_hit(store: SqliteHistoryStore) -> None:
    store.save(_record("job-1"))
    store.save(_record("job-2"))
    assert store.delete("job-1") is True
    assert store.get("job-1") is None
    assert store.delete("job-1") is False  # already gone → no row hit
    assert [r.job_id for r in store.recent(10)] == ["job-2"]  # the other survives


def test_delete_rolls_back_when_commit_fails(store: SqliteHistoryStore) -> None:
    """A commit failure mid-delete must roll back, leaving the row visible — not silently gone."""
    store.save(_record("job-1"))
    store._conn = _CommitFailsConnection(store._conn)  # type: ignore[assignment]
    with pytest.raises(sqlite3.Error):
        store.delete("job-1")
    assert store._conn.rolled_back is True  # type: ignore[attr-defined]
    assert store.get("job-1") is not None  # DELETE was undone, not left pending


def test_save_rolls_back_when_commit_fails(store: SqliteHistoryStore) -> None:
    """A commit failure mid-save must roll back so the uncommitted INSERT does not linger."""
    store.save(_record("seed"))
    store._conn = _CommitFailsConnection(store._conn)  # type: ignore[assignment]
    with pytest.raises(sqlite3.Error):
        store.save(_record("job-rollback"))
    assert store._conn.rolled_back is True  # type: ignore[attr-defined]
    assert store.get("job-rollback") is None  # INSERT was undone
    assert store.get("seed") is not None  # the earlier committed row is intact


def test_pruning_hysteresis() -> None:
    """No prune at exactly prune_at; over it, the table drops to keep, newest retained."""
    s = SqliteHistoryStore(":memory:", keep=2, prune_at=4)
    try:
        for i in range(4):
            s.save(_record(f"job-{i}"))
        assert len(s.recent(100)) == 4  # at the high-water mark, not yet over → no prune

        s.save(_record("job-4"))  # now 5 > prune_at(4) → prune down to keep(2)
        remaining = s.recent(100)
        assert [r.job_id for r in remaining] == ["job-4", "job-3"]  # two newest survive
    finally:
        s.close()


def test_pruned_job_is_unreachable() -> None:
    s = SqliteHistoryStore(":memory:", keep=2, prune_at=4)
    try:
        for i in range(5):
            s.save(_record(f"job-{i}"))
        assert s.get("job-0") is None  # oldest pruned away
        assert s.get("job-4") is not None
    finally:
        s.close()


# ── Format versioning: unreadable rows degrade to misses, never 500s ──────────────


def _insert_raw_row(
    store: SqliteHistoryStore,
    *,
    job_id: str,
    record_json: str,
    format_version: int | None,
    key: str | None = None,
) -> None:
    """Bypass save() to plant a row as a corrupted/foreign writer would leave it."""
    store._conn.execute(
        "INSERT INTO jobs (job_id, idempotency_key, status, record, format_version) "
        "VALUES (?, ?, 'printed', ?, ?)",
        (job_id, key, record_json, format_version),
    )
    store._conn.commit()


def test_corrupted_row_is_skipped_not_raised(
    store: SqliteHistoryStore, caplog: pytest.LogCaptureFixture
) -> None:
    """A row whose JSON no longer validates is a logged miss on every read path."""
    store.save(_record("job-good"))
    _insert_raw_row(
        store,
        job_id="job-bad",
        record_json='{"not": "PAYLOAD-MUST-NOT-LEAK"}',
        format_version=HISTORY_FORMAT_VERSION,
        key="k-bad",
    )

    with caplog.at_level("WARNING", logger="app.history"):
        assert store.get("job-bad") is None
        assert store.find_idempotent("k-bad") is None  # dedup miss, not a 500
        assert [r.job_id for r in store.recent(10)] == ["job-good"]
        assert [r.job_id for r in store.page(offset=0, limit=10)] == ["job-good"]
    assert any("job-bad" in m for m in caplog.messages)  # skip is observable, once per read
    # The warning names locations/error types only — row payload stays out of the log.
    assert not any("PAYLOAD-MUST-NOT-LEAK" in m for m in caplog.messages)


def test_future_format_version_row_is_skipped(
    store: SqliteHistoryStore, caplog: pytest.LogCaptureFixture
) -> None:
    """A valid record written by a newer format (downgrade scenario) is skipped, not trusted."""
    store.save(_record("job-old"))
    _insert_raw_row(
        store,
        job_id="job-future",
        record_json=_record("job-future", key="k-future").model_dump_json(),
        format_version=HISTORY_FORMAT_VERSION + 1,
        key="k-future",
    )

    with caplog.at_level("WARNING", logger="app.history"):
        assert store.get("job-future") is None
        assert store.find_idempotent("k-future") is None
        assert [r.job_id for r in store.recent(10)] == ["job-old"]
    assert any("format_version" in m and "job-future" in m for m in caplog.messages)


def test_legacy_db_without_version_column_migrates(tmp_path: Path) -> None:
    """Opening a pre-versioning history.db adds the column; old rows read as version 1."""
    db = tmp_path / "history.db"
    legacy = sqlite3.connect(str(db))
    legacy.execute(
        "CREATE TABLE jobs ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, "
        "idempotency_key TEXT, status TEXT NOT NULL, record TEXT NOT NULL)"
    )
    legacy.execute(
        "INSERT INTO jobs (job_id, idempotency_key, status, record) VALUES (?, ?, ?, ?)",
        (
            "job-legacy",
            "k-legacy",
            "printed",
            _record("job-legacy", key="k-legacy").model_dump_json(),
        ),
    )
    legacy.commit()
    legacy.close()

    s = SqliteHistoryStore(str(db), keep=1000, prune_at=1500)
    try:
        got = s.get("job-legacy")  # NULL format_version → treated as version 1
        assert got is not None and got.job_id == "job-legacy"
        assert s.find_idempotent("k-legacy") is not None

        s.save(_record("job-new"))  # new writes are stamped with the current version
        (stamped,) = s._conn.execute(
            "SELECT format_version FROM jobs WHERE job_id = 'job-new'"
        ).fetchone()
        assert stamped == HISTORY_FORMAT_VERSION
    finally:
        s.close()


def test_memory_store_starts_empty() -> None:
    s = SqliteHistoryStore(":memory:", keep=1000, prune_at=1500)
    try:
        assert s.recent(10) == []
    finally:
        s.close()


@pytest.mark.parametrize(
    ("keep", "prune_at"),
    [(0, 10), (-1, 10), (10, 10), (10, 5)],
)
def test_invalid_watermarks_raise(keep: int, prune_at: int) -> None:
    with pytest.raises(ValueError):
        SqliteHistoryStore(":memory:", keep=keep, prune_at=prune_at)


def test_null_store_drops_writes_and_misses() -> None:
    s = NullHistoryStore()
    s.save(_record("job-1", key="k"))
    assert s.get("job-1") is None
    assert s.find_idempotent("k") is None
    assert s.recent(10) == []
    assert s.page(offset=0, limit=10) == []
    assert s.count() == 0
    assert s.delete("job-1") is False
    s.close()


def test_build_history_store_modes(tmp_path: Path) -> None:
    base = {"history_keep_entries": 1000, "history_prune_at_entries": 1500, "data_dir": tmp_path}

    disabled = build_history_store(SimpleNamespace(history_mode="disabled", **base))
    assert isinstance(disabled, NullHistoryStore)

    memory = build_history_store(SimpleNamespace(history_mode="memory", **base))
    assert isinstance(memory, SqliteHistoryStore)
    memory.close()

    file_store = build_history_store(SimpleNamespace(history_mode="file", **base))
    assert isinstance(file_store, SqliteHistoryStore)
    file_store.save(_record("job-f"))
    assert (tmp_path / "history.db").exists()
    file_store.close()


def test_build_history_store_unknown_mode(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Unknown history_mode"):
        build_history_store(
            SimpleNamespace(
                history_mode="bogus",
                history_keep_entries=1000,
                history_prune_at_entries=1500,
                data_dir=tmp_path,
            )
        )


# ── End-to-end: the mode changes /print + /reprint behaviour ──────────────────────


def test_memory_mode_dedupes_keyed_retry(client: TestClient) -> None:
    """The default (memory) fixture: a repeated idempotency_key prints once."""
    import app.main as main_mod

    body = {"template": "simple", "fields": {"title": "X"}, "idempotency_key": "k1"}
    r1 = client.post("/print", json=body)
    r2 = client.post("/print", json=body)
    assert r1.status_code == r2.status_code == 200
    assert r1.json()["job_id"] == r2.json()["job_id"]
    assert main_mod._driver.render_payload.call_count == 1


def test_disabled_mode_loses_dedup_and_reprint(client: TestClient) -> None:
    """HISTORY_MODE=disabled: keyed retries reprint and /reprint always 404s."""
    import app.main as main_mod

    main_mod._history.close()  # release the fixture's in-memory store before swapping it out
    main_mod._history = NullHistoryStore()

    body = {"template": "simple", "fields": {"title": "X"}, "idempotency_key": "k1"}
    r1 = client.post("/print", json=body)
    r2 = client.post("/print", json=body)
    assert r1.json()["job_id"] != r2.json()["job_id"]  # no dedup → two distinct jobs
    assert main_mod._driver.render_payload.call_count == 2

    reprint = client.post(f"/reprint/{r1.json()['job_id']}")
    assert reprint.status_code == 404  # nothing was recorded
