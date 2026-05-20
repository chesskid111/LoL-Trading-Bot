-- 003_linkage.sql
-- Match-to-market linkage: maps a Kalshi market ticker to a specific
-- pro match (and optionally a game within it). Also: team_aliases for
-- canonicalizing Kalshi's team-name variants to Oracle's Elixir names.

CREATE TABLE IF NOT EXISTS team_aliases (
    alias_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    alias           TEXT NOT NULL UNIQUE,        -- e.g., "C9", "Cloud9 Esports"
    canonical_name  TEXT NOT NULL,                -- must match teams.canonical_name
    source          TEXT,                          -- "seed" / "manual" / "auto"
    created_at      INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_team_aliases_canonical ON team_aliases(canonical_name);


CREATE TABLE IF NOT EXISTS market_match_links (
    link_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    market_ticker      TEXT NOT NULL UNIQUE,
    match_id           INTEGER,                    -- NULL while unlinked
    game_id            INTEGER,                    -- NULL for series-winner markets; set for KXLOLMAP-...-N
    -- For YES contract: which team is it? 1 = team_a, 2 = team_b. Used to
    -- map "YES wins" back to a specific team's outcome.
    side               INTEGER,
    confidence         REAL NOT NULL,              -- 0.0 .. 1.0, per spec rubric
    manual_override    INTEGER NOT NULL DEFAULT 0, -- bool: was this set by user CLI?
    notes              TEXT,
    linked_at          INTEGER NOT NULL,
    FOREIGN KEY (market_ticker) REFERENCES kalshi_markets(market_ticker),
    FOREIGN KEY (match_id) REFERENCES matches(match_id),
    FOREIGN KEY (game_id) REFERENCES match_games(game_id)
);

CREATE INDEX IF NOT EXISTS idx_market_match_links_confidence ON market_match_links(confidence);
CREATE INDEX IF NOT EXISTS idx_market_match_links_match      ON market_match_links(match_id);


CREATE TABLE IF NOT EXISTS manual_review (
    review_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    market_ticker  TEXT NOT NULL UNIQUE,
    reason         TEXT NOT NULL,            -- "no_match" / "low_confidence" / "ambiguous"
    parsed_team_a  TEXT,
    parsed_team_b  TEXT,
    parsed_date    TEXT,
    created_at     INTEGER NOT NULL,
    resolved_at    INTEGER,
    FOREIGN KEY (market_ticker) REFERENCES kalshi_markets(market_ticker)
);

CREATE INDEX IF NOT EXISTS idx_manual_review_unresolved
    ON manual_review(resolved_at) WHERE resolved_at IS NULL;
