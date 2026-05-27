-- 013_v2_backtest_fields.sql
-- v2 backtest support: extend games_live with fields needed for historical
-- training data (winner labels, match-id grouping, source tag).
--
-- These are nullable so existing v1/v2 rows don't break. New historical-extraction
-- code populates them; live-discovery doesn't have to.

ALTER TABLE games_live ADD COLUMN winner_side TEXT;        -- 'blue' | 'red' | NULL while game in progress
ALTER TABLE games_live ADD COLUMN esports_match_id TEXT;   -- Riot's series-level match id (groups games of a bo3/bo5)
ALTER TABLE games_live ADD COLUMN game_number INTEGER;     -- which game of the series (1, 2, 3, ...)
ALTER TABLE games_live ADD COLUMN source TEXT NOT NULL DEFAULT 'live';  -- 'live' | 'historical_backtest'

CREATE INDEX IF NOT EXISTS idx_games_live_match_id ON games_live(esports_match_id);
CREATE INDEX IF NOT EXISTS idx_games_live_source   ON games_live(source);
