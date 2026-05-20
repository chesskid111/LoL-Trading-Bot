"""Tests for db.connect + db.migrate."""
from __future__ import annotations

from pathlib import Path

from loltrader.db import connect, migrate


def test_migrate_creates_tables(tmp_path: Path) -> None:
    db = tmp_path / "test.db"
    conn = connect(db)
    applied = migrate(conn)
    assert "001_kalshi" in applied
    tables = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    expected = {
        "schema_migrations",
        "kalshi_events",
        "kalshi_markets",
        "kalshi_candles",
        "kalshi_book_snapshots",
    }
    assert expected.issubset(tables), f"missing tables: {expected - tables}"
    conn.close()


def test_migrate_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "test.db"
    conn = connect(db)
    first = migrate(conn)
    second = migrate(conn)
    # First run applies all known migrations in alphabetical order
    assert "001_kalshi" in first
    assert "002_oracle" in first
    assert "003_linkage" in first
    assert second == []  # nothing to apply on second run
    conn.close()


def test_pragmas_set(tmp_path: Path) -> None:
    db = tmp_path / "test.db"
    conn = connect(db)
    journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
    fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    assert journal.lower() == "wal"
    assert fk == 1
    conn.close()
