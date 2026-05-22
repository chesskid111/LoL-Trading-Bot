"""Run hyperparameter search for the v1.6 model.

Builds the training frame once, then runs Optuna TPE over XGBoost
hyperparameters. Saves the best-params dict to models/best_params.json
which train_model can then load.

Usage:
    python -m loltrader.tools.tune_model --n-trials 60
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time

import pandas as pd

from loltrader.config import load_config
from loltrader.db import connect, migrate
from loltrader.features.team_strength import rebuild_team_glicko
from loltrader.model.dataset import build_training_frame, split_xy
from loltrader.model.tune import tune


def _setup_logging() -> logging.Logger:
    cfg = load_config()
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    logfile = cfg.logs_dir / "tune.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(logfile, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    return logging.getLogger("tune_model")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--n-trials", type=int, default=60)
    parser.add_argument("--holdout-days", type=int, default=14)
    parser.add_argument("--decay-half-life", type=float, default=180.0)
    args = parser.parse_args()

    log = _setup_logging()
    cfg = load_config()
    log.info("Starting hyperparameter search (%d trials)", args.n_trials)

    start = time.time()
    conn = connect()
    migrate(conn)

    # Rebuild Glicko snapshots so feature computation has fresh ratings
    rebuild_team_glicko(conn)

    log.info("Building training frame (one-time, ~minutes)")
    df_all = build_training_frame(conn)
    df_all = df_all.sort_values("date").reset_index(drop=True)
    latest_date = pd.to_datetime(df_all["date"]).max()
    holdout_cutoff = (latest_date - pd.Timedelta(days=args.holdout_days)).strftime("%Y-%m-%d")
    df_cv = df_all[df_all["date"] < holdout_cutoff].copy()
    log.info("CV pool: %d rows. Holdout (%d days, untouched by tuning): %d rows.",
             len(df_cv), args.holdout_days, len(df_all) - len(df_cv))

    _, _, feature_cols = split_xy(df_cv)
    log.info("Feature count: %d", len(feature_cols))

    result = tune(df_cv, feature_cols, n_trials=args.n_trials,
                  decay_half_life_days=args.decay_half_life)

    log.info("Best params: %s", result.best_params)
    log.info("Best CV calibrated Brier: %.4f", result.best_calibrated_brier)
    log.info("Best CV calibrated ECE:   %.4f", result.best_calibrated_ece)
    log.info("Best CV log loss:         %.4f", result.best_score)

    # Save
    cfg.models_dir.mkdir(parents=True, exist_ok=True)
    out_path = cfg.models_dir / "best_params.json"
    with open(out_path, "w") as f:
        json.dump({
            "best_params": result.best_params,
            "best_calibrated_brier": result.best_calibrated_brier,
            "best_calibrated_ece":   result.best_calibrated_ece,
            "best_log_loss":         result.best_score,
            "n_trials":              args.n_trials,
            "trial_log":             result.trial_log,
        }, f, indent=2, default=str)
    log.info("Saved best params -> %s", out_path)

    elapsed = time.time() - start
    log.info("Tuning complete in %.1fs", elapsed)
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
