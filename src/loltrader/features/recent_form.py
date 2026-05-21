"""Recent-form features: rolling winrates over the last N games."""
from __future__ import annotations

import sqlite3


def _winrate(conn: sqlite3.Connection, team_id: int, as_of_date: str, n: int) -> float:
    """Win rate over the team's last n games strictly before as_of_date.
    Returns 0.5 (a flat prior) if fewer than 3 games are available."""
    rows = conn.execute(
        """
        SELECT mg.winner_team_id
        FROM match_games mg
        JOIN matches m ON m.match_id = mg.match_id
        WHERE m.date < ?
          AND (mg.blue_team_id = ? OR mg.red_team_id = ?)
          AND mg.winner_team_id IS NOT NULL
        ORDER BY m.date DESC, mg.game_id DESC
        LIMIT ?
        """,
        (as_of_date, team_id, team_id, n),
    ).fetchall()
    if len(rows) < 3:
        return 0.5
    wins = sum(1 for r in rows if r["winner_team_id"] == team_id)
    return wins / len(rows)


def _winrate_on_patch(
    conn: sqlite3.Connection,
    team_id: int,
    as_of_date: str,
    patch_id: int | None,
) -> float:
    """Win rate on the current patch (rolling, no n-cap).
    Returns 0.5 if patch is unknown or no games."""
    if patch_id is None:
        return 0.5
    row = conn.execute(
        """
        SELECT
            SUM(CASE WHEN mg.winner_team_id = ? THEN 1 ELSE 0 END) AS wins,
            COUNT(*) AS total
        FROM match_games mg
        JOIN matches m ON m.match_id = mg.match_id
        WHERE m.date < ?
          AND (mg.blue_team_id = ? OR mg.red_team_id = ?)
          AND mg.patch_id = ?
          AND mg.winner_team_id IS NOT NULL
        """,
        (team_id, as_of_date, team_id, team_id, patch_id),
    ).fetchone()
    if not row or not row["total"]:
        return 0.5
    return row["wins"] / row["total"]


def recent_form_features(
    conn: sqlite3.Connection,
    team_a_id: int,
    team_b_id: int,
    as_of_date: str,
    patch_id: int | None,
) -> dict[str, float]:
    return {
        "team_a_winrate_5":      _winrate(conn, team_a_id, as_of_date, 5),
        "team_a_winrate_10":     _winrate(conn, team_a_id, as_of_date, 10),
        "team_a_winrate_20":     _winrate(conn, team_a_id, as_of_date, 20),
        "team_a_winrate_patch":  _winrate_on_patch(conn, team_a_id, as_of_date, patch_id),
        "team_b_winrate_5":      _winrate(conn, team_b_id, as_of_date, 5),
        "team_b_winrate_10":     _winrate(conn, team_b_id, as_of_date, 10),
        "team_b_winrate_20":     _winrate(conn, team_b_id, as_of_date, 20),
        "team_b_winrate_patch":  _winrate_on_patch(conn, team_b_id, as_of_date, patch_id),
    }
