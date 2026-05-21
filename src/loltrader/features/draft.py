"""Draft features.

Computed at game-1 granularity for each match in v1.5 (using Game 1's
draft as representative of the series). Each match's draft is fully
visible by ~5 minutes before game start.

Feature categories:
  1. Composition tag counts per team (6 tags x 2 teams = 12 features)
  2. Composition tag diffs (team_a - team_b) per tag (6 features)
  3. Champion-pick winrate aggregate (avg historical winrate of the 5
     picks per team, on the current patch) (2 features + diff)
"""
from __future__ import annotations

import sqlite3

TAG_COLUMNS = (
    "has_fighter", "has_mage", "has_assassin",
    "has_marksman", "has_tank", "has_support",
)


def _team_picks_for_match(
    conn: sqlite3.Connection,
    match_id: int,
    team_id: int,
) -> list[str]:
    """Get the 5 champion picks for a team in the first game of a match.
    Returns empty list if no draft data."""
    rows = conn.execute(
        """
        SELECT d.champion
        FROM match_drafts d
        JOIN match_games g ON g.game_id = d.game_id
        WHERE g.match_id = ?
          AND d.team_id = ?
          AND d.is_ban = 0
          AND g.game_number = 1
        ORDER BY d.pick_order
        """,
        (match_id, team_id),
    ).fetchall()
    return [r["champion"] for r in rows]


def _composition_tags(
    conn: sqlite3.Connection, picks: list[str]
) -> dict[str, int]:
    """Count tag instances across the 5 picks. A champ with 2 tags
    counts once for each."""
    counts = {col: 0 for col in TAG_COLUMNS}
    if not picks:
        return counts
    placeholders = ",".join("?" * len(picks))
    rows = conn.execute(
        f"SELECT {','.join(TAG_COLUMNS)} FROM champions WHERE champion_name IN ({placeholders})",
        picks,
    ).fetchall()
    for r in rows:
        for col in TAG_COLUMNS:
            counts[col] += r[col]
    return counts


def _avg_pick_winrate_on_patch(
    conn: sqlite3.Connection,
    picks: list[str],
    patch_id: int | None,
    as_of_date: str,
) -> float:
    """Average historical winrate of these champions on the current patch
    (strictly before as_of_date). Returns 0.5 if patch unknown or no data."""
    if not picks or patch_id is None:
        return 0.5
    placeholders = ",".join("?" * len(picks))
    sql = f"""
        SELECT
            d.champion,
            SUM(CASE WHEN g.winner_team_id = d.team_id THEN 1 ELSE 0 END) AS wins,
            COUNT(*) AS games
        FROM match_drafts d
        JOIN match_games g ON g.game_id = d.game_id
        JOIN matches m ON m.match_id = g.match_id
        WHERE d.is_ban = 0
          AND d.champion IN ({placeholders})
          AND m.patch_id = ?
          AND m.date < ?
        GROUP BY d.champion
    """
    args = [*picks, patch_id, as_of_date]
    rows = conn.execute(sql, args).fetchall()
    if not rows:
        return 0.5
    # Total wins / total games across all queried picks
    total_wins = sum(r["wins"] for r in rows)
    total_games = sum(r["games"] for r in rows)
    if total_games == 0:
        return 0.5
    return total_wins / total_games


def draft_features(
    conn: sqlite3.Connection,
    match_id: int,
    team_a_id: int,
    team_b_id: int,
    patch_id: int | None,
    as_of_date: str,
) -> dict[str, float]:
    """Compute the draft-derived features for one match.

    NOTE on as_of: draft features for this match's PICKS are known at
    draft lock time (just before game start). Historical winrates of
    those champions on the current patch use date < as_of_date (no leak).
    """
    a_picks = _team_picks_for_match(conn, match_id, team_a_id)
    b_picks = _team_picks_for_match(conn, match_id, team_b_id)

    a_tags = _composition_tags(conn, a_picks)
    b_tags = _composition_tags(conn, b_picks)

    a_wr = _avg_pick_winrate_on_patch(conn, a_picks, patch_id, as_of_date)
    b_wr = _avg_pick_winrate_on_patch(conn, b_picks, patch_id, as_of_date)

    feats: dict[str, float] = {}
    # Per-team tag counts
    for col in TAG_COLUMNS:
        feats[f"team_a_{col}"] = float(a_tags[col])
        feats[f"team_b_{col}"] = float(b_tags[col])
        feats[f"tag_diff_{col}"] = float(a_tags[col] - b_tags[col])

    feats["team_a_avg_pick_winrate_patch"] = a_wr
    feats["team_b_avg_pick_winrate_patch"] = b_wr
    feats["pick_winrate_diff"] = a_wr - b_wr

    # Number of picks we actually found (debug / data-quality)
    feats["team_a_n_picks"] = float(len(a_picks))
    feats["team_b_n_picks"] = float(len(b_picks))

    return feats
