"""LiveWinProbModel — XGBoost ensemble + isotonic calibrator wrapper.

This is the serving-side class. It loads a pickled bundle containing:

  - ensemble: list of 10 trained XGBoost classifiers (bootstrap members)
  - calibrator: IsotonicRegression fit on holdout predictions
  - feature_schema: list[str] — feature column order
  - metadata: training info (date, dataset size, validation metrics)

At inference time:
  - features dict in → vector in correct order → ensemble predictions →
    mean for central estimate → calibrator transforms → p10/p90 from
    ensemble for uncertainty band

The output ``WinProbPrediction`` is what the risk manager + dashboard consume.
"""
from __future__ import annotations

import logging
import pickle
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class WinProbPrediction:
    """One prediction's full output — calibrated central value + uncertainty."""
    p_blue: float                  # calibrated P(blue wins)
    p10: float                     # 10th percentile from ensemble (calibrated)
    p90: float                     # 90th percentile from ensemble (calibrated)
    raw_p_blue: float              # uncalibrated central estimate
    band_width: float              # p90 - p10 — used by risk manager
    feature_importances: dict[str, float] = field(default_factory=dict)


class LiveWinProbModel:
    """XGBoost ensemble + isotonic calibrator wrapper for live inference."""

    def __init__(
        self,
        ensemble: list,                  # list[xgb.XGBClassifier]
        calibrator,                      # IsotonicRegression
        feature_schema: list[str],
        metadata: dict,
    ) -> None:
        self.ensemble = ensemble
        self.calibrator = calibrator
        self.feature_schema = feature_schema
        self.metadata = metadata

    @classmethod
    def load(cls, path: str | Path) -> "LiveWinProbModel":
        """Load a pickled model bundle."""
        with open(path, "rb") as f:
            d = pickle.load(f)
        return cls(
            ensemble=d["ensemble"],
            calibrator=d["calibrator"],
            feature_schema=d["feature_schema"],
            metadata=d["metadata"],
        )

    def save(self, path: str | Path) -> None:
        """Pickle the model bundle to disk."""
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as f:
            pickle.dump({
                "ensemble": self.ensemble,
                "calibrator": self.calibrator,
                "feature_schema": self.feature_schema,
                "metadata": self.metadata,
            }, f)

    def _features_to_vector(self, features: dict) -> np.ndarray:
        """Order features per schema. Missing keys default to 0.0."""
        row = [float(features.get(k, 0.0)) for k in self.feature_schema]
        return np.array(row, dtype=np.float32).reshape(1, -1)

    def predict(self, features: dict) -> WinProbPrediction:
        """Run inference on one feature dict. Returns calibrated prediction."""
        X = self._features_to_vector(features)

        # Each ensemble member's predicted P(blue wins)
        raw_probs = np.array([
            float(m.predict_proba(X)[0, 1]) for m in self.ensemble
        ])
        raw_mean = float(raw_probs.mean())

        # Calibrate the mean and the percentiles independently. This keeps the
        # band on the same probability scale as the central estimate.
        p_blue = float(self.calibrator.transform([raw_mean])[0])
        p10 = float(self.calibrator.transform([float(np.percentile(raw_probs, 10))])[0])
        p90 = float(self.calibrator.transform([float(np.percentile(raw_probs, 90))])[0])

        return WinProbPrediction(
            p_blue=p_blue,
            p10=p10,
            p90=p90,
            raw_p_blue=raw_mean,
            band_width=p90 - p10,
        )

    def predict_batch(self, X: np.ndarray) -> np.ndarray:
        """Batch prediction for evaluation / backtest. Returns calibrated P(blue)."""
        # Ensemble mean
        raw = np.mean(
            [m.predict_proba(X)[:, 1] for m in self.ensemble], axis=0
        )
        return self.calibrator.transform(raw)
