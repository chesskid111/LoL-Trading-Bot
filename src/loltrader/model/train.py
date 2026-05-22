"""Train the v1 model.

Pipeline:
  1. Walk-forward CV: for each fold (train_end T, test [T, T+W)):
      a. Train an XGBoost binary classifier on data with date < T.
      b. Get raw predictions on the test fold.
      c. Aggregate predictions across folds for calibration training.
  2. Fit isotonic calibrator on out-of-fold predictions.
  3. Train a final model on ALL data (for live inference).
  4. Train a bootstrap ensemble (N small models on bootstrap resamples)
     for uncertainty estimates at inference.

Saves a versioned artifact: {model, calibrator, ensemble, feature_spec,
training_metadata}.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import xgboost as xgb

from loltrader.model.calibrate import IsotonicCalibrator
from loltrader.model.dataset import build_training_frame, split_xy
from loltrader.model.folds import slice_for_fold, walk_forward_folds
from loltrader.model.metrics import (
    calibration_metrics,
    reliability_diagram,
)

log = logging.getLogger(__name__)


# Default XGBoost hyperparameters. Sensible starting values; tunable via CV
# in future iterations. v1 keeps it simple.
DEFAULT_XGB_PARAMS: dict = {
    "objective": "binary:logistic",
    "eval_metric": "logloss",
    "tree_method": "hist",
    "max_depth": 4,
    "learning_rate": 0.05,
    "n_estimators": 400,
    "subsample": 0.8,
    "colsample_bytree": 0.8,
    "reg_lambda": 1.0,
    "random_state": 42,
}


@dataclass
class TrainedArtifact:
    """The complete saved model bundle."""
    model: xgb.XGBClassifier
    calibrator: IsotonicCalibrator
    ensemble: list[xgb.XGBClassifier]
    feature_spec: list[str]
    metadata: dict


def _train_one_model(
    X: np.ndarray,
    y: np.ndarray,
    sample_weight: np.ndarray | None = None,
    params: dict | None = None,
) -> xgb.XGBClassifier:
    clf = xgb.XGBClassifier(**(params or DEFAULT_XGB_PARAMS))
    clf.fit(X, y, sample_weight=sample_weight)
    return clf


def _load_tuned_params() -> dict | None:
    """Load tuned hyperparameters from models/best_params.json if it exists.
    Returns None if not found, in which case DEFAULT_XGB_PARAMS is used."""
    from loltrader.config import load_config
    cfg = load_config()
    path = cfg.models_dir / "best_params.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        return data.get("best_params")
    except Exception:
        return None


def _time_decay_weights(
    df: pd.DataFrame, decay_half_life_days: float = 180.0
) -> np.ndarray:
    """Exponential decay: weight = 2^(-age / half_life). Most recent
    matches have weight ~1; older matches taper off."""
    dates = pd.to_datetime(df["date"])
    most_recent = dates.max()
    ages = (most_recent - dates).dt.days.to_numpy(dtype=np.float64)
    return np.power(2.0, -ages / decay_half_life_days)


def train(
    conn: sqlite3.Connection,
    min_date: str | None = None,
    max_date: str | None = None,
    holdout_days: int = 14,
    ensemble_size: int = 20,
    decay_half_life_days: float = 180.0,
    output_dir: Path | None = None,
) -> TrainedArtifact:
    """End-to-end training.

    Args:
        conn: SQLite connection.
        min_date / max_date: optional date range for the training corpus.
        holdout_days: final N days reserved as a one-shot validation
            holdout (not used for walk-forward CV or calibration).
        ensemble_size: number of bootstrap models for uncertainty.
        decay_half_life_days: sample weight half-life in days.
        output_dir: where to save reliability diagrams. Default: models/.
    """
    log.info("Loading training data from corpus")
    df_all = build_training_frame(conn, min_date=min_date, max_date=max_date)
    if df_all.empty:
        raise RuntimeError("No training data — corpus is empty or filters too strict.")

    # Use tuned params if available; else fall back to defaults
    xgb_params = _load_tuned_params() or DEFAULT_XGB_PARAMS
    if xgb_params is not DEFAULT_XGB_PARAMS:
        log.info("Using TUNED hyperparameters from models/best_params.json")
    else:
        log.info("Using DEFAULT hyperparameters (run tune_model first for better results)")

    # Reserve final holdout_days as a one-shot validation set
    df_all = df_all.sort_values("date").reset_index(drop=True)
    latest_date = pd.to_datetime(df_all["date"]).max()
    holdout_cutoff = (latest_date - pd.Timedelta(days=holdout_days)).strftime("%Y-%m-%d")
    df_cv = df_all[df_all["date"] < holdout_cutoff].copy()
    df_holdout = df_all[df_all["date"] >= holdout_cutoff].copy()
    log.info(
        "Train+CV pool: %d rows. Holdout (%d days): %d rows.",
        len(df_cv), holdout_days, len(df_holdout),
    )

    # --- Walk-forward CV ---------------------------------------------------
    folds = walk_forward_folds(df_cv, initial_train_days=365, test_window_days=28, step_days=28)
    log.info("Generated %d walk-forward folds", len(folds))

    feature_cols: list[str] | None = None
    oof_preds: list[np.ndarray] = []
    oof_labels: list[np.ndarray] = []
    fold_reports: list[dict] = []

    for fold in folds:
        train_df, test_df = slice_for_fold(df_cv, fold)
        if train_df.empty or test_df.empty:
            log.warning("Fold %d: empty train or test, skipping", fold.fold_id)
            continue
        X_tr, y_tr, feature_cols = split_xy(train_df, feature_cols)
        X_te, y_te, _ = split_xy(test_df, feature_cols)
        w_tr = _time_decay_weights(train_df, decay_half_life_days)

        clf = _train_one_model(X_tr, y_tr, sample_weight=w_tr, params=xgb_params)
        p_te = clf.predict_proba(X_te)[:, 1]

        fm = calibration_metrics(y_te, p_te)
        fold_reports.append({
            "fold_id": fold.fold_id,
            "train_end": fold.train_end,
            "test_start": fold.test_start,
            "test_end": fold.test_end,
            "n_train": len(train_df),
            "n_test": len(test_df),
            **asdict(fm),
        })
        oof_preds.append(p_te)
        oof_labels.append(y_te)

    if not fold_reports:
        raise RuntimeError("No valid folds — need more history or smaller initial_train_days.")

    oof_p = np.concatenate(oof_preds)
    oof_y = np.concatenate(oof_labels)
    cv_metrics = calibration_metrics(oof_y, oof_p)
    log.info("Walk-forward CV metrics (uncalibrated): %s", cv_metrics)

    # --- Calibration -------------------------------------------------------
    calibrator = IsotonicCalibrator().fit(oof_p, oof_y)
    oof_p_cal = calibrator.transform(oof_p)
    cv_metrics_cal = calibration_metrics(oof_y, oof_p_cal)
    log.info("Walk-forward CV metrics (calibrated):   %s", cv_metrics_cal)

    # --- Final model: trained on ALL CV data (no test holdout shown yet) --
    X_full, y_full, feature_cols = split_xy(df_cv, feature_cols)
    w_full = _time_decay_weights(df_cv, decay_half_life_days)
    final_model = _train_one_model(X_full, y_full, sample_weight=w_full, params=xgb_params)

    # --- Bootstrap ensemble for uncertainty -------------------------------
    log.info("Training %d-model bootstrap ensemble", ensemble_size)
    ensemble: list[xgb.XGBClassifier] = []
    rng = np.random.default_rng(seed=42)
    n = len(X_full)
    for i in range(ensemble_size):
        idx = rng.integers(0, n, size=n)
        # Smaller per-model params for speed (still useful as uncertainty signal)
        small_params = {**DEFAULT_XGB_PARAMS, "n_estimators": 200, "random_state": int(rng.integers(0, 10_000_000))}
        m = _train_one_model(X_full[idx], y_full[idx], sample_weight=w_full[idx], params=small_params)
        ensemble.append(m)

    # --- Final holdout evaluation -----------------------------------------
    holdout_metrics: dict | None = None
    if not df_holdout.empty:
        X_ho, y_ho, _ = split_xy(df_holdout, feature_cols)
        p_ho_raw = final_model.predict_proba(X_ho)[:, 1]
        p_ho_cal = calibrator.transform(p_ho_raw)
        holdout_metrics = {
            "raw": asdict(calibration_metrics(y_ho, p_ho_raw)),
            "calibrated": asdict(calibration_metrics(y_ho, p_ho_cal)),
        }
        log.info("Holdout metrics (raw):        %s", holdout_metrics["raw"])
        log.info("Holdout metrics (calibrated): %s", holdout_metrics["calibrated"])

    # --- Reliability diagrams ---------------------------------------------
    output_dir = output_dir or Path(__file__).resolve().parents[3] / "models"
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    reliability_diagram(oof_y, oof_p, output_dir / f"v1_cv_raw_{ts}.png",
                        title=f"v1 walk-forward CV (raw, ECE={cv_metrics.ece:.3f})")
    reliability_diagram(oof_y, oof_p_cal, output_dir / f"v1_cv_calibrated_{ts}.png",
                        title=f"v1 walk-forward CV (calibrated, ECE={cv_metrics_cal.ece:.3f})")

    # --- Build artifact ---------------------------------------------------
    metadata = {
        "trained_at_utc": ts,
        "n_train_samples": int(len(X_full)),
        "n_features": len(feature_cols),
        "feature_cols": feature_cols,
        "ensemble_size": ensemble_size,
        "decay_half_life_days": decay_half_life_days,
        "holdout_days": holdout_days,
        "xgb_params": DEFAULT_XGB_PARAMS,
        "cv_metrics_raw": asdict(cv_metrics),
        "cv_metrics_calibrated": asdict(cv_metrics_cal),
        "fold_reports": fold_reports,
        "holdout_metrics": holdout_metrics,
    }
    return TrainedArtifact(
        model=final_model,
        calibrator=calibrator,
        ensemble=ensemble,
        feature_spec=feature_cols,
        metadata=metadata,
    )


def save_artifact(art: TrainedArtifact, path: Path) -> Path:
    """Pickle the bundled artifact. Returns the path."""
    import pickle
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        pickle.dump({
            "model": art.model,
            "calibrator": art.calibrator,
            "ensemble": art.ensemble,
            "feature_spec": art.feature_spec,
            "metadata": art.metadata,
        }, f)
    return path
