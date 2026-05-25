-- 009_v2_cv_frames.sql
-- v2 live trading: computer-vision pipeline outputs.
-- Spec §6.2 (CV is the primary numerical source), §6.3 (OCR-vs-livestats watchdog).

-- One row per broadcast frame processed by cv_pipeline (~1 fps).
-- Stored even for non-in_game frames so we can audit classifier behavior.
CREATE TABLE IF NOT EXISTS cv_frames (
    cv_frame_id              INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id                  TEXT NOT NULL,
    frame_ts_unix            INTEGER NOT NULL,          -- when CV processed the frame
    -- Classifier output (spec §6.2):
    classifier_class         TEXT NOT NULL,             -- in_game|studio|replay|ads|unknown
    classifier_confidence    REAL,                       -- 0..1
    -- OCR results, only populated when classifier_class='in_game' (else NULL):
    ocr_gold_blue            INTEGER,
    ocr_gold_red             INTEGER,
    ocr_kills_blue           INTEGER,
    ocr_kills_red            INTEGER,
    ocr_towers_blue          INTEGER,
    ocr_towers_red           INTEGER,
    ocr_dragons_blue         INTEGER,
    ocr_dragons_red          INTEGER,
    ocr_barons_blue          INTEGER,
    ocr_barons_red           INTEGER,
    ocr_timer_seconds        INTEGER,                    -- in-game clock from broadcast scoreboard
    -- Positional / qualitative outputs (JSON for flexibility while iterating):
    minimap_dots_json        TEXT,                       -- [{team,champion,x,y}, ...]
    items_json               TEXT,                       -- {participantId: [item_ids], ...}
    -- Path to PNG snapshot on disk (relative to project root). NULL if discarded.
    frame_png_path           TEXT,
    FOREIGN KEY (game_id) REFERENCES games_live(game_id),
    UNIQUE (game_id, frame_ts_unix)
);

CREATE INDEX IF NOT EXISTS idx_cv_frames_game_ts        ON cv_frames(game_id, frame_ts_unix);
CREATE INDEX IF NOT EXISTS idx_cv_frames_classifier     ON cv_frames(classifier_class);


-- CV-vs-livestats validation rows for the disagreement watchdog (spec §6.3, §17 #12).
-- Computed by joining each new cv_frames row against nearest-timestamp live_frames row.
CREATE TABLE IF NOT EXISTS cv_validation (
    validation_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    cv_frame_id              INTEGER NOT NULL,
    live_frame_id            INTEGER,                    -- NULL if no nearby livestats frame
    ts_offset_sec            REAL,                       -- cv_ts - livestats_ts (signed)
    gold_diff_blue           INTEGER,                    -- ocr_gold_blue - live_blue_gold
    gold_diff_red            INTEGER,
    gold_pct_diff_blue       REAL,                       -- abs(gold_diff_blue) / live_blue_gold
    gold_pct_diff_red        REAL,
    kills_diff_blue          INTEGER,
    kills_diff_red           INTEGER,
    flagged                  INTEGER NOT NULL DEFAULT 0, -- 1 if divergence exceeded threshold
    FOREIGN KEY (cv_frame_id) REFERENCES cv_frames(cv_frame_id),
    FOREIGN KEY (live_frame_id) REFERENCES live_frames(frame_id)
);

CREATE INDEX IF NOT EXISTS idx_cv_validation_flagged ON cv_validation(flagged);
CREATE INDEX IF NOT EXISTS idx_cv_validation_cv      ON cv_validation(cv_frame_id);
