"""Feature engineering orchestrator.

The single entry point ``compute_features(...)`` takes a match identifier
and an ``as_of`` timestamp. Every feature module reads only data with
timestamp strictly less than ``as_of``. This is the no-leak guarantee.

Returns a flat dict[str, float] with a fixed key set. Pass the same
``as_of`` for training (use match.date) and at inference time (use the
trade decision moment).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime

from loltrader.features.draft import draft_features
from loltrader.features.lane_matchup import lane_matchup_features
from loltrader.features.matchup import matchup_features
from loltrader.features.meta import meta_features
from loltrader.features.player_champ import player_champ_features
from loltrader.features.recent_form import recent_form_features
from loltrader.features.schedule import schedule_features
from loltrader.features.team_strength import team_strength_features


def compute_features(
    conn: sqlite3.Connection,
    match_id: int,
    as_of_date: str | None = None,
) -> dict[str, float]:
    """Compute the v1 feature vector for one match.

    Args:
        conn: SQLite connection.
        match_id: ``matches.match_id`` to compute features for.
        as_of_date: ISO date string (YYYY-MM-DD). If None, uses the match's
            own date (training mode). Pass an explicit value at inference
            time to control how "live" the features are.

    Returns: ordered dict with ~40+ float features.
    """
    match_row = conn.execute(
        """
        SELECT match_id, date, league, split, playoffs, patch_id,
               team_a_id, team_b_id, bo_format
        FROM matches WHERE match_id = ?
        """,
        (match_id,),
    ).fetchone()
    if match_row is None:
        raise ValueError(f"No match with match_id={match_id}")

    if as_of_date is None:
        as_of_date = match_row["date"]

    # Enforce strict ISO date format up front so all downstream date
    # comparisons in SQL are well-defined.
    try:
        datetime.strptime(as_of_date, "%Y-%m-%d")
    except ValueError as e:
        raise ValueError(f"as_of_date must be YYYY-MM-DD, got {as_of_date!r}") from e

    team_a_id = match_row["team_a_id"]
    team_b_id = match_row["team_b_id"]
    patch_id = match_row["patch_id"]

    features: dict[str, float] = {}
    features.update(team_strength_features(conn, team_a_id, team_b_id, as_of_date))
    features.update(recent_form_features(conn, team_a_id, team_b_id, as_of_date, patch_id))
    features.update(matchup_features(conn, team_a_id, team_b_id, as_of_date, patch_id))
    features.update(meta_features(
        conn,
        league=match_row["league"],
        bo_format=match_row["bo_format"],
        playoffs=match_row["playoffs"],
        patch_id=patch_id,
        as_of_date=as_of_date,
    ))
    features.update(schedule_features(conn, team_a_id, team_b_id, as_of_date))
    features.update(draft_features(
        conn,
        match_id=match_id,
        team_a_id=team_a_id,
        team_b_id=team_b_id,
        patch_id=patch_id,
        as_of_date=as_of_date,
    ))
    features.update(player_champ_features(
        conn,
        match_id=match_id,
        team_a_id=team_a_id,
        team_b_id=team_b_id,
        patch_id=patch_id,
        as_of_date=as_of_date,
    ))
    features.update(lane_matchup_features(
        conn,
        match_id=match_id,
        team_a_id=team_a_id,
        team_b_id=team_b_id,
        patch_id=patch_id,
        as_of_date=as_of_date,
    ))

    return features


def feature_names() -> list[str]:
    """Return the canonical feature-key order. For now, derive empirically
    by running on a synthetic input; this becomes our schema."""
    # We compute on a minimal connection later in tests to derive this
    # without DB. Simplest: just return the set of keys from a real call.
    # For now, caller imports compute_features and inspects keys; a strict
    # spec will be added when Phase 5 trains a model.
    return []  # placeholder; not used in Phase 3
