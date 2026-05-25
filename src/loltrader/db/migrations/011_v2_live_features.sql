-- 011_v2_live_features.sql
-- v2 live trading: per-frame feature vector snapshot for reproducibility.
-- Spec §7 (features), §17 (training data construction).
--
-- Long table (one row per feature per frame) rather than wide (one column per
-- feature) because the feature set will evolve. Adding/removing features
-- doesn't require schema migrations. Trades off some query speed for
-- flexibility; acceptable given ~220 features × ~300 frames/game × ~500 games
-- = ~33M rows, easily handled by SQLite with the right indexes.

CREATE TABLE IF NOT EXISTS live_features_cache (
    feature_row_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id                  TEXT NOT NULL,
    frame_ts_unix            INTEGER NOT NULL,
    feature_name             TEXT NOT NULL,
    value                    REAL,                       -- NULL allowed for missing features
    -- Tag distinguishing live-computed rows from VOD-replay-computed rows
    -- (spec §6 / §12.1). VOD rows are training data; live rows are for
    -- post-hoc audit of what features the trader actually saw.
    source_tag               TEXT NOT NULL,              -- "live" | "vod_replay"
    feature_set_version      TEXT NOT NULL,              -- e.g. "v2.0" — bumps when feature set changes
    FOREIGN KEY (game_id) REFERENCES games_live(game_id),
    UNIQUE (game_id, frame_ts_unix, feature_name, source_tag, feature_set_version)
);

CREATE INDEX IF NOT EXISTS idx_live_features_game_ts ON live_features_cache(game_id, frame_ts_unix);
CREATE INDEX IF NOT EXISTS idx_live_features_name    ON live_features_cache(feature_name);
CREATE INDEX IF NOT EXISTS idx_live_features_source  ON live_features_cache(source_tag);


-- Per-game per-frame model predictions, captured so we can audit calibration
-- against actual outcomes after the game resolves.
CREATE TABLE IF NOT EXISTS live_predictions (
    prediction_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id                  TEXT NOT NULL,
    frame_ts_unix            INTEGER NOT NULL,
    model_version            TEXT NOT NULL,
    p_yes_team_a             REAL NOT NULL,              -- KXLOLGAME team_a perspective
    p10                      REAL NOT NULL,
    p90                      REAL NOT NULL,
    uncertainty_width        REAL NOT NULL,              -- p90 - p10
    phase_bucket             TEXT NOT NULL,              -- "0-10" | "10-20" | "20-30" | "30+"
    FOREIGN KEY (game_id) REFERENCES games_live(game_id),
    UNIQUE (game_id, frame_ts_unix, model_version)
);

CREATE INDEX IF NOT EXISTS idx_live_predictions_game_ts ON live_predictions(game_id, frame_ts_unix);
CREATE INDEX IF NOT EXISTS idx_live_predictions_phase   ON live_predictions(phase_bucket);
