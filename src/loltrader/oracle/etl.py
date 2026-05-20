"""Oracle's Elixir CSV -> SQLite ETL.

Each CSV row in Oracle's Elixir format represents either:
  - one player's performance in one game (10 rows per game), or
  - one team's aggregate stats for that game (2 rows per game).

A "game" is identified by ``gameid``. A "series" (a.k.a. match) groups
games played the same day by the same two teams; we derive a stable
match_key from (date, lex-sorted team_a, team_b) for dedup.

The ETL is idempotent: re-running on the same CSV is a no-op except for
``last_seen`` updates on teams/players/patches.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)

# Leagues we care about for v1. Oracle's `league` column uses these codes.
# (Plus a few sometimes-spelled variants we map below.)
KEEP_LEAGUES: set[str] = {
    # Majors
    "LCK", "LPL", "LEC", "LCS", "LTA", "LTAN", "LTAS",
    # International / event tournaments
    "MSI", "Worlds", "WLDs", "First Stand", "FS",
    "EWC", "ENC", "Esports World Cup",
    # Minor regions we may include if Kalshi lists them
    "VCS", "PCS", "CBLOL", "LJL",
}


# --- helpers --------------------------------------------------------------

def _upsert_patch(conn: sqlite3.Connection, version: str, date: str) -> int:
    row = conn.execute(
        "SELECT patch_id, first_seen, last_seen FROM patches WHERE version = ?",
        (version,),
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO patches (version, first_seen, last_seen) VALUES (?, ?, ?)",
            (version, date, date),
        )
        return conn.execute(
            "SELECT patch_id FROM patches WHERE version = ?", (version,)
        ).fetchone()[0]
    pid = row["patch_id"]
    fs = min(row["first_seen"] or date, date)
    ls = max(row["last_seen"] or date, date)
    conn.execute(
        "UPDATE patches SET first_seen = ?, last_seen = ? WHERE patch_id = ?",
        (fs, ls, pid),
    )
    return pid


def _upsert_team(
    conn: sqlite3.Connection,
    oracle_teamid: str | None,
    teamname: str,
    region: str,
    date: str,
) -> int:
    # Prefer oracle_teamid as lookup key; fall back to canonical_name
    row = None
    if oracle_teamid:
        row = conn.execute(
            "SELECT team_id, first_seen, last_seen FROM teams WHERE oracle_teamid = ?",
            (oracle_teamid,),
        ).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT team_id, first_seen, last_seen FROM teams WHERE canonical_name = ?",
            (teamname,),
        ).fetchone()
    if row is None:
        conn.execute(
            """
            INSERT INTO teams (oracle_teamid, canonical_name, region, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?)
            """,
            (oracle_teamid, teamname, region, date, date),
        )
        return conn.execute(
            "SELECT team_id FROM teams WHERE canonical_name = ?", (teamname,)
        ).fetchone()[0]
    tid = row["team_id"]
    fs = min(row["first_seen"] or date, date)
    ls = max(row["last_seen"] or date, date)
    conn.execute(
        "UPDATE teams SET first_seen = ?, last_seen = ?, oracle_teamid = COALESCE(oracle_teamid, ?) "
        "WHERE team_id = ?",
        (fs, ls, oracle_teamid, tid),
    )
    return tid


def _upsert_player(
    conn: sqlite3.Connection,
    oracle_playerid: str | None,
    ign: str,
    role: str,
    date: str,
) -> int:
    row = None
    if oracle_playerid:
        row = conn.execute(
            "SELECT player_id, first_seen, last_seen FROM players WHERE oracle_playerid = ?",
            (oracle_playerid,),
        ).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT player_id, first_seen, last_seen FROM players WHERE ign = ?",
            (ign,),
        ).fetchone()
    if row is None:
        conn.execute(
            """
            INSERT INTO players (oracle_playerid, ign, role, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?)
            """,
            (oracle_playerid, ign, role, date, date),
        )
        return conn.execute(
            "SELECT player_id FROM players WHERE ign = ?", (ign,)
        ).fetchone()[0]
    pid = row["player_id"]
    fs = min(row["first_seen"] or date, date)
    ls = max(row["last_seen"] or date, date)
    conn.execute(
        "UPDATE players SET first_seen = ?, last_seen = ?, role = COALESCE(role, ?) WHERE player_id = ?",
        (fs, ls, role, pid),
    )
    return pid


def _to_int(v) -> int | None:
    if v is None or pd.isna(v):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _match_key(date: str, team_a_name: str, team_b_name: str) -> str:
    a, b = sorted([team_a_name, team_b_name])
    return f"{date}|{a}|{b}"


# --- main entry -----------------------------------------------------------

def etl_csv(conn: sqlite3.Connection, csv_path: Path) -> dict[str, int]:
    """ETL one Oracle's Elixir CSV into SQLite. Idempotent."""
    log.info("Reading %s", csv_path)
    df = pd.read_csv(csv_path, low_memory=False)
    log.info("Loaded %d rows from %s", len(df), csv_path.name)

    # Filter to leagues we care about
    df = df[df["league"].isin(KEEP_LEAGUES)].copy()
    log.info("After league filter: %d rows", len(df))

    # Drop wholly-broken rows but keep "partial" — Oracle marks all LPL
    # data as "partial" (no per-minute stats, but draft + results + team
    # stats are still present, which is what v1's model needs).
    if "datacompleteness" in df.columns:
        before = len(df)
        df = df[df["datacompleteness"].isin(["complete", "partial"])].copy()
        log.info("After completeness filter: %d rows (was %d)", len(df), before)

    # Group by gameid; each group is one game
    counts = {"games": 0, "matches": 0, "drafts": 0, "player_stats": 0}
    series_seen: set[str] = set()

    for gameid, gdf in df.groupby("gameid"):
        if gdf.empty:
            continue

        # Game-level info from any row
        first = gdf.iloc[0]
        date_raw = str(first.get("date"))
        date = date_raw[:10]  # YYYY-MM-DD
        league = str(first.get("league"))
        split = str(first.get("split")) if pd.notna(first.get("split")) else None
        playoffs = int(first.get("playoffs") or 0) if pd.notna(first.get("playoffs")) else 0
        patch_version = str(first.get("patch")) if pd.notna(first.get("patch")) else None
        game_number = _to_int(first.get("game")) or 1

        # Two team-aggregate rows have position == "team"
        team_rows = gdf[gdf["position"] == "team"]
        if len(team_rows) != 2:
            # Some early data has different formatting; fall back to side
            team_rows = gdf.groupby("side").first().reset_index()
            if len(team_rows) != 2:
                continue

        # Identify blue / red
        blue = team_rows[team_rows["side"] == "Blue"]
        red = team_rows[team_rows["side"] == "Red"]
        if len(blue) == 0 or len(red) == 0:
            continue
        blue = blue.iloc[0]
        red = red.iloc[0]

        blue_name = str(blue["teamname"])
        red_name = str(red["teamname"])
        if blue_name == "nan" or red_name == "nan":
            continue

        blue_oracle_id = str(blue.get("teamid")) if pd.notna(blue.get("teamid")) else None
        red_oracle_id = str(red.get("teamid")) if pd.notna(red.get("teamid")) else None

        # UPSERTs
        patch_id = _upsert_patch(conn, patch_version, date) if patch_version else None
        blue_team_id = _upsert_team(conn, blue_oracle_id, blue_name, league, date)
        red_team_id = _upsert_team(conn, red_oracle_id, red_name, league, date)

        # Series ID = match_key dedup
        match_key = _match_key(date, blue_name, red_name)
        team_a_name, team_b_name = sorted([blue_name, red_name])
        team_a_id = blue_team_id if blue_name == team_a_name else red_team_id
        team_b_id = red_team_id if red_name == team_b_name else blue_team_id

        match_row = conn.execute(
            "SELECT match_id FROM matches WHERE match_key = ?", (match_key,)
        ).fetchone()
        if match_row is None:
            conn.execute(
                """
                INSERT INTO matches
                    (match_key, date, league, split, playoffs, patch_id,
                     team_a_id, team_b_id, bo_format)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (match_key, date, league, split, playoffs, patch_id,
                 team_a_id, team_b_id, 1),  # bo_format updated below
            )
            match_id = conn.execute(
                "SELECT match_id FROM matches WHERE match_key = ?", (match_key,)
            ).fetchone()[0]
            counts["matches"] += 1
        else:
            match_id = match_row[0]

        if match_key not in series_seen:
            series_seen.add(match_key)

        # Insert or update match_games (use oracle_gameid as natural key)
        oracle_gameid = str(gameid)
        winner_team_id = blue_team_id if int(blue["result"]) == 1 else red_team_id
        duration = _to_int(first.get("gamelength"))

        existing_game = conn.execute(
            "SELECT game_id FROM match_games WHERE oracle_gameid = ?", (oracle_gameid,)
        ).fetchone()
        if existing_game is None:
            conn.execute(
                """
                INSERT INTO match_games
                    (oracle_gameid, match_id, game_number, blue_team_id, red_team_id,
                     winner_team_id, duration_sec, patch_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (oracle_gameid, match_id, game_number, blue_team_id, red_team_id,
                 winner_team_id, duration, patch_id),
            )
            game_id = conn.execute(
                "SELECT game_id FROM match_games WHERE oracle_gameid = ?", (oracle_gameid,)
            ).fetchone()[0]
            counts["games"] += 1
        else:
            game_id = existing_game[0]

        # Drafts: per-team pick1..5 and ban1..5
        for team_row, team_id in [(blue, blue_team_id), (red, red_team_id)]:
            for i in range(1, 6):
                pick_col = f"pick{i}"
                ban_col = f"ban{i}"
                pick_v = team_row.get(pick_col)
                ban_v = team_row.get(ban_col)
                if pd.notna(pick_v) and str(pick_v) != "nan":
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO match_drafts
                            (game_id, team_id, is_ban, pick_order, champion, role)
                        VALUES (?, ?, 0, ?, ?, NULL)
                        """,
                        (game_id, team_id, i, str(pick_v)),
                    )
                    counts["drafts"] += 1
                if pd.notna(ban_v) and str(ban_v) != "nan":
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO match_drafts
                            (game_id, team_id, is_ban, pick_order, champion, role)
                        VALUES (?, ?, 1, ?, ?, NULL)
                        """,
                        (game_id, team_id, i, str(ban_v)),
                    )
                    counts["drafts"] += 1

        # Player stats: 10 rows per game with position in {top, jng, mid, bot, sup}
        player_rows = gdf[gdf["position"].isin(["top", "jng", "mid", "bot", "sup"])]
        for _, pr in player_rows.iterrows():
            ign = str(pr.get("playername") or "")
            if not ign or ign == "nan":
                continue
            oracle_playerid = str(pr.get("playerid")) if pd.notna(pr.get("playerid")) else None
            role = str(pr.get("position"))
            player_id = _upsert_player(conn, oracle_playerid, ign, role, date)
            team_id = blue_team_id if pr["side"] == "Blue" else red_team_id
            conn.execute(
                """
                INSERT OR IGNORE INTO match_player_stats
                    (game_id, player_id, team_id, role, champion,
                     kills, deaths, assists, cs, gold, damage_to_champs, vision_score)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    game_id, player_id, team_id, role,
                    str(pr.get("champion")) if pd.notna(pr.get("champion")) else None,
                    _to_int(pr.get("kills")),
                    _to_int(pr.get("deaths")),
                    _to_int(pr.get("assists")),
                    _to_int(pr.get("total cs")),
                    _to_int(pr.get("totalgold")),
                    _to_int(pr.get("damagetochampions")),
                    _to_int(pr.get("visionscore")),
                ),
            )
            counts["player_stats"] += 1

    conn.commit()

    # After all games inserted, update bo_format and series_winner per series
    log.info("Computing per-series bo_format and winner")
    _finalize_series(conn)

    return counts


def _finalize_series(conn: sqlite3.Connection) -> None:
    """After insert: set bo_format from max game_number per series, and
    set series_winner_id from whichever team won the majority of games."""
    # Update bo_format from games
    conn.execute(
        """
        UPDATE matches AS m
        SET bo_format = (
            SELECT
                CASE
                    WHEN MAX(g.game_number) = 1 THEN 1
                    WHEN MAX(g.game_number) <= 3 THEN 3
                    ELSE 5
                END
            FROM match_games g WHERE g.match_id = m.match_id
        )
        WHERE EXISTS (SELECT 1 FROM match_games g WHERE g.match_id = m.match_id)
        """
    )
    # Update series_winner: team with strictly more wins among the games
    conn.execute(
        """
        UPDATE matches AS m
        SET series_winner_id = (
            WITH wins AS (
                SELECT
                    g.winner_team_id AS w,
                    COUNT(*) AS n
                FROM match_games g
                WHERE g.match_id = m.match_id AND g.winner_team_id IS NOT NULL
                GROUP BY g.winner_team_id
            )
            SELECT w FROM wins ORDER BY n DESC LIMIT 1
        )
        WHERE series_winner_id IS NULL
        """
    )
    conn.commit()


def etl_all(conn: sqlite3.Connection, raw_dir: Path) -> dict[str, dict[str, int]]:
    """Run ETL on every CSV under raw_dir matching the expected name pattern.
    Returns per-file counts."""
    results: dict[str, dict[str, int]] = {}
    csvs = sorted(raw_dir.glob("*_LoL_esports_match_data_from_OraclesElixir.csv"))
    if not csvs:
        log.warning("No Oracle's Elixir CSVs found under %s", raw_dir)
        return results
    for csv in csvs:
        start = time.time()
        counts = etl_csv(conn, csv)
        elapsed = time.time() - start
        log.info("%s: %s in %.1fs", csv.name, counts, elapsed)
        results[csv.name] = counts
    return results
