"""Schedule features: rest days, back-to-back, recent activity."""
from __future__ import annotations

import sqlite3
from datetime import datetime


def _days_since_last_game(
    conn: sqlite3.Connection, team_id: int, as_of_date: str
) -> int | None:
    row = conn.execute(
        """
        SELECT MAX(m.date) AS last_date
        FROM match_games mg
        JOIN matches m ON m.match_id = mg.match_id
        WHERE m.date < ?
          AND (mg.blue_team_id = ? OR mg.red_team_id = ?)
        """,
        (as_of_date, team_id, team_id),
    ).fetchone()
    last = row["last_date"] if row else None
    if not last:
        return None
    try:
        a = datetime.strptime(as_of_date, "%Y-%m-%d")
        b = datetime.strptime(last, "%Y-%m-%d")
        return max(0, (a - b).days)
    except ValueError:
        return None


def schedule_features(
    conn: sqlite3.Connection,
    team_a_id: int,
    team_b_id: int,
    as_of_date: str,
) -> dict[str, float]:
    rest_a = _days_since_last_game(conn, team_a_id, as_of_date)
    rest_b = _days_since_last_game(conn, team_b_id, as_of_date)
    return {
        # 999 = no prior game / debutant team
        "team_a_rest_days": float(rest_a if rest_a is not None else 999),
        "team_b_rest_days": float(rest_b if rest_b is not None else 999),
        "rest_diff_a_minus_b": float(
            (rest_a if rest_a is not None else 0)
            - (rest_b if rest_b is not None else 0)
        ),
        "team_a_back_to_back": float(rest_a is not None and rest_a <= 1),
        "team_b_back_to_back": float(rest_b is not None and rest_b <= 1),
    }
