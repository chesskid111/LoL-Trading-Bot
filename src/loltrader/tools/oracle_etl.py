"""Run the Oracle's Elixir ETL on all CSVs in data/raw/oracle/, then seed
team aliases and backfill linkage.

Idempotent: re-running is safe.
"""
from __future__ import annotations

import logging
import sys
import time

from loltrader.config import load_config
from loltrader.db import connect, migrate
from loltrader.kalshi.linkage import backfill_links
from loltrader.oracle.etl import etl_all
from loltrader.oracle.seed_aliases import seed_team_aliases


def _setup_logging() -> logging.Logger:
    cfg = load_config()
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    logfile = cfg.logs_dir / "oracle_etl.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(logfile, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    return logging.getLogger("oracle_etl")


def main() -> int:
    log = _setup_logging()
    cfg = load_config()
    raw_dir = cfg.project_root / "data" / "raw" / "oracle"
    raw_dir.mkdir(parents=True, exist_ok=True)
    log.info("Starting Oracle ETL run")
    start = time.time()
    try:
        conn = connect()
        applied = migrate(conn)
        if applied:
            log.info("Applied migrations: %s", applied)

        results = etl_all(conn, raw_dir)
        if not results:
            log.error("No CSVs found in %s — download them first.", raw_dir)
            return 1
        log.info("ETL per-file counts: %s", results)

        n_aliases = seed_team_aliases(conn)
        log.info("Seeded %d new team aliases", n_aliases)

        link_counts = backfill_links(conn)
        log.info("Linkage backfill: %s", link_counts)

        # Summary
        m = conn.execute("SELECT COUNT(*) AS n FROM matches").fetchone()["n"]
        g = conn.execute("SELECT COUNT(*) AS n FROM match_games").fetchone()["n"]
        t = conn.execute("SELECT COUNT(*) AS n FROM teams").fetchone()["n"]
        p = conn.execute("SELECT COUNT(*) AS n FROM players").fetchone()["n"]
        log.info("DB totals: matches=%d games=%d teams=%d players=%d", m, g, t, p)

        elapsed = time.time() - start
        log.info("Oracle ETL complete in %.1fs", elapsed)
        conn.close()
        return 0
    except Exception:
        log.exception("Oracle ETL run failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
