-- 010_v2_live_decisions.sql
-- v2 live trading: extend decisions table with frame-level + uncertainty + action-type.
-- Spec §9 (live trading logic), §11 (live-specific risk gates).
--
-- Strategy: ADD COLUMN to existing v1 decisions table (additive, backward-compatible).
-- v1 decisions remain valid with NULLs for the new columns. Trader v2 populates them.

ALTER TABLE decisions ADD COLUMN frame_id INTEGER REFERENCES live_frames(frame_id);
ALTER TABLE decisions ADD COLUMN cv_frame_id INTEGER REFERENCES cv_frames(cv_frame_id);
ALTER TABLE decisions ADD COLUMN model_uncertainty_width REAL;
ALTER TABLE decisions ADD COLUMN action_type TEXT;        -- OPEN|ADD|HOLD|CLOSE|REVERSE (spec §9.2)
ALTER TABLE decisions ADD COLUMN data_source TEXT;        -- cv_primary|livestats_fallback|both
ALTER TABLE decisions ADD COLUMN game_id TEXT REFERENCES games_live(game_id);
ALTER TABLE decisions ADD COLUMN in_game_seconds INTEGER; -- computed from games_live.game_start

CREATE INDEX IF NOT EXISTS idx_decisions_frame       ON decisions(frame_id);
CREATE INDEX IF NOT EXISTS idx_decisions_cv_frame    ON decisions(cv_frame_id);
CREATE INDEX IF NOT EXISTS idx_decisions_game        ON decisions(game_id);
CREATE INDEX IF NOT EXISTS idx_decisions_action_type ON decisions(action_type);


-- Add action_type to paper_trades too so we can audit (OPEN/ADD/CLOSE/REVERSE) flow.
ALTER TABLE paper_trades ADD COLUMN action_type TEXT;
ALTER TABLE paper_trades ADD COLUMN parent_trade_id INTEGER REFERENCES paper_trades(trade_id);
-- parent_trade_id links a CLOSE/REVERSE/ADD trade back to the OPEN it modifies.

CREATE INDEX IF NOT EXISTS idx_paper_trades_action_type ON paper_trades(action_type);
CREATE INDEX IF NOT EXISTS idx_paper_trades_parent       ON paper_trades(parent_trade_id);
