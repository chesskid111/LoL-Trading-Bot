-- 016_trade_journal.sql
-- Decision-support trade journal for the user's MANUAL Kalshi trades.
--
-- Distinct from paper_trades (which logs the automated paper trader). This
-- captures, per manual trade the user actually places on Kalshi: what the
-- model said at entry (fair / edge / leverage / recommended size), what the
-- user did, and what happened at exit. The point is measurement — after ~20
-- trades, summarize() answers "is my realized edge real?", which is the only
-- way to know whether the model + discipline are actually working.
--
-- All prob fields are oriented to the side the user is long (0..1). Price
-- fields are that side's market price in cents (0..100).

CREATE TABLE IF NOT EXISTS trade_journal (
    journal_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at          INTEGER NOT NULL,
    game_id             TEXT,
    ticker              TEXT,
    side                TEXT NOT NULL,           -- 'blue' / 'red'

    -- entry snapshot
    entry_ts            INTEGER NOT NULL,
    entry_minute        REAL,
    model_fair_entry    REAL NOT NULL,           -- your-side fair prob, 0..1
    market_entry_c      REAL NOT NULL,           -- your-side market price, 0..100
    edge_entry          REAL NOT NULL,           -- fair - market (prob units)
    leverage_entry      REAL,
    contracts           INTEGER NOT NULL,
    entry_price_c       INTEGER NOT NULL,        -- actual fill cents
    rec_contracts       INTEGER,                 -- size the model recommended

    -- exit snapshot (NULL until closed)
    exit_ts             INTEGER,
    exit_minute         REAL,
    model_fair_exit     REAL,
    market_exit_c       REAL,
    exit_price_c        INTEGER,
    exit_reason         TEXT,                    -- manual | coinflip | inhibitor |
                                                 -- opponent_baron | catastrophic_swing |
                                                 -- take_profit | settled | stop
    triggers_at_exit    TEXT,                    -- comma-joined exit triggers active
    flagged_exit_held   INTEGER DEFAULT 0,       -- 1 if system said exit but user held

    -- outcome
    realized_pnl_c      INTEGER,
    settled_value_c     INTEGER,                 -- 100/0 if held to settlement
    notes               TEXT
);

CREATE INDEX IF NOT EXISTS idx_journal_game   ON trade_journal(game_id);
CREATE INDEX IF NOT EXISTS idx_journal_open    ON trade_journal(exit_ts);
CREATE INDEX IF NOT EXISTS idx_journal_created ON trade_journal(created_at);
