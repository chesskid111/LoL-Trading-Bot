"""Post-hoc probability calibration via isotonic regression.

Workflow:
  1. Train the main classifier on train fold.
  2. Get out-of-fold predictions on a held-out calibration set.
  3. Fit isotonic regression: raw_prob -> calibrated_prob.
  4. At inference: ``calibrator.transform(raw_prob)`` -> calibrated_prob.

Isotonic preserves the ranking of the raw model but rescales so that
predicted probabilities match observed frequencies.
"""
from __future__ import annotations

import numpy as np
from sklearn.isotonic import IsotonicRegression


class IsotonicCalibrator:
    def __init__(self) -> None:
        self._iso = IsotonicRegression(
            out_of_bounds="clip", y_min=0.0, y_max=1.0
        )
        self._fitted = False

    def fit(self, raw_prob: np.ndarray, y_true: np.ndarray) -> "IsotonicCalibrator":
        self._iso.fit(np.asarray(raw_prob, dtype=np.float64),
                      np.asarray(y_true, dtype=np.float64))
        self._fitted = True
        return self

    def transform(self, raw_prob: np.ndarray) -> np.ndarray:
        if not self._fitted:
            raise RuntimeError("IsotonicCalibrator: call fit() before transform().")
        return self._iso.predict(np.asarray(raw_prob, dtype=np.float64))

    @property
    def fitted(self) -> bool:
        return self._fitted
