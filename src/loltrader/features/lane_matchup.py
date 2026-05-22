"""Per-lane champion matchup features.

For each role (top/jng/mid/bot/sup) in the match, look up the historical
winrate of (team A's champion in that role) vs (team B's champion in that
role) across all prior games in our corpus.

Hierarchical fallback (most data first):
  1. Exact champion pair on current patch  (e.g., Aatrox vs Sett on 14.10)
  2. Exact champion pair on any patch
  3. Champion A's overall winrate in role (against any opponent)
  4. 0.5 flat prior

The aggregate "lane advantage" feature sums signed advantages across the
5 roles. This is the kind of signal pro analysts cite ("our jungler
has a 60% winrate vs theirs on this patch").
"""
from __future__ import annotations

import sqlite3

ROLES = ("top", "jng", "mid", "bot", "sup")
MIN_SAMPLES_FOR_PAIR = 5     # need at least 5 prior games for a pair before trusting it


def _team_role_picks(
    conn: sqlite3.Connection, match_id: int, team_id: int
) -> dict[str, str]:
    """Return {role: champion} for this team in game 1 of the match."""
    rows = conn.execute(
        """
        SELECT mps.role, mps.champion
        FROM match_player_stats mps
        JOIN match_games g ON g.game_id = mps.game_id
        WHERE g.match_id = ?
          AND mps.team_id = ?
          AND g.game_number = 1
          AND mps.role IN ('top','jng','mid','bot','sup')
        """,
        (match_id, team_id),
    ).fetchall()
    return {r["role"]: r["champion"] for r in rows if r["champion"]}


def _pair_winrate(
    conn: sqlite3.Connection,
    champ_a: str, champ_b: str, role: str,
    as_of_date: str, patch_id: int | None,
) -> tuple[float, int]:
    """Historical winrate of champ_a (in role) vs champ_b (same role).
    Returns (winrate, n_games) — 0.5 if no data."""
    sql = [
        "SELECT",
        "    SUM(CASE WHEN g.winner_team_id = mps_a.team_id THEN 1 ELSE 0 END) AS wins,",
        "    COUNT(*) AS games",
        "FROM match_player_stats mps_a",
        "JOIN match_player_stats mps_b ON mps_b.game_id = mps_a.game_id",
        "    AND mps_b.team_id != mps_a.team_id",
        "    AND mps_b.role = mps_a.role",
        "JOIN match_games g ON g.game_id = mps_a.game_id",
        "JOIN matches m ON m.match_id = g.match_id",
        "WHERE mps_a.role = ? AND mps_a.champion = ? AND mps_b.champion = ?",
        "  AND m.date < ?",
    ]
    args: list = [role, champ_a, champ_b, as_of_date]
    if patch_id is not None:
        sql.append("AND m.patch_id = ?")
        args.append(patch_id)
    row = conn.execute("\n".join(sql), args).fetchone()
    games = int(row["games"]) if row and row["games"] else 0
    if games == 0:
        return 0.5, 0
    return (int(row["wins"]) / games), games


def _champ_role_overall_winrate(
    conn: sqlite3.Connection, champion: str, role: str, as_of_date: str
) -> tuple[float, int]:
    """Champion's overall winrate in role (any opponent)."""
    row = conn.execute(
        """
        SELECT
            SUM(CASE WHEN g.winner_team_id = mps.team_id THEN 1 ELSE 0 END) AS wins,
            COUNT(*) AS games
        FROM match_player_stats mps
        JOIN match_games g ON g.game_id = mps.game_id
        JOIN matches m ON m.match_id = g.match_id
        WHERE mps.role = ? AND mps.champion = ? AND m.date < ?
        """,
        (role, champion, as_of_date),
    ).fetchone()
    games = int(row["games"]) if row and row["games"] else 0
    if games == 0:
        return 0.5, 0
    return (int(row["wins"]) / games), games


def _lane_matchup_winrate(
    conn: sqlite3.Connection,
    champ_a: str, champ_b: str, role: str,
    as_of_date: str, patch_id: int | None,
) -> tuple[float, str]:
    """Hierarchical fallback for one lane's matchup winrate.
    Returns (winrate, source_label)."""
    # 1. Exact pair on current patch
    wr, n = _pair_winrate(conn, champ_a, champ_b, role, as_of_date, patch_id)
    if n >= MIN_SAMPLES_FOR_PAIR:
        return wr, "pair_patch"
    # 2. Exact pair across all patches
    wr_all, n_all = _pair_winrate(conn, champ_a, champ_b, role, as_of_date, None)
    if n_all >= MIN_SAMPLES_FOR_PAIR:
        return wr_all, "pair_alltime"
    # 3. Champion A solo winrate in role
    wr_solo, n_solo = _champ_role_overall_winrate(conn, champ_a, role, as_of_date)
    if n_solo >= 3:
        return wr_solo, "champ_solo"
    # 4. Flat prior
    return 0.5, "prior"


def lane_matchup_features(
    conn: sqlite3.Connection,
    match_id: int,
    team_a_id: int,
    team_b_id: int,
    patch_id: int | None,
    as_of_date: str,
) -> dict[str, float]:
    """Per-role matchup winrate + aggregate."""
    a_by_role = _team_role_picks(conn, match_id, team_a_id)
    b_by_role = _team_role_picks(conn, match_id, team_b_id)

    feats: dict[str, float] = {}
    total_advantage = 0.0      # sum of (winrate - 0.5) across roles
    roles_with_data = 0

    for role in ROLES:
        a_champ = a_by_role.get(role)
        b_champ = b_by_role.get(role)
        if a_champ and b_champ:
            wr, _src = _lane_matchup_winrate(conn, a_champ, b_champ, role,
                                              as_of_date, patch_id)
            feats[f"lane_{role}_a_winrate"] = wr
            total_advantage += (wr - 0.5)
            roles_with_data += 1
        else:
            feats[f"lane_{role}_a_winrate"] = 0.5

    feats["lane_total_advantage"] = total_advantage
    feats["lane_n_roles_with_data"] = float(roles_with_data)
    return feats
