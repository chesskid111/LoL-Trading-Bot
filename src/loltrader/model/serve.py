"""Model loading + inference.

Usage:
    from loltrader.model.serve import Model

    model = Model.load("models/v1_<ts>.pkl")
    yes_prob, p10, p90 = model.predict_dict({"team_a_glicko_rating": 1800, ...})
"""
from __future__ import annotations

import pickle
from dataclasses import dataclass
from pathlib import Path

import numpy as np


@dataclass
class Prediction:
    yes_prob: float       # calibrated probability
    p10: float            # 10th percentile of ensemble predictions
    p90: float            # 90th percentile of ensemble predictions
    raw_prob: float       # uncalibrated, for diagnostics


class Model:
    def __init__(
        self,
        model,
        calibrator,
        ensemble: list,
        feature_spec: list[str],
        metadata: dict,
    ) -> None:
        self._model = model
        self._calibrator = calibrator
        self._ensemble = ensemble
        self.feature_spec = feature_spec
        self.metadata = metadata

    @classmethod
    def load(cls, path: str | Path) -> "Model":
        with open(path, "rb") as f:
            d = pickle.load(f)
        return cls(
            model=d["model"],
            calibrator=d["calibrator"],
            ensemble=d["ensemble"],
            feature_spec=d["feature_spec"],
            metadata=d["metadata"],
        )

    def _dict_to_array(self, features: dict[str, float]) -> np.ndarray:
        """Convert a features dict to a row-vector in the exact order the
        model was trained on. Raises on missing or unexpected keys."""
        try:
            row = [features[k] for k in self.feature_spec]
        except KeyError as e:
            raise KeyError(
                f"Feature dict missing required key {e}. "
                f"Expected keys: {self.feature_spec[:5]}..."
            ) from e
        unexpected = set(features) - set(self.feature_spec)
        if unexpected:
            # Not fatal — features may include richer downstream signals —
            # but warn so users know they're being dropped.
            import warnings
            warnings.warn(
                f"Extra feature keys ignored: {sorted(unexpected)[:5]}{'...' if len(unexpected) > 5 else ''}",
                stacklevel=2,
            )
        return np.array(row, dtype=np.float64).reshape(1, -1)

    def predict_dict(self, features: dict[str, float]) -> Prediction:
        """Predict from a features dict (output of compute_features)."""
        X = self._dict_to_array(features)
        raw = float(self._model.predict_proba(X)[0, 1])
        calibrated = float(self._calibrator.transform(np.array([raw]))[0])

        # Ensemble for uncertainty band
        ens_preds = np.array([
            float(m.predict_proba(X)[0, 1]) for m in self._ensemble
        ])
        # Apply the same calibrator to ensemble preds so the band is on the
        # same probability scale as the central estimate.
        ens_cal = self._calibrator.transform(ens_preds)
        p10 = float(np.percentile(ens_cal, 10))
        p90 = float(np.percentile(ens_cal, 90))

        return Prediction(
            yes_prob=calibrated,
            p10=p10,
            p90=p90,
            raw_prob=raw,
        )

    def __repr__(self) -> str:
        return (
            f"Model(n_features={len(self.feature_spec)}, "
            f"ensemble={len(self._ensemble)}, "
            f"trained_at={self.metadata.get('trained_at_utc')})"
        )
