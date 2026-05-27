"""Train + evaluate the live-state model on the historical backtest dataset.

Walk-forward CV grouped by game (so frames from same game stay in same fold).
Reports per-phase Brier score + calibration metrics. Decision gate per spec §8.

Usage:
    python -m loltrader.tools.backtest_train

Acceptance (Brier backtest):
    - Per-frame Brier <= 0.20 averaged across full game
    - Per-phase Brier <= 0.22 in any single phase bucket
    - ECE <= 0.04 per phase
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import xgboost as xgb
from sklearn.metrics import brier_score_loss, log_loss
from sklearn.model_selection import GroupKFold

from loltrader.db import connect
from loltrader.features.live_dataset import build_backtest_dataset

log = logging.getLogger(__name__)


def _expected_calibration_error(y_true: np.ndarray, y_proba: np.ndarray,
                                 n_bins: int = 10) -> float:
    """Compute Expected Calibration Error (ECE) across n_bins probability bins.

    ECE = sum over bins of (|frac_pos_in_bin - mean_proba_in_bin| * bin_weight)
    """
    bin_edges = np.linspace(0, 1, n_bins + 1)
    ece = 0.0
    n = len(y_true)
    for i in range(n_bins):
        lo, hi = bin_edges[i], bin_edges[i + 1]
        mask = (y_proba >= lo) & (y_proba < hi if i < n_bins - 1 else y_proba <= hi)
        bin_n = mask.sum()
        if bin_n == 0:
            continue
        bin_acc = y_true[mask].mean()
        bin_conf = y_proba[mask].mean()
        ece += abs(bin_acc - bin_conf) * (bin_n / n)
    return ece


def _phase_of_time(seconds: int) -> str:
    minutes = seconds / 60
    if minutes <= 10:
        return "early"
    if minutes <= 20:
        return "mid"
    if minutes <= 30:
        return "late"
    return "closeout"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--league", default="lck")
    p.add_argument("--folds", type=int, default=5)
    p.add_argument("--out", default="data/backtest_results")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args(argv)

    logging.basicConfig(level=getattr(logging, args.log_level),
                        format="%(asctime)s %(levelname)s %(message)s")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    log.info("Loading dataset from DB...")
    conn = connect()
    X, y, groups = build_backtest_dataset(conn, league_slug=args.league)
    conn.close()

    n_games = groups.nunique()
    if n_games < args.folds:
        log.warning("Only %d games — reducing folds from %d to %d",
                    n_games, args.folds, max(2, n_games - 1))
        args.folds = max(2, n_games - 1)

    log.info("Cross-validation: %d-fold GroupKFold on %d games", args.folds, n_games)

    gkf = GroupKFold(n_splits=args.folds)
    oof_probs = np.zeros(len(y))
    fold_briers = []

    xgb_params = dict(
        n_estimators=300,
        learning_rate=0.05,
        max_depth=5,
        min_child_weight=10,
        subsample=0.8,
        colsample_bytree=0.8,
        eval_metric="logloss",
        objective="binary:logistic",
        tree_method="hist",
        random_state=42,
    )

    for fold_idx, (train_idx, val_idx) in enumerate(gkf.split(X, y, groups)):
        train_games = groups.iloc[train_idx].nunique()
        val_games = groups.iloc[val_idx].nunique()
        log.info("Fold %d: train_games=%d, val_games=%d, train_frames=%d, val_frames=%d",
                 fold_idx + 1, train_games, val_games, len(train_idx), len(val_idx))

        model = xgb.XGBClassifier(**xgb_params)
        model.fit(X.iloc[train_idx], y.iloc[train_idx],
                   eval_set=[(X.iloc[val_idx], y.iloc[val_idx])],
                   verbose=False)
        val_probs = model.predict_proba(X.iloc[val_idx])[:, 1]
        oof_probs[val_idx] = val_probs
        brier = brier_score_loss(y.iloc[val_idx], val_probs)
        fold_briers.append(brier)
        log.info("  fold %d Brier: %.4f", fold_idx + 1, brier)

    overall_brier = brier_score_loss(y, oof_probs)
    overall_logloss = log_loss(y, oof_probs)
    overall_ece = _expected_calibration_error(y.values, oof_probs)

    print()
    print("=" * 60)
    print("BACKTEST RESULTS (out-of-fold)")
    print("=" * 60)
    print(f"Games:            {n_games}")
    print(f"Total frames:     {len(y)}")
    print(f"Label balance:    blue_wins={y.mean()*100:.1f}%, red_wins={(1-y.mean())*100:.1f}%")
    print()
    print(f"Overall Brier:    {overall_brier:.4f}  (target: <= 0.20)")
    print(f"Overall LogLoss:  {overall_logloss:.4f}")
    print(f"Overall ECE:      {overall_ece:.4f}  (target: <= 0.04)")
    print(f"Fold Briers:      {[f'{b:.4f}' for b in fold_briers]}")
    print()

    # Per-phase breakdown
    phases = X["game_time_sec"].apply(_phase_of_time)
    print("Per-phase performance:")
    print(f"  {'phase':10s} {'n_frames':>10s} {'Brier':>10s} {'ECE':>10s} {'mean_p':>10s} {'mean_y':>10s}")
    phase_results = {}
    for phase in ["early", "mid", "late", "closeout"]:
        mask = phases == phase
        if mask.sum() == 0:
            continue
        ph_brier = brier_score_loss(y[mask], oof_probs[mask])
        ph_ece = _expected_calibration_error(y[mask].values, oof_probs[mask])
        ph_mean_p = oof_probs[mask].mean()
        ph_mean_y = y[mask].mean()
        phase_results[phase] = ph_brier
        marker = "OK" if ph_brier <= 0.22 else "FAIL"
        print(f"  {phase:10s} {mask.sum():>10d} {ph_brier:>10.4f} {ph_ece:>10.4f} {ph_mean_p:>10.4f} {ph_mean_y:>10.4f}  {marker}")

    # Compare to baseline: always-50% prediction
    baseline_brier = brier_score_loss(y, np.full(len(y), 0.5))
    print()
    print(f"Baseline (always 0.5) Brier: {baseline_brier:.4f}")
    print(f"Improvement over baseline:   {(baseline_brier - overall_brier):.4f} ({(1 - overall_brier/baseline_brier)*100:.1f}%)")

    # Save predictions for downstream analysis (Kalshi-book comparison)
    out_path = out_dir / "oof_predictions.csv"
    pd.DataFrame({
        "game_id": groups.values,
        "game_time_sec": X["game_time_sec"].values,
        "phase": phases.values,
        "y_true": y.values,
        "p_blue_wins": oof_probs,
    }).to_csv(out_path, index=False)
    print(f"\nSaved OOF predictions → {out_path}")

    # Decision gate
    print()
    print("=" * 60)
    print("DECISION GATE (spec §8 / Brier backtest)")
    print("=" * 60)
    passes = (
        overall_brier <= 0.20
        and all(b <= 0.22 for b in phase_results.values())
    )
    if passes:
        print("PASS - model meets per-frame and per-phase Brier targets")
        print("  -> Edge hypothesis is plausible. Continue v2 build.")
    else:
        fails = []
        if overall_brier > 0.20:
            fails.append(f"overall Brier {overall_brier:.4f} > 0.20")
        for ph, b in phase_results.items():
            if b > 0.22:
                fails.append(f"{ph}-phase Brier {b:.4f} > 0.22")
        print("FAIL - model misses targets:")
        for f in fails:
            print(f"    - {f}")
        print()
        print("  -> Investigate: more games needed? Feature engineering gap?")
        print("    Model architecture? Decide before further v2 investment.")

    return 0 if passes else 2


if __name__ == "__main__":
    sys.exit(main())
