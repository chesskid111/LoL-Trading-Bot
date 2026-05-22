"""Hyperparameter search for the v1.6 model using Optuna.

We separate concerns:
  1. Build the training frame ONCE (slow — does feature computation
     for every match in the corpus).
  2. Run many XGBoost training iterations on that cached matrix with
     different hyperparameter combinations (fast — seconds per trial).
  3. Score each trial by walk-forward CV log loss.
  4. Return the best params dict.

The same fold-generator and walk-forward strategy as production
training; we just sweep the model params.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import optuna
import pandas as pd
import xgboost as xgb

from loltrader.model.calibrate import IsotonicCalibrator
from loltrader.model.dataset import build_training_frame, split_xy
from loltrader.model.folds import slice_for_fold, walk_forward_folds
from loltrader.model.metrics import calibration_metrics
from loltrader.model.train import _time_decay_weights

log = logging.getLogger(__name__)


@dataclass
class TuneResult:
    best_params: dict
    best_score: float                 # mean walk-forward CV log loss
    best_calibrated_brier: float
    best_calibrated_ece: float
    trial_log: list[dict]


def _cv_log_loss(
    df_cv: pd.DataFrame,
    feature_cols: list[str],
    params: dict,
    decay_half_life_days: float,
) -> tuple[float, float, float]:
    """Walk-forward CV. Returns (mean_log_loss, calibrated_brier,
    calibrated_ece) on out-of-fold predictions."""
    folds = walk_forward_folds(
        df_cv, initial_train_days=365, test_window_days=28, step_days=28
    )
    oof_preds = []
    oof_labels = []
    for fold in folds:
        train_df, test_df = slice_for_fold(df_cv, fold)
        if train_df.empty or test_df.empty:
            continue
        X_tr, y_tr, _ = split_xy(train_df, feature_cols)
        X_te, y_te, _ = split_xy(test_df, feature_cols)
        w_tr = _time_decay_weights(train_df, decay_half_life_days)
        clf = xgb.XGBClassifier(**params)
        clf.fit(X_tr, y_tr, sample_weight=w_tr)
        p_te = clf.predict_proba(X_te)[:, 1]
        oof_preds.append(p_te)
        oof_labels.append(y_te)

    if not oof_preds:
        return float("inf"), float("inf"), float("inf")

    oof_p = np.concatenate(oof_preds)
    oof_y = np.concatenate(oof_labels)
    raw = calibration_metrics(oof_y, oof_p)
    cal = IsotonicCalibrator().fit(oof_p, oof_y)
    cal_p = cal.transform(oof_p)
    cal_m = calibration_metrics(oof_y, cal_p)
    return raw.log_loss, cal_m.brier, cal_m.ece


def tune(
    df_cv: pd.DataFrame,
    feature_cols: list[str],
    n_trials: int = 60,
    decay_half_life_days: float = 180.0,
    seed: int = 42,
) -> TuneResult:
    """Run Optuna TPE search over XGBoost hyperparams.
    Returns the best parameter dict + score history."""

    trial_log: list[dict] = []

    def objective(trial: optuna.Trial) -> float:
        params = {
            "objective": "binary:logistic",
            "eval_metric": "logloss",
            "tree_method": "hist",
            "random_state": seed,
            "max_depth": trial.suggest_int("max_depth", 3, 7),
            "learning_rate": trial.suggest_float("learning_rate", 0.01, 0.15, log=True),
            "n_estimators": trial.suggest_int("n_estimators", 150, 700, step=50),
            "subsample": trial.suggest_float("subsample", 0.5, 1.0),
            "colsample_bytree": trial.suggest_float("colsample_bytree", 0.5, 1.0),
            "reg_lambda": trial.suggest_float("reg_lambda", 0.1, 20.0, log=True),
            "reg_alpha": trial.suggest_float("reg_alpha", 0.0, 5.0),
            "min_child_weight": trial.suggest_int("min_child_weight", 1, 20),
        }
        try:
            ll, brier, ece = _cv_log_loss(df_cv, feature_cols, params, decay_half_life_days)
        except Exception as e:
            log.warning("Trial failed: %s", e)
            return float("inf")
        trial_log.append({
            "trial": trial.number, "log_loss": ll, "brier": brier, "ece": ece,
            **params,
        })
        log.info(
            "Trial %d: log_loss=%.4f calibrated_brier=%.4f ece=%.4f  params=%s",
            trial.number, ll, brier, ece,
            {k: params[k] for k in ("max_depth", "learning_rate", "n_estimators")},
        )
        return ll

    sampler = optuna.samplers.TPESampler(seed=seed)
    study = optuna.create_study(direction="minimize", sampler=sampler)
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    study.optimize(objective, n_trials=n_trials, show_progress_bar=False)

    best = study.best_params
    best_full = {
        "objective": "binary:logistic",
        "eval_metric": "logloss",
        "tree_method": "hist",
        "random_state": seed,
        **best,
    }
    best_score, best_brier, best_ece = _cv_log_loss(
        df_cv, feature_cols, best_full, decay_half_life_days
    )
    return TuneResult(
        best_params=best_full,
        best_score=best_score,
        best_calibrated_brier=best_brier,
        best_calibrated_ece=best_ece,
        trial_log=trial_log,
    )
