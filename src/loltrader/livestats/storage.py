"""SQLite writes for livestats: games_live, live_frames.

Spec §6.1 (Riot livestats), §10.5 (cache game-start on first detection),
§17 #7 (frame-order dedup via UNIQUE(game_id, frame_ts_unix)).
"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any


def parse_rfc460_to_unix(ts_str: str) -> int:
    """Parse a Riot livestats timestamp (RFC 460-ish, e.g. '2026-05-24T22:21:29.817Z')
    into a unix-second integer. Sub-second precision is dropped — sufficient for
    a 10s frame cadence."""
    # Strip any fractional seconds + 'Z' tail
    base = ts_str[:19]
    dt = datetime.strptime(base, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def register_game_first_seen(
    conn: sqlite3.Connection,
    game_id: str,
    league_slug: str,
    blue_team_code: str | None = None,
    red_team_code: str | None = None,
    blue_esports_team_id: str | None = None,
    red_esports_team_id: str | None = None,
) -> None:
    """Insert a row into games_live on first detection. Idempotent.

    Does NOT set game_start_ts_unix — that's done later via
    set_game_start_if_unset() once we observe an in_game frame.
    """
    now = int(time.time())
    conn.execute(
        """
        INSERT INTO games_live (
            game_id, league, blue_team_code, red_team_code,
            blue_esports_team_id, red_esports_team_id,
            first_seen_ts_unix
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(game_id) DO UPDATE SET
            -- Only fill in fields that are NULL; preserve first-seen values.
            league = COALESCE(games_live.league, excluded.league),
            blue_team_code = COALESCE(games_live.blue_team_code, excluded.blue_team_code),
            red_team_code = COALESCE(games_live.red_team_code, excluded.red_team_code),
            blue_esports_team_id = COALESCE(games_live.blue_esports_team_id, excluded.blue_esports_team_id),
            red_esports_team_id = COALESCE(games_live.red_esports_team_id, excluded.red_esports_team_id)
        """,
        (game_id, league_slug, blue_team_code, red_team_code,
         blue_esports_team_id, red_esports_team_id, now),
    )
    conn.commit()


def set_game_start_if_unset(
    conn: sqlite3.Connection,
    game_id: str,
    game_start_ts_unix: int,
) -> bool:
    """Set games_live.game_start_ts_unix exactly once per game (spec §6.1, §10.5).

    Returns True if we set it; False if it was already set. The "never re-probe"
    rule from spec §10.5 is enforced by this UPDATE only firing when the column
    is NULL.
    """
    cursor = conn.execute(
        """
        UPDATE games_live
        SET game_start_ts_unix = ?
        WHERE game_id = ? AND game_start_ts_unix IS NULL
        """,
        (game_start_ts_unix, game_id),
    )
    conn.commit()
    return cursor.rowcount > 0


def set_game_end(
    conn: sqlite3.Connection,
    game_id: str,
    game_end_ts_unix: int,
) -> None:
    """Mark a game as ended (called when state=finished sustains for >2 min)."""
    conn.execute(
        "UPDATE games_live SET game_end_ts_unix = ? WHERE game_id = ?",
        (game_end_ts_unix, game_id),
    )
    conn.commit()


def set_adaptive_delay(
    conn: sqlite3.Connection,
    game_id: str,
    delay_sec: int,
) -> None:
    """Cache the probed minimum API delay for a game."""
    conn.execute(
        "UPDATE games_live SET api_min_delay_sec = ? WHERE game_id = ?",
        (delay_sec, game_id),
    )
    conn.commit()


def write_frame(
    conn: sqlite3.Connection,
    game_id: str,
    frame: dict[str, Any],
) -> bool:
    """Upsert one livestats frame into live_frames.

    Returns True if a new row was inserted, False if it was a duplicate
    (game_id + frame_ts already present — spec §17 #7).

    Also parses the frame's gameState=in_game timestamp into game_start_ts
    via set_game_start_if_unset() when applicable.
    """
    ts_str = frame.get("rfc460Timestamp")
    if not ts_str:
        return False
    try:
        frame_ts_unix = parse_rfc460_to_unix(ts_str)
    except ValueError:
        return False

    blue = frame.get("blueTeam") or {}
    red = frame.get("redTeam") or {}
    fetched_ts = int(time.time())

    cursor = conn.execute(
        """
        INSERT INTO live_frames (
            game_id, frame_ts_unix, fetched_ts_unix, game_state,
            blue_gold, blue_kills, blue_towers, blue_inhibitors,
            blue_dragons_json, blue_barons,
            red_gold, red_kills, red_towers, red_inhibitors,
            red_dragons_json, red_barons,
            raw_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(game_id, frame_ts_unix) DO NOTHING
        """,
        (
            game_id, frame_ts_unix, fetched_ts, frame.get("gameState", "unknown"),
            blue.get("totalGold"), blue.get("totalKills"),
            blue.get("towers"), blue.get("inhibitors"),
            json.dumps(blue.get("dragons", []) or []), blue.get("barons"),
            red.get("totalGold"), red.get("totalKills"),
            red.get("towers"), red.get("inhibitors"),
            json.dumps(red.get("dragons", []) or []), red.get("barons"),
            json.dumps(frame),
        ),
    )
    inserted = cursor.rowcount > 0
    if inserted and frame.get("gameState") == "in_game":
        # First in_game frame we observe → cache as game start (spec §6.1).
        set_game_start_if_unset(conn, game_id, frame_ts_unix)
    conn.commit()
    return inserted


def get_latest_frame_ts(conn: sqlite3.Connection, game_id: str) -> int | None:
    """Return the latest frame_ts_unix observed for a game, or None."""
    row = conn.execute(
        "SELECT MAX(frame_ts_unix) AS m FROM live_frames WHERE game_id = ?",
        (game_id,),
    ).fetchone()
    return row["m"] if row and row["m"] is not None else None


def get_game_state(conn: sqlite3.Connection, game_id: str) -> dict[str, Any] | None:
    """Return the games_live row for a game, or None if not registered."""
    row = conn.execute(
        "SELECT * FROM games_live WHERE game_id = ?", (game_id,),
    ).fetchone()
    return dict(row) if row else None
