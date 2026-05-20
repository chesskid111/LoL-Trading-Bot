-- 001_kalshi.sql
-- Initial Kalshi market-data tables: events, markets, candles, book snapshots.
-- All price fields are integer cents (Kalshi's native unit). Volume/open
-- interest are REAL because Kalshi returns them as floats.

CREATE TABLE IF NOT EXISTS kalshi_events (
    event_ticker        TEXT PRIMARY KEY,
    series_ticker       TEXT NOT NULL,
    title               TEXT,
    sub_title           TEXT,
    category            TEXT,
    competition         TEXT,    -- product_metadata.competition (e.g., "League of Legends")
    competition_scope   TEXT,    -- product_metadata.competition_scope (e.g., "Map 1 Winner")
    mutually_exclusive  INTEGER, -- bool as 0/1
    last_updated_ts     TEXT,    -- ISO 8601 string from Kalshi
    first_seen_at       INTEGER NOT NULL,  -- Unix seconds when we first inserted
    last_seen_at        INTEGER NOT NULL   -- Unix seconds of most recent touch
);

CREATE INDEX IF NOT EXISTS idx_kalshi_events_series      ON kalshi_events(series_ticker);
CREATE INDEX IF NOT EXISTS idx_kalshi_events_competition ON kalshi_events(competition);


CREATE TABLE IF NOT EXISTS kalshi_markets (
    market_ticker        TEXT PRIMARY KEY,
    event_ticker         TEXT NOT NULL,
    series_ticker        TEXT NOT NULL,
    title                TEXT,
    subtitle             TEXT,
    open_time            TEXT,        -- ISO 8601
    open_time_unix       INTEGER,     -- denormalized for indexing
    close_time           TEXT,
    close_time_unix      INTEGER,
    expected_close_time  TEXT,
    status               TEXT,        -- "open" / "closed" / "settled" / etc.
    result               TEXT,        -- "yes" / "no" / NULL while open
    last_price_cents     INTEGER,
    yes_bid_cents        INTEGER,
    yes_ask_cents        INTEGER,
    volume               REAL,
    volume_24h           REAL,
    open_interest        REAL,
    settled_at_unix      INTEGER,
    first_seen_at        INTEGER NOT NULL,
    last_seen_at         INTEGER NOT NULL,
    FOREIGN KEY (event_ticker) REFERENCES kalshi_events(event_ticker)
);

CREATE INDEX IF NOT EXISTS idx_kalshi_markets_event      ON kalshi_markets(event_ticker);
CREATE INDEX IF NOT EXISTS idx_kalshi_markets_series     ON kalshi_markets(series_ticker);
CREATE INDEX IF NOT EXISTS idx_kalshi_markets_status     ON kalshi_markets(status);
CREATE INDEX IF NOT EXISTS idx_kalshi_markets_close_time ON kalshi_markets(close_time_unix);


CREATE TABLE IF NOT EXISTS kalshi_candles (
    market_ticker         TEXT NOT NULL,
    end_period_ts         INTEGER NOT NULL,   -- Unix seconds at end of bar
    period_interval       INTEGER NOT NULL,   -- minutes (1, 60, 1440, etc.)

    price_open_cents      INTEGER,
    price_high_cents      INTEGER,
    price_low_cents       INTEGER,
    price_close_cents     INTEGER,
    price_mean_cents      INTEGER,
    price_previous_cents  INTEGER,

    yes_bid_open_cents    INTEGER,
    yes_bid_high_cents    INTEGER,
    yes_bid_low_cents     INTEGER,
    yes_bid_close_cents   INTEGER,

    yes_ask_open_cents    INTEGER,
    yes_ask_high_cents    INTEGER,
    yes_ask_low_cents     INTEGER,
    yes_ask_close_cents   INTEGER,

    volume                REAL,
    open_interest         REAL,

    fetched_at            INTEGER NOT NULL,
    PRIMARY KEY (market_ticker, end_period_ts, period_interval),
    FOREIGN KEY (market_ticker) REFERENCES kalshi_markets(market_ticker)
);

CREATE INDEX IF NOT EXISTS idx_kalshi_candles_end_ts ON kalshi_candles(end_period_ts);


CREATE TABLE IF NOT EXISTS kalshi_book_snapshots (
    snapshot_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    market_ticker   TEXT NOT NULL,
    snapshot_ts     INTEGER NOT NULL,
    yes_levels_json TEXT,
    no_levels_json  TEXT,
    FOREIGN KEY (market_ticker) REFERENCES kalshi_markets(market_ticker)
);

CREATE INDEX IF NOT EXISTS idx_book_snapshots_market_ts
    ON kalshi_book_snapshots(market_ticker, snapshot_ts);
