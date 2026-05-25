-- 012_v2_risk_state.sql
-- v2 live trading: shared risk-accounting state for concurrent-game exposure.
-- Spec §11.6 (concurrent-game accounting).
--
-- LCK plays multiple series in parallel on weekends. Bankroll caps must be
-- enforced ACROSS games, not per-game. This table holds a single row read by
-- every OPEN/ADD trader decision and updated atomically on every fill.

CREATE TABLE IF NOT EXISTS risk_state (
    id                          INTEGER PRIMARY KEY CHECK (id = 1),  -- enforces singleton
    current_total_exposure_cents INTEGER NOT NULL DEFAULT 0,         -- across all open positions
    daily_pnl_cents             INTEGER NOT NULL DEFAULT 0,           -- summed across all games today
    session_pnl_cents           INTEGER NOT NULL DEFAULT 0,           -- summed across all games this session
    daily_anchor_ts_unix        INTEGER NOT NULL,                     -- start-of-day (PDT midnight) used to reset daily_pnl
    last_updated_ts_unix        INTEGER NOT NULL,
    halt_reason                 TEXT,                                 -- NULL=trading allowed; else string code
    halt_set_ts_unix            INTEGER                                -- when halt was triggered
);

-- Seed the singleton row. Subsequent reads use SELECT * FROM risk_state WHERE id=1.
INSERT OR IGNORE INTO risk_state (id, daily_anchor_ts_unix, last_updated_ts_unix)
VALUES (1, strftime('%s','now'), strftime('%s','now'));


-- Per-game exposure breakdown for diagnostics. Not used in gate decisions
-- (those read risk_state.current_total_exposure_cents directly), but lets the
-- morning report and Streamlit UI show "TL game: $X, LYON game: $Y" details.
CREATE TABLE IF NOT EXISTS per_game_exposure (
    game_id                     TEXT PRIMARY KEY,
    exposure_cents              INTEGER NOT NULL DEFAULT 0,
    realized_pnl_cents          INTEGER NOT NULL DEFAULT 0,            -- closed positions only
    open_position_count         INTEGER NOT NULL DEFAULT 0,
    last_updated_ts_unix        INTEGER NOT NULL,
    FOREIGN KEY (game_id) REFERENCES games_live(game_id)
);

CREATE INDEX IF NOT EXISTS idx_per_game_exposure_updated ON per_game_exposure(last_updated_ts_unix);
