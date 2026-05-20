"""SQLite connection helper and migration runner.

Usage:
    from loltrader.db import connect, migrate

    conn = connect()
    migrate(conn)
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from loltrader.config import load_config


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    """Open a SQLite connection with WAL mode + foreign keys enabled."""
    if db_path is None:
        db_path = load_config().db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    return conn


def migrate(conn: sqlite3.Connection | None = None) -> list[str]:
    """Apply all migration .sql files in ``db/migrations/`` not yet applied.

    Returns the list of migration versions applied this run (empty if up-to-date).
    Idempotent across runs.
    """
    close_after = False
    if conn is None:
        conn = connect()
        close_after = True
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version TEXT PRIMARY KEY,
                applied_at INTEGER NOT NULL
            )
            """
        )
        conn.commit()
        applied = {
            row["version"]
            for row in conn.execute("SELECT version FROM schema_migrations")
        }
        migrations_dir = Path(__file__).parent / "migrations"
        files = sorted(migrations_dir.glob("*.sql"))
        run: list[str] = []
        for f in files:
            ver = f.stem
            if ver in applied:
                continue
            sql = f.read_text(encoding="utf-8")
            with conn:
                conn.executescript(sql)
                conn.execute(
                    "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                    (ver, int(time.time())),
                )
            run.append(ver)
        return run
    finally:
        if close_after:
            conn.close()
