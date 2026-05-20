-- 002_oracle.sql
-- Pro match data sourced from Oracle's Elixir CSVs.
--
-- Granularity:
--   patches         : one per game version (e.g., 14.10)
--   teams           : one per canonical team name
--   players         : one per player (by Oracle's playerid)
--   matches         : one per series (Bo1 = 1 game; Bo3/Bo5 = multiple games)
--   match_games     : one per individual game within a series
--   match_drafts    : one per pick/ban (5 picks + 5 bans per team per game = 20 rows per game)
--   match_player_stats : one per player per game (5 per team per game = 10 rows per game)

CREATE TABLE IF NOT EXISTS patches (
    patch_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    version        TEXT NOT NULL UNIQUE,         -- e.g., "14.10"
    first_seen     TEXT,                          -- ISO date of earliest game on this patch
    last_seen      TEXT                           -- ISO date of latest game on this patch (updated by ETL)
);

CREATE INDEX IF NOT EXISTS idx_patches_version ON patches(version);


CREATE TABLE IF NOT EXISTS teams (
    team_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    oracle_teamid    TEXT UNIQUE,                  -- Oracle's Elixir teamid (can be NULL for old rows)
    canonical_name   TEXT NOT NULL UNIQUE,
    region           TEXT,                          -- e.g., "LCK", "LPL", "LEC", "LCS"
    first_seen       TEXT,
    last_seen        TEXT
);

CREATE INDEX IF NOT EXISTS idx_teams_canonical ON teams(canonical_name);


CREATE TABLE IF NOT EXISTS players (
    player_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    oracle_playerid  TEXT UNIQUE,
    ign              TEXT NOT NULL,
    role             TEXT,                          -- top / jng / mid / bot / sup
    first_seen       TEXT,
    last_seen        TEXT
);

CREATE INDEX IF NOT EXISTS idx_players_ign ON players(ign);


-- A series. For Bo1 leagues this maps 1:1 to a single game; for Bo3/Bo5
-- multiple match_games share the same match_id.
CREATE TABLE IF NOT EXISTS matches (
    match_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    -- Stable natural key for dedup: date + team_a_id + team_b_id (ordered)
    match_key        TEXT NOT NULL UNIQUE,
    date             TEXT NOT NULL,                 -- ISO date (YYYY-MM-DD)
    league           TEXT,                          -- LCK / LPL / LEC / LCS / Worlds / ...
    split            TEXT,                          -- Spring / Summer / Worlds / ...
    playoffs         INTEGER,                       -- bool 0/1
    patch_id         INTEGER,
    team_a_id        INTEGER NOT NULL,              -- lex-smaller canonical_name
    team_b_id        INTEGER NOT NULL,
    series_winner_id INTEGER,                       -- NULL until decided
    bo_format        INTEGER,                       -- 1 / 3 / 5
    FOREIGN KEY (patch_id) REFERENCES patches(patch_id),
    FOREIGN KEY (team_a_id) REFERENCES teams(team_id),
    FOREIGN KEY (team_b_id) REFERENCES teams(team_id),
    FOREIGN KEY (series_winner_id) REFERENCES teams(team_id)
);

CREATE INDEX IF NOT EXISTS idx_matches_date  ON matches(date);
CREATE INDEX IF NOT EXISTS idx_matches_teams ON matches(team_a_id, team_b_id);


CREATE TABLE IF NOT EXISTS match_games (
    game_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    oracle_gameid    TEXT UNIQUE,                   -- Oracle's gameid for traceability
    match_id         INTEGER NOT NULL,
    game_number      INTEGER NOT NULL,              -- 1, 2, 3, ... within the match
    blue_team_id     INTEGER NOT NULL,
    red_team_id      INTEGER NOT NULL,
    winner_team_id   INTEGER,                       -- NULL if game in progress (shouldn't happen post-ETL)
    duration_sec     INTEGER,
    patch_id         INTEGER,
    FOREIGN KEY (match_id) REFERENCES matches(match_id),
    FOREIGN KEY (blue_team_id) REFERENCES teams(team_id),
    FOREIGN KEY (red_team_id) REFERENCES teams(team_id),
    FOREIGN KEY (winner_team_id) REFERENCES teams(team_id),
    FOREIGN KEY (patch_id) REFERENCES patches(patch_id)
);

CREATE INDEX IF NOT EXISTS idx_match_games_match ON match_games(match_id);


CREATE TABLE IF NOT EXISTS match_drafts (
    draft_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id      INTEGER NOT NULL,
    team_id      INTEGER NOT NULL,
    is_ban       INTEGER NOT NULL,                  -- 0=pick, 1=ban
    pick_order   INTEGER NOT NULL,                  -- 1..5
    champion     TEXT NOT NULL,
    role         TEXT,                              -- top/jng/mid/bot/sup for picks; NULL for bans
    FOREIGN KEY (game_id) REFERENCES match_games(game_id),
    FOREIGN KEY (team_id) REFERENCES teams(team_id),
    UNIQUE (game_id, team_id, is_ban, pick_order)
);

CREATE INDEX IF NOT EXISTS idx_match_drafts_game ON match_drafts(game_id);


CREATE TABLE IF NOT EXISTS match_player_stats (
    stat_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id          INTEGER NOT NULL,
    player_id        INTEGER NOT NULL,
    team_id          INTEGER NOT NULL,
    role             TEXT,                          -- top/jng/mid/bot/sup
    champion         TEXT,
    kills            INTEGER,
    deaths           INTEGER,
    assists          INTEGER,
    cs               INTEGER,                       -- total minions+monsters killed
    gold             INTEGER,                       -- total gold earned
    damage_to_champs INTEGER,
    vision_score     INTEGER,
    FOREIGN KEY (game_id) REFERENCES match_games(game_id),
    FOREIGN KEY (player_id) REFERENCES players(player_id),
    FOREIGN KEY (team_id) REFERENCES teams(team_id),
    UNIQUE (game_id, player_id)
);

CREATE INDEX IF NOT EXISTS idx_match_player_stats_game ON match_player_stats(game_id);
