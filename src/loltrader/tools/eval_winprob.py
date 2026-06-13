"""Evaluate a trained win-prob model: calibration + per-minute reliability.

Goes beyond the single Brier/AUC number to answer the question that actually
matters for trading: *is the model's stated confidence trustworthy at the
states we trade on* — especially near-50% predictions in the late game, where
a single fight settles the market and overconfidence is most costly.

Outputs:
  1. Overall Brier / AUC / logloss on the holdout
  2. Calibration table: predicted-prob bucket -> actual win frequency
     (a well-calibrated model's 60% bucket wins ~60% of the time)
  3. Per-minute-bucket Brier (where in the game is the model reliable?)
  4. The critical cell: late-game (>=28 min) near-50% reliability — if the
     model says ~50% late and the actual freq is also ~50%, holding those
     positions is genuinely a coinflip (exit them); if it's miscalibrated,
     that's a bug to fix before trading late states.

Usage:
    python -m loltrader.tools.eval_winprob --model models/winprob_v2.pkl \\
        --dataset data/winprob_v2_clean_moderate.parquet
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import numpy as np


def _time_split_holdout(df, val_frac=0.15, holdout_frac=0.15):
    """Reproduce train.py's chronological split to get the SAME holdout games.

    Must match loltrader.winprob.train._time_split ordering so we evaluate on
    games the model never saw.
    """
    games_in_order = list(dict.fromkeys(df["game_id"].tolist()))
    n = len(games_in_order)
    n_train = int(n * (1 - val_frac - holdout_frac))
    n_val = int(n * val_frac)
    holdout_games = set(games_in_order[n_train + n_val:])
    return df[df["game_id"].isin(holdout_games)].copy()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--model", required=True)
    p.add_argument("--dataset", required=True)
    p.add_argument("--full", action="store_true",
                   help="Evaluate on the whole dataset instead of just holdout")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args(argv)

    logging.basicConfig(level=getattr(logging, args.log_level), format="%(message)s")
    log = logging.getLogger(__name__)

    import pandas as pd
    from sklearn.metrics import brier_score_loss, roc_auc_score, log_loss
    from loltrader.winprob.model import LiveWinProbModel

    model = LiveWinProbModel.load(args.model)
    df = pd.read_parquet(args.dataset)

    eval_df = df if args.full else _time_split_holdout(df)
    log.info("Evaluating %s on %d rows (%d games)%s",
             Path(args.model).name, len(eval_df), eval_df["game_id"].nunique(),
             " [FULL]" if args.full else " [holdout]")

    # Feature matrix in schema order
    feat_cols = [c for c in model.feature_schema if c in eval_df.columns]
    missing = [c for c in model.feature_schema if c not in eval_df.columns]
    if missing:
        log.warning("dataset missing %d schema features (zero-filled): %s",
                    len(missing), missing[:5])
    X = np.zeros((len(eval_df), len(model.feature_schema)), dtype=np.float32)
    for j, c in enumerate(model.feature_schema):
        if c in eval_df.columns:
            X[:, j] = eval_df[c].fillna(0).to_numpy(dtype=np.float32)
    y = eval_df["label"].to_numpy(dtype=np.int32)

    p_blue = model.predict_batch(X)

    # ---- Overall metrics ----
    brier = brier_score_loss(y, p_blue)
    auc = roc_auc_score(y, p_blue)
    ll = log_loss(y, np.clip(p_blue, 1e-6, 1 - 1e-6))
    log.info("\n=== Overall (holdout) ===")
    log.info("  Brier:   %.4f  (lower better; 0.25=coinflip, <0.15=good)", brier)
    log.info("  AUC:     %.4f  (higher better; 0.5=random, >0.80=good)", auc)
    log.info("  LogLoss: %.4f", ll)

    # ---- Calibration table ----
    log.info("\n=== Calibration (predicted vs actual) ===")
    log.info("  %-14s %8s %10s %8s", "pred bucket", "n", "actual WR", "gap")
    bins = [0, .1, .2, .3, .4, .45, .5, .55, .6, .7, .8, .9, 1.01]
    max_gap = 0.0
    for lo, hi in zip(bins[:-1], bins[1:]):
        mask = (p_blue >= lo) & (p_blue < hi)
        if mask.sum() < 20:
            continue
        actual = y[mask].mean()
        pred_mid = p_blue[mask].mean()
        gap = actual - pred_mid
        max_gap = max(max_gap, abs(gap))
        flag = "  <-- off" if abs(gap) > 0.08 else ""
        log.info("  [%.2f,%.2f)%-4s %8d %9.1f%% %+7.1f%%%s",
                 lo, hi, "", int(mask.sum()), actual * 100, gap * 100, flag)
    log.info("  max calibration gap: %.1f%%", max_gap * 100)

    # ---- Per-minute Brier ----
    if "minute" in eval_df.columns:
        log.info("\n=== Per-minute-bucket Brier ===")
        mins = eval_df["minute"].to_numpy()
        for lo, hi in [(0, 10), (10, 15), (15, 20), (20, 25), (25, 30), (30, 60)]:
            mask = (mins >= lo) & (mins < hi)
            if mask.sum() < 20:
                continue
            b = brier_score_loss(y[mask], p_blue[mask]) if len(set(y[mask])) > 1 else float("nan")
            log.info("  min [%2d,%2d): n=%-6d Brier=%.4f", lo, hi, int(mask.sum()), b)

    # ---- THE critical cell: late-game near-50% reliability ----
    if "minute" in eval_df.columns:
        log.info("\n=== Late-game (>=28 min) near-50%% reliability ===")
        mins = eval_df["minute"].to_numpy()
        late_5050 = (mins >= 28) & (p_blue >= 0.40) & (p_blue <= 0.60)
        if late_5050.sum() >= 20:
            actual = y[late_5050].mean()
            pred = p_blue[late_5050].mean()
            log.info("  n=%d  model says %.1f%%  actual %.1f%%  gap %+.1f%%",
                     int(late_5050.sum()), pred * 100, actual * 100,
                     (actual - pred) * 100)
            if abs(actual - pred) <= 0.05:
                log.info("  -> CALIBRATED: when model says ~50%% late, it really is "
                         "a coinflip. Exit those positions (no edge, max variance).")
            else:
                log.info("  -> MISCALIBRATED by %.1f%% — investigate before trading "
                         "late 50/50 states.", abs(actual - pred) * 100)
        else:
            log.info("  insufficient late near-50%% samples (n=%d)", int(late_5050.sum()))

    return 0


if __name__ == "__main__":
    sys.exit(main())
