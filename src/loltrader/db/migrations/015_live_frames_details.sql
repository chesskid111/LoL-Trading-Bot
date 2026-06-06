-- 015_live_frames_details.sql
-- Per-participant snapshot from Riot's livestats /details endpoint.
-- The /window endpoint we already poll gives team-aggregate state; /details
-- adds items, stats, runes, ability allocation, ward counts, and damage share
-- per player. Stored as one row per (game_id, frame_ts_unix, participant_id)
-- so existing live_frames rows can be joined by (game_id, frame_ts_unix).
--
-- items, perks, abilities are kept as JSON because they're variable-length
-- arrays of small ints; storing them as JSON is fine for SQLite and keeps
-- the schema flat.

CREATE TABLE IF NOT EXISTS live_frames_details (
    detail_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id               TEXT NOT NULL,
    frame_ts_unix         INTEGER NOT NULL,
    fetched_ts_unix       INTEGER NOT NULL,
    side                  TEXT NOT NULL CHECK (side IN ('blue','red')),
    participant_id        INTEGER NOT NULL,
    level                 INTEGER,
    kills                 INTEGER,
    deaths                INTEGER,
    assists               INTEGER,
    total_gold            INTEGER,
    creep_score           INTEGER,
    kill_participation    REAL,
    champion_damage_share REAL,
    wards_placed          INTEGER,
    wards_destroyed       INTEGER,
    attack_damage         INTEGER,
    ability_power         INTEGER,
    armor                 INTEGER,
    magic_resistance      INTEGER,
    attack_speed          INTEGER,
    critical_chance       REAL,
    life_steal            REAL,
    tenacity              REAL,
    items_json            TEXT,   -- e.g. "[1120,3111,3078,...]"
    perks_json            TEXT,   -- runes metadata
    abilities_json        TEXT,   -- ability allocation order
    FOREIGN KEY (game_id) REFERENCES games_live(game_id),
    UNIQUE (game_id, frame_ts_unix, participant_id)
);

CREATE INDEX IF NOT EXISTS idx_live_frames_details_game_ts
    ON live_frames_details(game_id, frame_ts_unix);
CREATE INDEX IF NOT EXISTS idx_live_frames_details_participant
    ON live_frames_details(participant_id);
