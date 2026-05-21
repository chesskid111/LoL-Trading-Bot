"""Head-to-head (H2H) features.

Two windows: last 6 months total and current-patch only. Older H2H is
mostly noise (different rosters, different metas).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta


def _h2h(
    conn: sqlite3.Connection,
    team_a_id: int,
    team_b_id: int,
    as_of_date: str,
    earliest_date: str | None = None,
    patch_id: int | None = None,
) -> tuple[int, int]:
    """Return (team_a_wins, total_games) for h2h before as_of_date."""
    sql = [
        "SELECT mg.winner_team_id",
        "FROM match_games mg",
        "JOIN matches m ON m.match_id = mg.match_id",
        "WHERE m.date < ?",
        "  AND ((mg.blue_team_id = ? AND mg.red_team_id = ?)",
        "    OR (mg.blue_team_id = ? AND mg.red_team_id = ?))",
        "  AND mg.winner_team_id IS NOT NULL",
    ]
    args: list = [as_of_date, team_a_id, team_b_id, team_b_id, team_a_id]
    if earliest_date:
        sql.append("  AND m.date >= ?")
        args.append(earliest_date)
    if patch_id is not None:
        sql.append("  AND mg.patch_id = ?")
        args.append(patch_id)
    rows = conn.execute("\n".join(sql), args).fetchall()
    if not rows:
        return 0, 0
    wins = sum(1 for r in rows if r["winner_team_id"] == team_a_id)
    return wins, len(rows)


def matchup_features(
    conn: sqlite3.Connection,
    team_a_id: int,
    team_b_id: int,
    as_of_date: str,
    patch_id: int | None,
) -> dict[str, float]:
    six_months_ago = (
        datetime.strptime(as_of_date, "%Y-%m-%d") - timedelta(days=180)
    ).strftime("%Y-%m-%d")

    wins_recent, n_recent = _h2h(
        conn, team_a_id, team_b_id, as_of_date, earliest_date=six_months_ago
    )
    wins_patch, n_patch = _h2h(
        conn, team_a_id, team_b_id, as_of_date, patch_id=patch_id
    )

    return {
        # Win rate for team_a vs team_b in last 6 months (0.5 = no data)
        "h2h_a_winrate_6mo": (wins_recent / n_recent) if n_recent else 0.5,
        "h2h_n_games_6mo": float(n_recent),
        "h2h_a_winrate_patch": (wins_patch / n_patch) if n_patch else 0.5,
        "h2h_n_games_patch": float(n_patch),
    }
