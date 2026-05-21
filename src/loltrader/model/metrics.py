"""Calibration metrics: Brier score, ECE, log loss, reliability diagram."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np


@dataclass(frozen=True)
class CalibrationMetrics:
    brier: float
    log_loss: float
    accuracy: float
    ece: float           # Expected Calibration Error (10 buckets)
    n_samples: int


def brier_score(y_true: np.ndarray, y_prob: np.ndarray) -> float:
    """Mean squared error between predicted probability and binary outcome."""
    return float(np.mean((y_prob - y_true) ** 2))


def log_loss(y_true: np.ndarray, y_prob: np.ndarray, eps: float = 1e-15) -> float:
    """Binary cross-entropy. Clipped to avoid log(0)."""
    p = np.clip(y_prob, eps, 1 - eps)
    return float(-np.mean(y_true * np.log(p) + (1 - y_true) * np.log(1 - p)))


def expected_calibration_error(
    y_true: np.ndarray, y_prob: np.ndarray, n_bins: int = 10
) -> float:
    """Bin predicted probabilities into n_bins equal-width buckets; for
    each, compute |mean_pred - mean_actual| weighted by bucket size."""
    bins = np.linspace(0.0, 1.0, n_bins + 1)
    ece = 0.0
    n = len(y_true)
    for i in range(n_bins):
        lo, hi = bins[i], bins[i + 1]
        in_bin = (y_prob >= lo) & (y_prob < hi if i < n_bins - 1 else y_prob <= hi)
        if not np.any(in_bin):
            continue
        bucket_pred = np.mean(y_prob[in_bin])
        bucket_actual = np.mean(y_true[in_bin])
        ece += (np.sum(in_bin) / n) * abs(bucket_pred - bucket_actual)
    return float(ece)


def calibration_metrics(y_true: np.ndarray, y_prob: np.ndarray) -> CalibrationMetrics:
    return CalibrationMetrics(
        brier=brier_score(y_true, y_prob),
        log_loss=log_loss(y_true, y_prob),
        accuracy=float(np.mean((y_prob >= 0.5) == (y_true == 1))),
        ece=expected_calibration_error(y_true, y_prob),
        n_samples=len(y_true),
    )


def reliability_diagram(
    y_true: np.ndarray,
    y_prob: np.ndarray,
    out_path: str | Path,
    n_bins: int = 10,
    title: str = "Reliability diagram",
) -> Path:
    """Save a reliability diagram (matplotlib) to disk. Returns the path."""
    import matplotlib
    matplotlib.use("Agg")  # headless
    import matplotlib.pyplot as plt

    bins = np.linspace(0.0, 1.0, n_bins + 1)
    midpoints, accuracies, counts = [], [], []
    for i in range(n_bins):
        lo, hi = bins[i], bins[i + 1]
        in_bin = (y_prob >= lo) & (y_prob < hi if i < n_bins - 1 else y_prob <= hi)
        if not np.any(in_bin):
            midpoints.append((lo + hi) / 2)
            accuracies.append(np.nan)
            counts.append(0)
            continue
        midpoints.append((lo + hi) / 2)
        accuracies.append(np.mean(y_true[in_bin]))
        counts.append(int(np.sum(in_bin)))

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 5))

    ax1.plot([0, 1], [0, 1], "k--", label="Perfect calibration")
    ax1.plot(midpoints, accuracies, "bo-", label="Model")
    ax1.set_xlabel("Predicted probability")
    ax1.set_ylabel("Observed frequency")
    ax1.set_title(title)
    ax1.legend()
    ax1.set_xlim(0, 1)
    ax1.set_ylim(0, 1)
    ax1.grid(True, alpha=0.3)

    ax2.bar(midpoints, counts, width=1.0 / n_bins, edgecolor="black")
    ax2.set_xlabel("Predicted probability")
    ax2.set_ylabel("Sample count")
    ax2.set_title("Prediction distribution")
    ax2.grid(True, alpha=0.3)

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(out, dpi=120)
    plt.close(fig)
    return out
