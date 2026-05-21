"""Run the Kalshi WebSocket streamer for currently-active LoL markets.

Subscribes to ticker_v2 updates for all KXLOLGAME markets with confident
linkage that are open within the next 48 hours, and writes incoming
ticker updates to the kalshi_markets DB so the rest of the bot sees
fresh prices.

Run alongside the trader:
    # terminal 1
    python -m loltrader.tools.ws_streamer
    # terminal 2
    python -m loltrader.tools.run_bot
"""
from __future__ import annotations

import asyncio
import logging
import signal
import sys
import time

from loltrader.config import load_config
from loltrader.db import connect, migrate
from loltrader.kalshi.ws import KalshiWS, persist_market_state


def _candidate_tickers(conn) -> list[str]:
    now = int(time.time())
    horizon = now + 48 * 3600
    rows = conn.execute(
        """
        SELECT m.market_ticker
        FROM kalshi_markets m
        JOIN market_match_links l ON l.market_ticker = m.market_ticker
        WHERE m.status IN ('active', 'open')
          AND m.series_ticker = 'KXLOLGAME'
          AND l.confidence >= 0.7
          AND m.close_time_unix BETWEEN ? AND ?
        ORDER BY m.close_time_unix ASC
        """,
        (now, horizon),
    ).fetchall()
    return [r["market_ticker"] for r in rows]


async def amain() -> int:
    cfg = load_config()
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(cfg.logs_dir / "ws_streamer.log", encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    log = logging.getLogger("ws_streamer")

    conn = connect()
    migrate(conn)
    tickers = _candidate_tickers(conn)
    if not tickers:
        log.warning("No candidate markets to subscribe to. Exiting.")
        return 0
    log.info("Subscribing to %d markets", len(tickers))

    def on_ticker(state):
        persist_market_state(conn, state)

    ws = KalshiWS(market_tickers=tickers, on_ticker=on_ticker)

    loop = asyncio.get_event_loop()
    if sys.platform != "win32":
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, ws.stop)

    try:
        await ws.run()
    except KeyboardInterrupt:
        ws.stop()
    log.info("WS streamer stopped. Stats: %s", ws.stats)
    return 0


def main() -> int:
    return asyncio.run(amain())


if __name__ == "__main__":
    sys.exit(main())
