"""Build and extend the local Kalshi LoL corpus.

Pulls events, markets, and candlestick history for the three LoL series
we trade (``KXLOLGAME``, ``KXLOLMAP``, ``KXLOLTOTALMAPS``) and UPSERTs
them into SQLite. Idempotent: repeated runs touch ``last_seen_at`` but
do not create duplicate rows.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime

from loltrader.kalshi.rest import KalshiClient, KalshiRestError

log = logging.getLogger(__name__)

LOL_SERIES: tuple[str, ...] = ("KXLOLGAME", "KXLOLMAP", "KXLOLTOTALMAPS")
MAX_CANDLES_PER_REQUEST = 5000
DEFAULT_CANDLE_INTERVAL_MIN = 60  # 60-min bars; one request covers ~208 days


# --- conversion helpers ---------------------------------------------------

def dollars_str_to_cents(s: str | None) -> int | None:
    """Convert Kalshi's dollars-string (e.g. "0.4500") to integer cents."""
    if s is None or s == "":
        return None
    return int(round(float(s) * 100))


def iso_to_unix(s: str | None) -> int | None:
    """Convert Kalshi's ISO 8601 timestamp to Unix seconds."""
    if not s:
        return None
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())


def is_lol_event(event: dict) -> bool:
    """Filter rule for LoL events using product_metadata.competition."""
    pm = event.get("product_metadata") or {}
    return pm.get("competition") == "League of Legends"


# --- per-table snapshotters ----------------------------------------------

def snapshot_events(client: KalshiClient, conn: sqlite3.Connection) -> int:
    """Pull all current LoL events (open + settled) into kalshi_events.
    Returns the number of UPSERTs performed."""
    now = int(time.time())
    upserts = 0
    for series in LOL_SERIES:
        for status in ("open", "settled"):
            cursor: str | None = None
            while True:
                params: dict = {"limit": 200, "status": status, "series_ticker": series}
                if cursor:
                    params["cursor"] = cursor
                resp = client.list_events(**params)
                for ev in resp.get("events", []) or []:
                    pm = ev.get("product_metadata") or {}
                    conn.execute(
                        """
                        INSERT INTO kalshi_events
                            (event_ticker, series_ticker, title, sub_title, category,
                             competition, competition_scope, mutually_exclusive,
                             last_updated_ts, first_seen_at, last_seen_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(event_ticker) DO UPDATE SET
                            title=excluded.title,
                            sub_title=excluded.sub_title,
                            last_updated_ts=excluded.last_updated_ts,
                            last_seen_at=excluded.last_seen_at
                        """,
                        (
                            ev["event_ticker"],
                            ev.get("series_ticker") or series,
                            ev.get("title"),
                            ev.get("sub_title"),
                            ev.get("category"),
                            pm.get("competition"),
                            pm.get("competition_scope"),
                            int(bool(ev.get("mutually_exclusive"))),
                            ev.get("last_updated_ts"),
                            now,
                            now,
                        ),
                    )
                    upserts += 1
                cursor = resp.get("cursor")
                if not cursor:
                    break
            conn.commit()
    log.info("snapshot_events: %d upserts", upserts)
    return upserts


def snapshot_markets(client: KalshiClient, conn: sqlite3.Connection) -> int:
    """For each known event in kalshi_events, pull its markets.
    Returns number of UPSERTs."""
    now = int(time.time())
    upserts = 0
    events = conn.execute(
        "SELECT event_ticker, series_ticker FROM kalshi_events"
    ).fetchall()
    for ev_row in events:
        et = ev_row["event_ticker"]
        try:
            resp = client.list_markets(event_ticker=et, limit=50)
        except KalshiRestError as e:
            log.warning("Failed to list markets for %s: %s", et, e)
            continue
        for m in resp.get("markets", []) or []:
            conn.execute(
                """
                INSERT INTO kalshi_markets
                    (market_ticker, event_ticker, series_ticker, title, subtitle,
                     open_time, open_time_unix, close_time, close_time_unix,
                     expected_close_time, status, result,
                     last_price_cents, yes_bid_cents, yes_ask_cents,
                     volume, volume_24h, open_interest,
                     first_seen_at, last_seen_at)
                VALUES (?, ?, ?, ?, ?,  ?, ?, ?, ?,  ?, ?, ?,  ?, ?, ?,  ?, ?, ?,  ?, ?)
                ON CONFLICT(market_ticker) DO UPDATE SET
                    title=excluded.title,
                    subtitle=excluded.subtitle,
                    open_time=excluded.open_time,
                    open_time_unix=excluded.open_time_unix,
                    close_time=excluded.close_time,
                    close_time_unix=excluded.close_time_unix,
                    expected_close_time=excluded.expected_close_time,
                    status=excluded.status,
                    result=excluded.result,
                    last_price_cents=excluded.last_price_cents,
                    yes_bid_cents=excluded.yes_bid_cents,
                    yes_ask_cents=excluded.yes_ask_cents,
                    volume=excluded.volume,
                    volume_24h=excluded.volume_24h,
                    open_interest=excluded.open_interest,
                    last_seen_at=excluded.last_seen_at
                """,
                (
                    m["ticker"],
                    et,
                    ev_row["series_ticker"],
                    m.get("title"),
                    m.get("subtitle"),
                    m.get("open_time"),
                    iso_to_unix(m.get("open_time")),
                    m.get("close_time"),
                    iso_to_unix(m.get("close_time")),
                    m.get("expected_close_time"),
                    m.get("status"),
                    m.get("result"),
                    m.get("last_price"),
                    m.get("yes_bid"),
                    m.get("yes_ask"),
                    m.get("volume"),
                    m.get("volume_24h"),
                    m.get("open_interest"),
                    now,
                    now,
                ),
            )
            upserts += 1
        conn.commit()
    log.info("snapshot_markets: %d upserts across %d events", upserts, len(events))
    return upserts


def snapshot_candles(
    client: KalshiClient,
    conn: sqlite3.Connection,
    period_interval: int = DEFAULT_CANDLE_INTERVAL_MIN,
) -> int:
    """Fetch candlesticks for every known market that we haven't fully
    covered yet. Returns total bars inserted/updated.

    Strategy: for each market, determine ``[start_ts, end_ts]`` window
    (open_time → min(close_time, now), excluding bars we already have).
    Chunk in slices of up to MAX_CANDLES_PER_REQUEST × period_interval
    minutes to stay under Kalshi's 5000-bar/request cap.
    """
    now = int(time.time())
    upserts = 0
    markets = conn.execute(
        """
        SELECT m.market_ticker, m.series_ticker, m.open_time_unix, m.close_time_unix
        FROM kalshi_markets m
        WHERE m.open_time_unix IS NOT NULL
          AND m.close_time_unix IS NOT NULL
        """
    ).fetchall()
    chunk_seconds = MAX_CANDLES_PER_REQUEST * period_interval * 60

    for m_row in markets:
        mt = m_row["market_ticker"]
        latest = conn.execute(
            """
            SELECT MAX(end_period_ts) AS max_ts FROM kalshi_candles
            WHERE market_ticker = ? AND period_interval = ?
            """,
            (mt, period_interval),
        ).fetchone()
        last_ts = latest["max_ts"]
        start_ts = max(last_ts + 1, m_row["open_time_unix"]) if last_ts else m_row["open_time_unix"]
        end_ts = min(m_row["close_time_unix"], now)
        if start_ts >= end_ts:
            continue

        chunk_start = start_ts
        while chunk_start < end_ts:
            chunk_end = min(chunk_start + chunk_seconds, end_ts)
            try:
                resp = client.get_candlesticks(
                    series_ticker=m_row["series_ticker"],
                    market_ticker=mt,
                    start_unix=chunk_start,
                    end_unix=chunk_end,
                    period_interval=period_interval,
                )
            except KalshiRestError as e:
                # Some markets 404 or return bad_request — skip and move on
                if e.status in (400, 404):
                    log.debug("Candles skipped for %s (%s)", mt, e.status)
                    break
                raise

            for bar in resp.get("candlesticks", []) or []:
                price = bar.get("price") or {}
                yb = bar.get("yes_bid") or {}
                ya = bar.get("yes_ask") or {}
                conn.execute(
                    """
                    INSERT INTO kalshi_candles
                        (market_ticker, end_period_ts, period_interval,
                         price_open_cents, price_high_cents, price_low_cents,
                         price_close_cents, price_mean_cents, price_previous_cents,
                         yes_bid_open_cents, yes_bid_high_cents, yes_bid_low_cents,
                         yes_bid_close_cents,
                         yes_ask_open_cents, yes_ask_high_cents, yes_ask_low_cents,
                         yes_ask_close_cents,
                         volume, open_interest, fetched_at)
                    VALUES (?, ?, ?,  ?, ?, ?, ?, ?, ?,  ?, ?, ?, ?,  ?, ?, ?, ?,  ?, ?, ?)
                    ON CONFLICT(market_ticker, end_period_ts, period_interval) DO UPDATE SET
                        price_open_cents=excluded.price_open_cents,
                        price_high_cents=excluded.price_high_cents,
                        price_low_cents=excluded.price_low_cents,
                        price_close_cents=excluded.price_close_cents,
                        price_mean_cents=excluded.price_mean_cents,
                        price_previous_cents=excluded.price_previous_cents,
                        yes_bid_open_cents=excluded.yes_bid_open_cents,
                        yes_bid_high_cents=excluded.yes_bid_high_cents,
                        yes_bid_low_cents=excluded.yes_bid_low_cents,
                        yes_bid_close_cents=excluded.yes_bid_close_cents,
                        yes_ask_open_cents=excluded.yes_ask_open_cents,
                        yes_ask_high_cents=excluded.yes_ask_high_cents,
                        yes_ask_low_cents=excluded.yes_ask_low_cents,
                        yes_ask_close_cents=excluded.yes_ask_close_cents,
                        volume=excluded.volume,
                        open_interest=excluded.open_interest,
                        fetched_at=excluded.fetched_at
                    """,
                    (
                        mt,
                        bar["end_period_ts"],
                        period_interval,
                        dollars_str_to_cents(price.get("open_dollars")),
                        dollars_str_to_cents(price.get("high_dollars")),
                        dollars_str_to_cents(price.get("low_dollars")),
                        dollars_str_to_cents(price.get("close_dollars")),
                        dollars_str_to_cents(price.get("mean_dollars")),
                        dollars_str_to_cents(price.get("previous_dollars")),
                        dollars_str_to_cents(yb.get("open_dollars")),
                        dollars_str_to_cents(yb.get("high_dollars")),
                        dollars_str_to_cents(yb.get("low_dollars")),
                        dollars_str_to_cents(yb.get("close_dollars")),
                        dollars_str_to_cents(ya.get("open_dollars")),
                        dollars_str_to_cents(ya.get("high_dollars")),
                        dollars_str_to_cents(ya.get("low_dollars")),
                        dollars_str_to_cents(ya.get("close_dollars")),
                        float(bar.get("volume_fp") or 0),
                        float(bar.get("open_interest_fp") or 0),
                        now,
                    ),
                )
                upserts += 1
            chunk_start = chunk_end
        conn.commit()

    log.info("snapshot_candles: %d bars upserted across %d markets", upserts, len(markets))
    return upserts


def snapshot_all_lol_markets(
    client: KalshiClient | None = None,
    conn: sqlite3.Connection | None = None,
    period_interval: int = DEFAULT_CANDLE_INTERVAL_MIN,
) -> dict[str, int]:
    """Top-level entry: events -> markets -> candles. Returns counts per stage."""
    if client is None:
        client = KalshiClient()
    if conn is None:
        from loltrader.db import connect

        conn = connect()
    return {
        "events": snapshot_events(client, conn),
        "markets": snapshot_markets(client, conn),
        "candles": snapshot_candles(client, conn, period_interval),
    }
