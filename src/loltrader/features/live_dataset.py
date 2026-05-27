"""Build training dataset from live_frames + games_live for the Brier backtest.

Output: (X, y, groups) ready for sklearn GroupKFold + xgboost training.

X: pandas DataFrame, one row per frame, columns from FrameFeatures.to_dict()
y: 1 if blue won the game, 0 if red won
groups: game_id (so GroupKFold doesn't leak frames from same game across folds)
"""
from __future__ import annotations

import logging
import sqlite3

import pandas as pd

from loltrader.features.live_state import extract_frame_features

log = logging.getLogger(__name__)


def build_backtest_dataset(
    conn: sqlite3.Connection,
    league_slug: str = "lck",
    source: str = "historical_backtest",
    min_game_time_sec: int = 60,
    min_game_duration_min: float = 18.0,
) -> tuple[pd.DataFrame, pd.Series, pd.Series]:
    """Pull all historical frames for a league and assemble training data.

    Args:
        conn: SQLite connection
        league_slug: filter games by league
        source: filter to 'historical_backtest' (avoid mixing live data)
        min_game_time_sec: drop frames in the first N seconds of the game
            (no signal in those frames — everyone is at level 1 with 0 gold)
        min_game_duration_min: filter out games shorter than this. Pro LoL
            (LCK) has NO surrender — minimum real game length is ~17-20 min
            for fast nexus destruction. Games shorter than 18 min are almost
            always remakes (player DC, technical pause forcing restart).

    Returns:
        X: DataFrame of features
        y: Series of binary labels (1=blue won)
        groups: Series of game_ids for GroupKFold splitting
    """
    # Game-level filter: only games with sufficient frame coverage
    min_duration_sec = int(min_game_duration_min * 60)
    games = conn.execute(
        """
        SELECT * FROM (
            SELECT g.game_id, g.winner_side, g.game_start_ts_unix,
                   g.esports_match_id, g.game_number,
                   (SELECT MAX(frame_ts_unix) - MIN(frame_ts_unix)
                    FROM live_frames WHERE game_id = g.game_id) AS span_sec
            FROM games_live g
            WHERE g.league = ? AND g.source = ? AND g.winner_side IS NOT NULL
                  AND g.game_start_ts_unix IS NOT NULL
        ) WHERE span_sec >= ?
        """,
        (league_slug, source, min_duration_sec),
    ).fetchall()

    if not games:
        raise RuntimeError(
            f"No games found in DB with league={league_slug!r} source={source!r}. "
            "Run `python -m loltrader.tools.backtest_extract --max-matches N` first."
        )

    rows = []
    skipped_frames = 0
    for g in games:
        game_id = g["game_id"]
        winner = g["winner_side"]
        start_ts = g["game_start_ts_unix"]
        y_label = 1 if winner == "blue" else 0

        frames = conn.execute(
            """
            SELECT frame_id, game_id, frame_ts_unix, raw_json, game_state
            FROM live_frames
            WHERE game_id = ? AND game_state = 'in_game'
              AND frame_ts_unix >= ?
            ORDER BY frame_ts_unix
            """,
            (game_id, start_ts + min_game_time_sec),
        ).fetchall()

        for f in frames:
            try:
                feats = extract_frame_features(dict(f), start_ts)
            except (ValueError, KeyError) as e:
                skipped_frames += 1
                continue
            row = feats.to_dict()
            row["_game_id"] = game_id
            row["_match_id"] = g["esports_match_id"]
            row["_label"] = y_label
            rows.append(row)

    if not rows:
        raise RuntimeError("Built dataset is empty after filtering")

    df = pd.DataFrame(rows)
    if skipped_frames:
        log.warning("skipped %d frames due to parse errors", skipped_frames)

    feature_cols = [c for c in df.columns if not c.startswith("_")]
    log.info(
        "Built dataset: %d frames across %d games, %d features",
        len(df),
        df["_game_id"].nunique(),
        len(feature_cols),
    )
    log.info(
        "Label balance: blue_wins=%d (%.1f%%), red_wins=%d (%.1f%%)",
        (df["_label"] == 1).sum(), (df["_label"] == 1).mean() * 100,
        (df["_label"] == 0).sum(), (df["_label"] == 0).mean() * 100,
    )

    X = df[feature_cols]
    y = df["_label"]
    groups = df["_game_id"]
    return X, y, groups
