"""Walk-forward cross-validation folds.

For time-series-aware validation: train on data up to date T, test on
[T, T+window), slide T forward by step, repeat. Never test on data older
than the most recent training point.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import pandas as pd


@dataclass(frozen=True)
class Fold:
    fold_id: int
    train_end: str       # exclusive cutoff: train uses date < train_end
    test_start: str      # inclusive start: test uses train_end <= date < test_end
    test_end: str        # exclusive end


def walk_forward_folds(
    df: pd.DataFrame,
    initial_train_days: int = 365,
    test_window_days: int = 28,
    step_days: int = 28,
    max_folds: int | None = None,
) -> list[Fold]:
    """Generate walk-forward folds from a date-sorted DataFrame.

    The first fold uses all data older than initial_train_days from the
    earliest test_start as its training set.

    Args:
        df: must have a 'date' column (ISO strings).
        initial_train_days: minimum training history before the first test
            window starts.
        test_window_days: length of each test window.
        step_days: how far to slide the test window each fold.
        max_folds: cap on number of folds returned.
    """
    if df.empty:
        return []
    dates = pd.to_datetime(df["date"])
    earliest = dates.min()
    latest = dates.max()

    cur_test_start = earliest + timedelta(days=initial_train_days)
    folds: list[Fold] = []
    fold_id = 0
    while cur_test_start < latest:
        cur_test_end = cur_test_start + timedelta(days=test_window_days)
        if cur_test_end > latest + timedelta(days=1):
            cur_test_end = latest + timedelta(days=1)
        folds.append(Fold(
            fold_id=fold_id,
            train_end=cur_test_start.strftime("%Y-%m-%d"),
            test_start=cur_test_start.strftime("%Y-%m-%d"),
            test_end=cur_test_end.strftime("%Y-%m-%d"),
        ))
        fold_id += 1
        cur_test_start = cur_test_start + timedelta(days=step_days)
        if max_folds is not None and fold_id >= max_folds:
            break
    return folds


def slice_for_fold(df: pd.DataFrame, fold: Fold) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (train_df, test_df) for the given fold."""
    train = df[df["date"] < fold.train_end].copy()
    test = df[(df["date"] >= fold.test_start) & (df["date"] < fold.test_end)].copy()
    return train, test
