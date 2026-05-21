"""ETL for Riot Data Dragon champion metadata into the champions table.

Reads ``data/raw/champions.json`` (downloaded via curl from
``https://ddragon.leagueoflegends.com/cdn/<version>/data/en_US/champion.json``)
and UPSERTs each champion's tags.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path

log = logging.getLogger(__name__)

TAG_COLUMNS = {
    "Fighter":  "has_fighter",
    "Mage":     "has_mage",
    "Assassin": "has_assassin",
    "Marksman": "has_marksman",
    "Tank":     "has_tank",
    "Support":  "has_support",
}


def etl_champions(conn: sqlite3.Connection, json_path: Path) -> int:
    """Load champion metadata from a Data Dragon JSON file. Returns # upserts."""
    if not json_path.exists():
        raise FileNotFoundError(
            f"Champion JSON not found at {json_path}. "
            f"Fetch with: curl https://ddragon.leagueoflegends.com/cdn/<ver>/data/en_US/champion.json"
        )
    raw = json.loads(json_path.read_text(encoding="utf-8"))
    champs = raw["data"]
    now = int(time.time())
    upserts = 0
    for _, c in champs.items():
        name = c["id"]
        tags = c.get("tags", []) or []
        flags = {col: 0 for col in TAG_COLUMNS.values()}
        for t in tags:
            if t in TAG_COLUMNS:
                flags[TAG_COLUMNS[t]] = 1
        conn.execute(
            """
            INSERT INTO champions
                (champion_name, riot_key, tags,
                 has_fighter, has_mage, has_assassin, has_marksman, has_tank, has_support,
                 fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(champion_name) DO UPDATE SET
                riot_key=excluded.riot_key,
                tags=excluded.tags,
                has_fighter=excluded.has_fighter,
                has_mage=excluded.has_mage,
                has_assassin=excluded.has_assassin,
                has_marksman=excluded.has_marksman,
                has_tank=excluded.has_tank,
                has_support=excluded.has_support,
                fetched_at=excluded.fetched_at
            """,
            (
                name, c.get("key"), ",".join(tags),
                flags["has_fighter"], flags["has_mage"], flags["has_assassin"],
                flags["has_marksman"], flags["has_tank"], flags["has_support"],
                now,
            ),
        )
        upserts += 1
    conn.commit()
    log.info("etl_champions: %d champions upserted", upserts)
    return upserts
