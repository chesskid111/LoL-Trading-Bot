-- 004_features.sql
-- Per-game team Glicko-2 snapshots.
-- One row per (team_id, game_id) after the team's rating updates from
-- playing that game. Lookup pattern:
--   "give me <team>'s rating as of date t" =
--     SELECT * FROM team_glicko_snapshots
--     WHERE team_id = ? AND after_date < ?
--     ORDER BY after_date DESC LIMIT 1

CREATE TABLE IF NOT EXISTS team_glicko_snapshots (
    team_id          INTEGER NOT NULL,
    after_game_id    INTEGER NOT NULL,
    after_date       TEXT NOT NULL,        -- denormalized YYYY-MM-DD for fast queries
    mu               REAL NOT NULL,
    phi              REAL NOT NULL,
    sigma            REAL NOT NULL,
    rating           REAL NOT NULL,        -- mu * 173.7178 + 1500, denormalized
    rd               REAL NOT NULL,        -- phi * 173.7178
    PRIMARY KEY (team_id, after_game_id),
    FOREIGN KEY (team_id) REFERENCES teams(team_id),
    FOREIGN KEY (after_game_id) REFERENCES match_games(game_id)
);

CREATE INDEX IF NOT EXISTS idx_glicko_team_date
    ON team_glicko_snapshots(team_id, after_date);
