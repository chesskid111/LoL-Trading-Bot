-- 005_champions.sql
-- Champion metadata from Riot Data Dragon.
-- Tags are a comma-separated list of Riot's classifications:
-- {Fighter, Mage, Assassin, Marksman, Tank, Support}

CREATE TABLE IF NOT EXISTS champions (
    champion_name   TEXT PRIMARY KEY,         -- "Aatrox", "Kha'Zix", etc.
    riot_key        TEXT,                      -- numeric "266"
    tags            TEXT NOT NULL,             -- comma-separated tags
    has_fighter     INTEGER NOT NULL DEFAULT 0,
    has_mage        INTEGER NOT NULL DEFAULT 0,
    has_assassin    INTEGER NOT NULL DEFAULT 0,
    has_marksman    INTEGER NOT NULL DEFAULT 0,
    has_tank        INTEGER NOT NULL DEFAULT 0,
    has_support     INTEGER NOT NULL DEFAULT 0,
    fetched_at      INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_champions_tags ON champions(tags);
