-- 008_v2_live_frames.sql
-- v2 live trading: Riot livestats frames and per-game state.
-- Spec §6.1 (Riot livestats), §10.5 (game-start caching).

-- One row per game observed by game_discovery, with cached game-start timestamp.
-- The game_start_ts_unix is set ONCE on first detection of state=in_game and
-- never re-probed (spec §6.1, §10.5). All in-game-clock calculations downstream
-- read from here.
CREATE TABLE IF NOT EXISTS games_live (
    game_id               TEXT PRIMARY KEY,             -- Riot's esports gameId
    league                TEXT NOT NULL,                -- "lck", "lcs", etc.
    blue_team_code        TEXT,                          -- e.g. "TLAW" (from participant prefixes)
    red_team_code         TEXT,
    blue_esports_team_id  TEXT,                          -- Riot's team identifier
    red_esports_team_id   TEXT,
    first_seen_ts_unix    INTEGER NOT NULL,             -- when game_discovery first picked it up
    game_start_ts_unix    INTEGER,                       -- earliest in_game frame's rfc460Timestamp; NULL until detected
    game_end_ts_unix      INTEGER,                       -- when state transitions to ended (>2 min)
    api_min_delay_sec     INTEGER,                       -- adaptive delay probe result
    match_id              INTEGER,                       -- link to v1 matches table once known
    notes                 TEXT,
    FOREIGN KEY (match_id) REFERENCES matches(match_id)
);

CREATE INDEX IF NOT EXISTS idx_games_live_first_seen ON games_live(first_seen_ts_unix);
CREATE INDEX IF NOT EXISTS idx_games_live_match      ON games_live(match_id);


-- One row per livestats frame ingested by livestats_poller.
-- Dedup on (game_id, frame_ts_unix) to handle out-of-order responses (spec §17 #7).
CREATE TABLE IF NOT EXISTS live_frames (
    frame_id              INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id               TEXT NOT NULL,
    frame_ts_unix         INTEGER NOT NULL,             -- rfc460Timestamp converted to unix
    fetched_ts_unix       INTEGER NOT NULL,             -- when we pulled it (for latency tracking)
    game_state            TEXT NOT NULL,                -- "in_game", "paused", "finished", "pre_game"
    -- Parsed team-level fields (denormalized for query speed; raw JSON kept too):
    blue_gold             INTEGER,
    blue_kills            INTEGER,
    blue_towers           INTEGER,
    blue_inhibitors       INTEGER,
    blue_dragons_json     TEXT,                          -- JSON array of elemental types
    blue_barons           INTEGER,
    red_gold              INTEGER,
    red_kills             INTEGER,
    red_towers            INTEGER,
    red_inhibitors        INTEGER,
    red_dragons_json      TEXT,
    red_barons            INTEGER,
    -- Raw payload, in case we need fields we didn't parse:
    raw_json              BLOB,
    FOREIGN KEY (game_id) REFERENCES games_live(game_id),
    UNIQUE (game_id, frame_ts_unix)                       -- dedup key (spec §17 #7)
);

CREATE INDEX IF NOT EXISTS idx_live_frames_game_ts ON live_frames(game_id, frame_ts_unix);
CREATE INDEX IF NOT EXISTS idx_live_frames_state   ON live_frames(game_state);
