"""Tests for the model training + serving stack."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from loltrader.model.calibrate import IsotonicCalibrator
from loltrader.model.folds import slice_for_fold, walk_forward_folds
from loltrader.model.metrics import (
    brier_score,
    expected_calibration_error,
    log_loss,
)


# --- Metrics --------------------------------------------------------------

def test_brier_perfect_predictions():
    y_true = np.array([0, 1, 0, 1])
    y_prob = np.array([0.0, 1.0, 0.0, 1.0])
    assert brier_score(y_true, y_prob) == 0.0


def test_brier_random_predictions():
    """Random 0.5 predictor on 50/50 data has Brier = 0.25."""
    y_true = np.array([0, 1, 0, 1, 0, 1])
    y_prob = np.full(6, 0.5)
    assert brier_score(y_true, y_prob) == pytest.approx(0.25)


def test_log_loss_perfect_zero():
    y_true = np.array([0, 1])
    y_prob = np.array([0.0, 1.0])
    # Should be near zero (clip prevents -inf)
    assert log_loss(y_true, y_prob) < 1e-10


def test_log_loss_worst_case():
    y_true = np.array([0, 1])
    y_prob = np.array([1.0, 0.0])
    # Predicted opposite of truth = very high log loss
    assert log_loss(y_true, y_prob) > 10.0


def test_ece_perfect_calibration():
    # Predictions perfectly match observed frequency in each bucket
    y_true = np.array([0, 0, 1, 1])
    y_prob = np.array([0.0, 0.0, 1.0, 1.0])
    assert expected_calibration_error(y_true, y_prob) == 0.0


def test_ece_miscalibrated():
    # Always predicts 0.9 but only 50% are 1s -> bad calibration
    y_true = np.array([0, 0, 1, 1])
    y_prob = np.array([0.9, 0.9, 0.9, 0.9])
    assert expected_calibration_error(y_true, y_prob) > 0.3


# --- Calibrator -----------------------------------------------------------

def test_isotonic_basic():
    """Calibrator should pull miscalibrated predictions toward observed."""
    rng = np.random.default_rng(0)
    n = 1000
    raw = rng.uniform(0, 1, n)
    # The actual probability is raw squared (so model is OVERconfident at high values)
    actual_p = raw ** 2
    y = (rng.uniform(0, 1, n) < actual_p).astype(int)

    cal = IsotonicCalibrator().fit(raw, y)
    calibrated = cal.transform(raw)

    # After calibration, predicted prob should be closer to true prob
    raw_brier = np.mean((raw - actual_p) ** 2)
    cal_brier = np.mean((calibrated - actual_p) ** 2)
    assert cal_brier < raw_brier


def test_isotonic_unfitted_raises():
    cal = IsotonicCalibrator()
    with pytest.raises(RuntimeError):
        cal.transform(np.array([0.5]))


# --- Walk-forward folds ---------------------------------------------------

def test_walk_forward_basic():
    import pandas as pd
    # 2 years of weekly games starting 2024-01-01
    dates = pd.date_range("2024-01-01", "2025-12-31", freq="7D")
    df = pd.DataFrame({"date": dates.strftime("%Y-%m-%d")})

    folds = walk_forward_folds(df, initial_train_days=365, test_window_days=28, step_days=28)
    assert len(folds) > 0
    # Each test_start should be strictly after train_end? Actually they're equal
    # (train_end is exclusive). Each next fold's test_start should advance.
    for i in range(1, len(folds)):
        assert folds[i].test_start > folds[i - 1].test_start

    # First fold's train_end is at least initial_train_days after earliest date
    assert folds[0].test_start >= "2024-12-31"


def test_walk_forward_slice_no_leak():
    """A fold's training set must contain no dates >= train_end."""
    import pandas as pd
    dates = pd.date_range("2024-01-01", "2025-12-31", freq="7D")
    df = pd.DataFrame({
        "date": dates.strftime("%Y-%m-%d"),
        "label": np.zeros(len(dates), dtype=int),
        "x": np.ones(len(dates), dtype=float),
    })
    folds = walk_forward_folds(df, initial_train_days=365)
    fold = folds[0]
    train, test = slice_for_fold(df, fold)
    assert train["date"].max() < fold.train_end
    assert test["date"].min() >= fold.test_start
    assert test["date"].max() < fold.test_end


# --- Integration: tiny end-to-end train pipeline --------------------------

def test_end_to_end_train_tiny(tmp_path: Path):
    """Build a tiny synthetic features DataFrame and train a model on it.
    Verifies the artifact-save / Model.load round-trip and basic shape."""
    import pandas as pd
    from loltrader.model.serve import Model
    from loltrader.model.train import save_artifact

    rng = np.random.default_rng(0)
    n = 600
    # Synthetic features where team_a_rating_diff > 0 strongly predicts win
    rating_diff = rng.normal(0, 200, n)
    # Probability of team_a winning is a logistic of rating_diff/100
    p = 1.0 / (1.0 + np.exp(-rating_diff / 100.0))
    y = (rng.uniform(0, 1, n) < p).astype(int)
    dates = pd.date_range("2024-01-01", periods=n, freq="1D").strftime("%Y-%m-%d")

    df = pd.DataFrame({
        "match_id": np.arange(n),
        "date": dates,
        "league": "LCS",
        "team_a_id": 1,
        "team_b_id": 2,
        "label": y,
        "rating_diff": rating_diff,
        "noise": rng.normal(0, 1, n),
    })

    # Run a tiny manual training pipeline using the same primitives as train()
    from loltrader.model.calibrate import IsotonicCalibrator
    from loltrader.model.train import TrainedArtifact, _train_one_model

    from loltrader.model.dataset import split_xy
    X, y_, feat_cols = split_xy(df)
    final_model = _train_one_model(X, y_)

    p_raw = final_model.predict_proba(X)[:, 1]
    cal = IsotonicCalibrator().fit(p_raw, y_)

    artifact = TrainedArtifact(
        model=final_model,
        calibrator=cal,
        ensemble=[final_model],  # single-element ensemble for test
        feature_spec=feat_cols,
        metadata={"trained_at_utc": "test"},
    )

    path = tmp_path / "tiny.pkl"
    save_artifact(artifact, path)
    assert path.exists()

    # Round-trip
    m = Model.load(path)
    sample_features = {col: float(df[col].iloc[0]) for col in feat_cols}
    pred = m.predict_dict(sample_features)
    assert 0.0 <= pred.yes_prob <= 1.0
    assert pred.p10 <= pred.yes_prob <= pred.p90 or pred.p10 == pred.p90
    # Highly predictive feature: large positive rating_diff -> high yes_prob
    big_a = m.predict_dict({"rating_diff": 500.0, "noise": 0.0})
    big_b = m.predict_dict({"rating_diff": -500.0, "noise": 0.0})
    assert big_a.yes_prob > big_b.yes_prob


def test_model_serve_rejects_missing_feature(tmp_path: Path):
    """A features dict missing a required key should raise."""
    from loltrader.model.calibrate import IsotonicCalibrator
    from loltrader.model.serve import Model
    from loltrader.model.train import TrainedArtifact, _train_one_model, save_artifact

    rng = np.random.default_rng(1)
    X = rng.normal(0, 1, (100, 3))
    y = (X[:, 0] + rng.normal(0, 0.1, 100) > 0).astype(int)
    model = _train_one_model(X, y)
    cal = IsotonicCalibrator().fit(model.predict_proba(X)[:, 1], y)
    art = TrainedArtifact(
        model=model, calibrator=cal, ensemble=[model],
        feature_spec=["f0", "f1", "f2"], metadata={},
    )
    path = tmp_path / "m.pkl"
    save_artifact(art, path)

    m = Model.load(path)
    with pytest.raises(KeyError):
        m.predict_dict({"f0": 1.0, "f1": 2.0})  # missing f2
