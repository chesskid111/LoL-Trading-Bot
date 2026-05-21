-- 006_trader.sql
-- Trader-side state: decisions made, paper trades executed.
-- A "decision" is logged every time the trader evaluates a market
-- (whether it actually places a trade or skips). A "paper_trade" is
-- created only when a decision results in a (simulated) order.

CREATE TABLE IF NOT EXISTS decisions (
    decision_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_ts           INTEGER NOT NULL,           -- Unix seconds
    market_ticker         TEXT NOT NULL,
    match_id              INTEGER,
    model_version         TEXT,                        -- artifact timestamp
    model_prob            REAL,                        -- calibrated P(YES) per market
    p10                   REAL,
    p90                   REAL,
    market_yes_bid_cents  INTEGER,
    market_yes_ask_cents  INTEGER,
    edge                  REAL,                        -- signed edge at decision moment
    edge_threshold        REAL,
    action                TEXT NOT NULL,               -- "BUY_YES" / "BUY_NO" / "HOLD"
    gate_reason           TEXT,                        -- NULL if no skip; else first failed gate
    made_by               TEXT NOT NULL DEFAULT 'bot', -- "bot" / "user"
    FOREIGN KEY (market_ticker) REFERENCES kalshi_markets(market_ticker),
    FOREIGN KEY (match_id) REFERENCES matches(match_id)
);

CREATE INDEX IF NOT EXISTS idx_decisions_ts        ON decisions(decision_ts);
CREATE INDEX IF NOT EXISTS idx_decisions_market    ON decisions(market_ticker);
CREATE INDEX IF NOT EXISTS idx_decisions_action    ON decisions(action);


CREATE TABLE IF NOT EXISTS paper_trades (
    trade_id              INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id           INTEGER NOT NULL,
    opened_at             INTEGER NOT NULL,            -- Unix seconds at simulated fill
    side                  TEXT NOT NULL,                -- "YES" / "NO"
    contracts             INTEGER NOT NULL,
    fill_price_cents      INTEGER NOT NULL,
    entry_fee_cents       INTEGER NOT NULL,
    -- Filled at settlement:
    closed_at             INTEGER,
    settle_value_cents    INTEGER,                      -- 100 if our side won, 0 if not
    pnl_cents             INTEGER,
    FOREIGN KEY (decision_id) REFERENCES decisions(decision_id)
);

CREATE INDEX IF NOT EXISTS idx_paper_trades_decision ON paper_trades(decision_id);
CREATE INDEX IF NOT EXISTS idx_paper_trades_open     ON paper_trades(closed_at);


-- Bot session state. A new row per bot startup, used to track
-- session-level PnL for the daily-stop-loss kill switch.
CREATE TABLE IF NOT EXISTS bot_sessions (
    session_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at            INTEGER NOT NULL,
    starting_bankroll_cents INTEGER NOT NULL,
    ended_at              INTEGER,
    end_reason            TEXT                          -- "manual" / "soft_kill" / "hard_kill" / etc.
);
