"""Train the win-probability XGBoost ensemble + isotonic calibrator.

Pipeline:
  1. Load parquet dataset built by ``build_winprob_dataset``
  2. Time-based train/val/holdout split (chronological by game order)
  3. Train 10-member ensemble with bootstrap row sampling
  4. Compute calibration via isotonic regression on the validation predictions
  5. Compute holdout metrics: Brier score, AUC, per-minute accuracy
  6. Save bundled (ensemble, calibrator, schema, metadata) via LiveWinProbModel

Spec §Phase 4.2-4.3.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import xgboost as xgb
from sklearn.isotonic import IsotonicRegression
from sklearn.metrics import brier_score_loss, roc_auc_score

from loltrader.winprob.model import LiveWinProbModel
from loltrader.winprob.state import FEATURE_SCHEMA

log = logging.getLogger(__name__)


# Default XGBoost hyperparameters. Conservative for small samples — tune later.
DEFAULT_PARAMS = {
    "max_depth": 6,
    "n_estimators": 200,
    "learning_rate": 0.05,
    "reg_alpha": 0.1,             # L1 regularization
    "reg_lambda": 1.0,            # L2 regularization
    "subsample": 0.8,
    "colsample_bytree": 0.8,
    "objective": "binary:logistic",
    "eval_metric": "logloss",
    "tree_method": "hist",
    "n_jobs": -1,
    "random_state": 0,
}

ENSEMBLE_SIZE = 10
TRAIN_FRAC = 0.70
VAL_FRAC = 0.15


@dataclass
class TrainingMetrics:
    """Holdout evaluation results."""
    brier: float
    auc: float
    accuracy: float
    n_train: int
    n_val: int
    n_holdout: int
    per_minute_accuracy: dict[int, float]


def _time_split(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Time-based split by game_id (chronological).

    Games are sorted by their first appearance in the dataset (which already
    came in chronological order from the assembly step), then split into
    train/val/holdout. All frames from the same game stay in the same split
    — prevents leakage from frames of the same game appearing in both train
    and validation.
    """
    games_in_order = list(df["game_id"].unique())  # iteration preserves order in pandas
    n = len(games_in_order)
    n_train = int(n * TRAIN_FRAC)
    n_val = int(n * VAL_FRAC)
    train_games = set(games_in_order[:n_train])
    val_games = set(games_in_order[n_train:n_train + n_val])
    holdout_games = set(games_in_order[n_train + n_val:])
    return (
        df[df["game_id"].isin(train_games)].copy(),
        df[df["game_id"].isin(val_games)].copy(),
        df[df["game_id"].isin(holdout_games)].copy(),
    )


def _features_matrix(df: pd.DataFrame) -> np.ndarray:
    """Extract feature columns in schema order, fill missing with 0."""
    X = np.zeros((len(df), len(FEATURE_SCHEMA)), dtype=np.float32)
    for i, col in enumerate(FEATURE_SCHEMA):
        if col in df.columns:
            X[:, i] = df[col].fillna(0.0).to_numpy(dtype=np.float32)
    return X


def train_ensemble(
    train_df: pd.DataFrame,
    val_df: pd.DataFrame,
    ensemble_size: int = ENSEMBLE_SIZE,
    params: dict | None = None,
) -> list[xgb.XGBClassifier]:
    """Train ``ensemble_size`` XGBoost models with bootstrap row sampling."""
    params = {**DEFAULT_PARAMS, **(params or {})}

    X_train = _features_matrix(train_df)
    y_train = train_df["label"].to_numpy(dtype=np.int32)
    w_train = train_df["weight"].to_numpy(dtype=np.float32)

    X_val = _features_matrix(val_df)
    y_val = val_df["label"].to_numpy(dtype=np.int32)

    ensemble: list[xgb.XGBClassifier] = []
    rng = np.random.RandomState(0)

    for i in range(ensemble_size):
        # Bootstrap sample for this member
        idx = rng.randint(0, len(X_train), size=len(X_train))
        Xi = X_train[idx]
        yi = y_train[idx]
        wi = w_train[idx]

        m = xgb.XGBClassifier(**{**params, "random_state": i})
        m.fit(Xi, yi, sample_weight=wi,
              eval_set=[(X_val, y_val)],
              verbose=False)
        ensemble.append(m)
        log.info("ensemble[%d/%d] trained (val logloss=%.4f)",
                 i + 1, ensemble_size,
                 m.evals_result_["validation_0"]["logloss"][-1])

    return ensemble


def calibrate_ensemble(
    ensemble: list[xgb.XGBClassifier],
    val_df: pd.DataFrame,
) -> IsotonicRegression:
    """Fit isotonic regression on ensemble predictions over the validation set."""
    X_val = _features_matrix(val_df)
    y_val = val_df["label"].to_numpy(dtype=np.int32)

    # Ensemble mean prediction
    raw_probs = np.mean(
        [m.predict_proba(X_val)[:, 1] for m in ensemble], axis=0
    )

    calibrator = IsotonicRegression(out_of_bounds="clip")
    calibrator.fit(raw_probs, y_val)
    log.info("isotonic calibrator fit on %d validation predictions", len(raw_probs))
    return calibrator


def evaluate_on_holdout(
    ensemble: list[xgb.XGBClassifier],
    calibrator: IsotonicRegression,
    holdout_df: pd.DataFrame,
) -> TrainingMetrics:
    """Compute Brier, AUC, accuracy, and per-minute accuracy on holdout."""
    X_h = _features_matrix(holdout_df)
    y_h = holdout_df["label"].to_numpy(dtype=np.int32)

    raw_probs = np.mean(
        [m.predict_proba(X_h)[:, 1] for m in ensemble], axis=0
    )
    cal_probs = calibrator.transform(raw_probs)
    preds = (cal_probs >= 0.5).astype(np.int32)

    brier = float(brier_score_loss(y_h, cal_probs))
    # AUC requires both classes present
    try:
        auc = float(roc_auc_score(y_h, cal_probs))
    except ValueError:
        auc = float("nan")
    accuracy = float((preds == y_h).mean())

    # Per-minute accuracy
    per_minute: dict[int, float] = {}
    if "minute" in holdout_df.columns:
        for m_val in sorted(holdout_df["minute"].unique()):
            mask = (holdout_df["minute"] == m_val).to_numpy()
            if mask.sum() < 5:
                continue
            per_minute[int(m_val)] = float((preds[mask] == y_h[mask]).mean())

    return TrainingMetrics(
        brier=brier,
        auc=auc,
        accuracy=accuracy,
        n_train=0,        # filled in by caller
        n_val=0,
        n_holdout=len(holdout_df),
        per_minute_accuracy=per_minute,
    )


def train_full(
    dataset_path: str | Path,
    output_path: str | Path,
    ensemble_size: int = ENSEMBLE_SIZE,
    params: dict | None = None,
) -> tuple[LiveWinProbModel, TrainingMetrics]:
    """Run the full training pipeline end-to-end."""
    df = pd.read_parquet(dataset_path)
    log.info("loaded dataset: %d rows, %d unique games",
             len(df), df["game_id"].nunique())

    train_df, val_df, holdout_df = _time_split(df)
    log.info("split: train=%d val=%d holdout=%d", len(train_df), len(val_df), len(holdout_df))

    if len(train_df) == 0 or len(val_df) == 0:
        raise RuntimeError("not enough games for time-based split — need at least 4")

    t0 = time.time()
    ensemble = train_ensemble(train_df, val_df, ensemble_size, params)
    log.info("ensemble trained in %.0fs", time.time() - t0)

    calibrator = calibrate_ensemble(ensemble, val_df)

    metrics = evaluate_on_holdout(ensemble, calibrator, holdout_df) \
        if len(holdout_df) > 0 \
        else TrainingMetrics(brier=float("nan"), auc=float("nan"),
                              accuracy=float("nan"),
                              n_train=0, n_val=0, n_holdout=0,
                              per_minute_accuracy={})
    metrics.n_train = len(train_df)
    metrics.n_val = len(val_df)

    model = LiveWinProbModel(
        ensemble=ensemble,
        calibrator=calibrator,
        feature_schema=list(FEATURE_SCHEMA),
        metadata={
            "trained_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "dataset_path": str(dataset_path),
            "n_train": len(train_df),
            "n_val": len(val_df),
            "n_holdout": len(holdout_df),
            "brier": metrics.brier,
            "auc": metrics.auc,
            "accuracy": metrics.accuracy,
            "ensemble_size": ensemble_size,
            "params": {**DEFAULT_PARAMS, **(params or {})},
        },
    )

    model.save(output_path)
    log.info("saved model to %s", output_path)
    log.info("holdout metrics: brier=%.4f auc=%.4f accuracy=%.4f",
             metrics.brier, metrics.auc, metrics.accuracy)
    if metrics.per_minute_accuracy:
        log.info("per-minute accuracy: %s",
                 {k: round(v, 3) for k, v in sorted(metrics.per_minute_accuracy.items())})

    return model, metrics
