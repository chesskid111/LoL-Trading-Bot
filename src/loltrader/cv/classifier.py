"""Frame classifier: in_game | studio | replay | ads | unknown.

Approach (spec §6.2, "First pass: template-difference heuristic"):
    1. Maintain a small library of reference frames per class on disk:
       data/cv_references/{class_name}/*.png
    2. At init, load and downsample all reference frames to a fixed compare-size.
    3. For each input frame, downsample, then compute SSIM against each reference.
    4. Assign to the class with highest MEAN SSIM across that class's references
       (mean is more robust than max — single outlier reference can't lie).
    5. If max class-mean SSIM is below UNKNOWN_THRESHOLD, return 'unknown'.

SSIM is implemented in pure NumPy/OpenCV (no scikit-image dep). Standard
formula from Wang et al., 2004.

If first-pass accuracy is insufficient (stage-1 acceptance criterion: ≥90%
matching manual annotation), upgrade to a small CNN — spec §17 #3.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

log = logging.getLogger(__name__)

# Compare-size (W, H). Small enough for fast SSIM, large enough to retain
# enough structure to distinguish in_game from studio. Empirical;
# can be tuned in stage 1.
COMPARE_SIZE: tuple[int, int] = (320, 180)

# Classes the classifier can return.
CLASSES = ("in_game", "studio", "replay", "ads")
UNKNOWN = "unknown"

# Default threshold below which we return UNKNOWN. Calibrated in stage 1.
DEFAULT_UNKNOWN_THRESHOLD: float = 0.15

# SSIM constants (Wang et al., 2004). For uint8 images.
_K1, _K2 = 0.01, 0.03
_L = 255.0
_C1 = (_K1 * _L) ** 2
_C2 = (_K2 * _L) ** 2


@dataclass(frozen=True)
class ClassificationResult:
    label: str
    confidence: float  # 0..1
    per_class_scores: dict[str, float]  # SSIM for each class (max across refs)


def _ssim(img_a: np.ndarray, img_b: np.ndarray) -> float:
    """Structural Similarity Index between two single-channel uint8 images.

    Implements the standard formula on a single global window (faster than
    per-pixel Gaussian-weighted SSIM and adequate for our coarse classification).
    Returns a value in approximately [-1, 1]; identical images → 1.0.
    """
    if img_a.shape != img_b.shape:
        raise ValueError(f"SSIM requires equal shapes, got {img_a.shape} vs {img_b.shape}")
    a = img_a.astype(np.float64)
    b = img_b.astype(np.float64)
    mu_a, mu_b = a.mean(), b.mean()
    var_a = a.var()
    var_b = b.var()
    cov_ab = ((a - mu_a) * (b - mu_b)).mean()
    num = (2 * mu_a * mu_b + _C1) * (2 * cov_ab + _C2)
    den = (mu_a ** 2 + mu_b ** 2 + _C1) * (var_a + var_b + _C2)
    if den == 0:
        return 0.0
    return float(num / den)


def _preprocess(img: np.ndarray) -> np.ndarray:
    """Downsample to COMPARE_SIZE and convert to grayscale."""
    h, w = COMPARE_SIZE[1], COMPARE_SIZE[0]
    resized = cv2.resize(img, (w, h), interpolation=cv2.INTER_AREA)
    if resized.ndim == 3:
        gray = cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY)
    else:
        gray = resized
    return gray


def _load_reference_library(refs_dir: Path) -> dict[str, list[np.ndarray]]:
    """Load reference PNGs from refs_dir/{class}/*.png into preprocessed arrays.

    Missing class subdirectories are silently treated as zero-reference (that
    class won't be selectable). The classifier will report 'unknown' if no
    class has any references.
    """
    library: dict[str, list[np.ndarray]] = {c: [] for c in CLASSES}
    if not refs_dir.exists():
        log.warning("Reference dir does not exist: %s", refs_dir)
        return library
    for cls in CLASSES:
        cls_dir = refs_dir / cls
        if not cls_dir.exists():
            continue
        pngs = sorted(cls_dir.glob("*.png"))
        for p in pngs:
            img = cv2.imread(str(p), cv2.IMREAD_COLOR)
            if img is None:
                log.warning("Failed to read reference image: %s", p)
                continue
            library[cls].append(_preprocess(img))
        log.info("Loaded %d reference frames for class '%s'", len(library[cls]), cls)
    return library


class FrameClassifier:
    """Stateful classifier. Holds preprocessed reference frames in memory."""

    def __init__(self, refs_dir: Path,
                 unknown_threshold: float = DEFAULT_UNKNOWN_THRESHOLD) -> None:
        self._refs_dir = Path(refs_dir)
        self._library = _load_reference_library(self._refs_dir)
        self._unknown_threshold = unknown_threshold
        self._n_refs = sum(len(v) for v in self._library.values())
        log.info("FrameClassifier ready with %d total reference frames", self._n_refs)

    @property
    def has_references(self) -> bool:
        return self._n_refs > 0

    def class_reference_counts(self) -> dict[str, int]:
        return {cls: len(refs) for cls, refs in self._library.items()}

    def classify(self, img: np.ndarray) -> ClassificationResult:
        """Classify a single BGR image.

        If no references are loaded (e.g. fresh install), returns UNKNOWN
        with confidence 0.
        """
        if not self.has_references:
            return ClassificationResult(
                label=UNKNOWN, confidence=0.0,
                per_class_scores={c: 0.0 for c in CLASSES},
            )

        target = _preprocess(img)
        scores: dict[str, float] = {}
        for cls in CLASSES:
            refs = self._library[cls]
            if not refs:
                scores[cls] = 0.0
                continue
            ssims = [_ssim(target, r) for r in refs]
            # Use mean — more robust than max against a single noisy reference.
            scores[cls] = float(np.mean(ssims))

        best_class = max(scores, key=lambda c: scores[c])
        best_score = scores[best_class]

        if best_score < self._unknown_threshold:
            return ClassificationResult(
                label=UNKNOWN, confidence=best_score, per_class_scores=scores,
            )
        return ClassificationResult(
            label=best_class, confidence=best_score, per_class_scores=scores,
        )

    def classify_batch(self, imgs: Iterable[np.ndarray]) -> list[ClassificationResult]:
        return [self.classify(img) for img in imgs]
