"""Daily Kalshi corpus logger.

Run via Windows Task Scheduler every ~6 hours. Each invocation:
  1. Connects to SQLite (creates the DB if missing).
  2. Applies any pending schema migrations.
  3. Pulls open + settled LoL events for each LoL series.
  4. Pulls each event's markets.
  5. Pulls missing candlesticks for each market.
  6. Writes everything via UPSERT (idempotent).

Exit code 0 on success, 1 on failure (caught and logged).
"""
from __future__ import annotations

import logging
import sys
import time

from loltrader.config import load_config
from loltrader.db import connect, migrate
from loltrader.kalshi.corpus import snapshot_all_lol_markets
from loltrader.kalshi.rest import KalshiClient


def _setup_logging() -> logging.Logger:
    cfg = load_config()
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    logfile = cfg.logs_dir / "daily_logger.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(logfile, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    return logging.getLogger("daily_logger")


def main() -> int:
    log = _setup_logging()
    start = time.time()
    log.info("Starting daily logger run")
    try:
        conn = connect()
        applied = migrate(conn)
        if applied:
            log.info("Applied migrations: %s", applied)
        else:
            log.info("Schema up to date")

        client = KalshiClient()
        balance = client.get_balance()
        log.info("Kalshi balance: %s cents", balance.get("balance"))

        stats = snapshot_all_lol_markets(client, conn)
        elapsed = time.time() - start
        log.info(
            "Run complete in %.1fs: events=%d markets=%d candles=%d",
            elapsed, stats["events"], stats["markets"], stats["candles"],
        )

        # Summary counts from DB for sanity
        e = conn.execute("SELECT COUNT(*) AS n FROM kalshi_events").fetchone()["n"]
        m = conn.execute("SELECT COUNT(*) AS n FROM kalshi_markets").fetchone()["n"]
        c = conn.execute("SELECT COUNT(*) AS n FROM kalshi_candles").fetchone()["n"]
        log.info("DB totals: events=%d markets=%d candles=%d", e, m, c)
        conn.close()
        return 0
    except Exception:
        log.exception("Daily logger run failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
