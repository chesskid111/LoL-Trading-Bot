"""Build a training dataset by computing features for every settled
match in the corpus.

Each row = one match (a series). Label = 1 if team_a won the series, 0 if
team_b. The feature vector is constructed via ``compute_features`` with
``as_of_date = match.date`` — i.e., features available *just before* the
match was played.
"""
from __future__ import annotations

import logging
import sqlite3

import numpy as np
import pandas as pd

from loltrader.features import compute_features

log = logging.getLogger(__name__)


def build_training_frame(
    conn: sqlite3.Connection,
    min_date: str | None = None,
    max_date: str | None = None,
    include_partial_data: bool = True,
) -> pd.DataFrame:
    """Build a (n_matches, n_features + label + match_id + date) DataFrame.

    Args:
        conn: SQLite connection.
        min_date / max_date: optional date filters (inclusive). Useful for
            holding out a final period for one-shot validation.
        include_partial_data: include matches whose games have at least
            one ``partial`` datacompleteness row in Oracle (i.e., all LPL).
            v1 includes these because they have valid labels + drafts.

    Returns: DataFrame with columns:
        - match_id, date, league, team_a_id, team_b_id, label
        - all feature columns from compute_features()
    """
    where = ["m.series_winner_id IS NOT NULL"]
    args: list = []
    if min_date:
        where.append("m.date >= ?")
        args.append(min_date)
    if max_date:
        where.append("m.date <= ?")
        args.append(max_date)
    where_sql = " AND ".join(where)

    matches = conn.execute(
        f"""
        SELECT match_id, date, league, team_a_id, team_b_id, series_winner_id
        FROM matches m
        WHERE {where_sql}
        ORDER BY date ASC, match_id ASC
        """,
        args,
    ).fetchall()

    log.info("Building training frame from %d settled matches", len(matches))

    rows: list[dict] = []
    for m in matches:
        match_id = m["match_id"]
        label = 1 if m["series_winner_id"] == m["team_a_id"] else 0
        feats = compute_features(conn, match_id)
        rows.append({
            "match_id": match_id,
            "date": m["date"],
            "league": m["league"],
            "team_a_id": m["team_a_id"],
            "team_b_id": m["team_b_id"],
            "label": label,
            **feats,
        })

    df = pd.DataFrame(rows)
    log.info("Built frame: %d rows, %d feature columns", len(df), len(df.columns) - 6)
    return df


def split_xy(
    df: pd.DataFrame,
    feature_cols: list[str] | None = None,
) -> tuple[np.ndarray, np.ndarray, list[str]]:
    """Split DataFrame into (X, y, feature_names).
    If feature_cols is provided, X is restricted to that set (enforces
    consistent feature schema across train and inference).
    """
    if feature_cols is None:
        # Everything except metadata + label
        meta_cols = {"match_id", "date", "league", "team_a_id", "team_b_id", "label"}
        feature_cols = [c for c in df.columns if c not in meta_cols]
    X = df[feature_cols].to_numpy(dtype=np.float64)
    y = df["label"].to_numpy(dtype=np.int64)
    return X, y, feature_cols
