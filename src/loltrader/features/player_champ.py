"""Player-on-champion winrate features.

For each team's draft, aggregate the historical winrate of each player
on each specific champion they played. Faker's Azir is not Faker's Yuumi.
"""
from __future__ import annotations

import sqlite3


def _team_player_champ_pairs(
    conn: sqlite3.Connection, match_id: int, team_id: int
) -> list[tuple[int, str]]:
    """Get the (player_id, champion) pairs for this team in the
    first game of the match. Returns 5 pairs in the normal case."""
    rows = conn.execute(
        """
        SELECT mps.player_id, mps.champion
        FROM match_player_stats mps
        JOIN match_games g ON g.game_id = mps.game_id
        WHERE g.match_id = ?
          AND mps.team_id = ?
          AND g.game_number = 1
        """,
        (match_id, team_id),
    ).fetchall()
    return [(r["player_id"], r["champion"]) for r in rows if r["champion"]]


def _player_champ_winrate(
    conn: sqlite3.Connection,
    player_id: int,
    champion: str,
    as_of_date: str,
    patch_id: int | None = None,
) -> tuple[int, int]:
    """Return (wins, games) for this player on this champion strictly
    before as_of_date. If patch_id given, restrict to that patch."""
    sql = [
        "SELECT",
        "    SUM(CASE WHEN g.winner_team_id = mps.team_id THEN 1 ELSE 0 END) AS wins,",
        "    COUNT(*) AS games",
        "FROM match_player_stats mps",
        "JOIN match_games g ON g.game_id = mps.game_id",
        "JOIN matches m ON m.match_id = g.match_id",
        "WHERE mps.player_id = ? AND mps.champion = ? AND m.date < ?",
    ]
    args: list = [player_id, champion, as_of_date]
    if patch_id is not None:
        sql.append("AND m.patch_id = ?")
        args.append(patch_id)
    row = conn.execute("\n".join(sql), args).fetchone()
    if not row or not row["games"]:
        return 0, 0
    return int(row["wins"]), int(row["games"])


def _team_aggregate_player_champ_winrate(
    conn: sqlite3.Connection,
    pairs: list[tuple[int, str]],
    as_of_date: str,
    patch_id: int | None = None,
) -> tuple[float, int]:
    """Aggregate winrate across all (player, champion) pairs for the team.
    Falls back gracefully — if no data exists, returns 0.5 and 0 games."""
    total_wins = 0
    total_games = 0
    for player_id, champion in pairs:
        w, g = _player_champ_winrate(conn, player_id, champion, as_of_date, patch_id)
        total_wins += w
        total_games += g
    if total_games == 0:
        return 0.5, 0
    return total_wins / total_games, total_games


def player_champ_features(
    conn: sqlite3.Connection,
    match_id: int,
    team_a_id: int,
    team_b_id: int,
    patch_id: int | None,
    as_of_date: str,
) -> dict[str, float]:
    """Compute player-on-champion winrate aggregates per team.

    Returns:
      - team_a/b_pcwr_alltime: historical winrate aggregated across all
        (player, champ) pairs they're playing this game
      - team_a/b_pcwr_patch:   same but restricted to current patch
      - pcwr_diff_*:           team A minus team B
      - team_a/b_pcwr_games:   sample size for the alltime version
    """
    a_pairs = _team_player_champ_pairs(conn, match_id, team_a_id)
    b_pairs = _team_player_champ_pairs(conn, match_id, team_b_id)

    a_wr_all, a_n_all = _team_aggregate_player_champ_winrate(conn, a_pairs, as_of_date)
    b_wr_all, b_n_all = _team_aggregate_player_champ_winrate(conn, b_pairs, as_of_date)

    a_wr_patch, _ = _team_aggregate_player_champ_winrate(conn, a_pairs, as_of_date, patch_id)
    b_wr_patch, _ = _team_aggregate_player_champ_winrate(conn, b_pairs, as_of_date, patch_id)

    return {
        "team_a_pcwr_alltime": a_wr_all,
        "team_b_pcwr_alltime": b_wr_all,
        "pcwr_diff_alltime":   a_wr_all - b_wr_all,
        "team_a_pcwr_patch":   a_wr_patch,
        "team_b_pcwr_patch":   b_wr_patch,
        "pcwr_diff_patch":     a_wr_patch - b_wr_patch,
        "team_a_pcwr_games":   float(a_n_all),
        "team_b_pcwr_games":   float(b_n_all),
    }
