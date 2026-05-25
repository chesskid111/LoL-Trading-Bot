"""Tests for loltrader.cv.classifier (SSIM-based frame classifier)."""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from loltrader.cv import classifier
from loltrader.cv.classifier import (
    CLASSES,
    UNKNOWN,
    FrameClassifier,
    _ssim,
)


def _synthetic_class_image(seed: int, color_offset: tuple[int, int, int]) -> np.ndarray:
    """Make a deterministic 'image' for a synthetic class. Two classes will
    look very different from each other, so the classifier should distinguish them."""
    rng = np.random.RandomState(seed)
    img = rng.randint(0, 30, size=(180, 320, 3), dtype=np.uint8)
    # Add a class-specific structural bias (a colored band) so each class has
    # a distinct mean signature.
    band_color = np.array(color_offset, dtype=np.uint8)
    img[40:80, :, :] = band_color
    img[100:140, :, :] = band_color // 2
    return img


def _write_refs(tmp_path: Path, refs_by_class: dict[str, list[np.ndarray]]) -> Path:
    """Write reference frames to a temp directory in the expected layout."""
    refs_dir = tmp_path / "cv_references"
    for cls, frames in refs_by_class.items():
        cls_dir = refs_dir / cls
        cls_dir.mkdir(parents=True, exist_ok=True)
        for i, img in enumerate(frames):
            cv2.imwrite(str(cls_dir / f"{i:03d}.png"), img)
    return refs_dir


def test_ssim_identical_is_one() -> None:
    img = np.random.RandomState(0).randint(0, 256, size=(50, 50), dtype=np.uint8)
    assert abs(_ssim(img, img) - 1.0) < 1e-9


def test_ssim_zero_arrays_is_one() -> None:
    img = np.zeros((50, 50), dtype=np.uint8)
    assert abs(_ssim(img, img) - 1.0) < 1e-9


def test_ssim_different_arrays_below_one() -> None:
    a = np.zeros((50, 50), dtype=np.uint8)
    b = np.full((50, 50), 255, dtype=np.uint8)
    assert _ssim(a, b) < 0.5


def test_classifier_with_no_references_returns_unknown(tmp_path: Path) -> None:
    refs_dir = tmp_path / "empty_refs"
    clf = FrameClassifier(refs_dir)
    img = np.random.randint(0, 256, size=(180, 320, 3), dtype=np.uint8)
    result = clf.classify(img)
    assert result.label == UNKNOWN
    assert result.confidence == 0.0


def test_classifier_distinguishes_two_classes(tmp_path: Path) -> None:
    """Build two synthetic classes with different visual signatures. Verify
    that input matching one class's signature gets classified into that class."""
    in_game_refs = [_synthetic_class_image(s, (50, 200, 50)) for s in range(3)]
    studio_refs = [_synthetic_class_image(s + 100, (200, 50, 50)) for s in range(3)]
    refs_dir = _write_refs(tmp_path, {
        "in_game": in_game_refs,
        "studio": studio_refs,
    })
    clf = FrameClassifier(refs_dir, unknown_threshold=0.0)

    # Generate a new image with the same visual style as in_game class
    target = _synthetic_class_image(7, (50, 200, 50))
    result = clf.classify(target)
    assert result.label == "in_game"
    assert result.confidence > 0.3  # well above unknown threshold

    # And one with studio's signature
    target_studio = _synthetic_class_image(7, (200, 50, 50))
    result2 = clf.classify(target_studio)
    assert result2.label == "studio"


def test_classifier_unknown_threshold(tmp_path: Path) -> None:
    """When max-class-score is below the threshold, classifier returns UNKNOWN."""
    in_game_refs = [_synthetic_class_image(s, (50, 200, 50)) for s in range(3)]
    refs_dir = _write_refs(tmp_path, {"in_game": in_game_refs})

    # Use a very-high threshold so even similar images fall below it.
    clf = FrameClassifier(refs_dir, unknown_threshold=0.999)
    target = _synthetic_class_image(99, (50, 200, 50))
    result = clf.classify(target)
    assert result.label == UNKNOWN
    # But the score should still be the per-class score
    assert result.per_class_scores["in_game"] > 0


def test_classifier_class_reference_counts(tmp_path: Path) -> None:
    refs_dir = _write_refs(tmp_path, {
        "in_game": [_synthetic_class_image(s, (50, 200, 50)) for s in range(3)],
        "studio":  [_synthetic_class_image(s + 100, (200, 50, 50)) for s in range(2)],
    })
    clf = FrameClassifier(refs_dir)
    counts = clf.class_reference_counts()
    assert counts["in_game"] == 3
    assert counts["studio"] == 2
    assert counts["replay"] == 0
    assert counts["ads"] == 0


def test_classifier_per_class_scores_cover_all_classes(tmp_path: Path) -> None:
    """Even when only some classes have refs, per_class_scores has entries for all."""
    refs_dir = _write_refs(tmp_path, {
        "in_game": [_synthetic_class_image(0, (50, 200, 50))],
    })
    clf = FrameClassifier(refs_dir, unknown_threshold=0.0)
    target = _synthetic_class_image(7, (50, 200, 50))
    result = clf.classify(target)
    assert set(result.per_class_scores.keys()) == set(CLASSES)
    assert result.per_class_scores["studio"] == 0.0
    assert result.per_class_scores["replay"] == 0.0
    assert result.per_class_scores["ads"] == 0.0


def test_classifier_ignores_unreadable_reference_files(tmp_path: Path, caplog) -> None:
    """If a reference PNG is corrupt/unreadable, the classifier logs and skips it."""
    refs_dir = tmp_path / "cv_references" / "in_game"
    refs_dir.mkdir(parents=True)
    # Write one valid + one corrupt
    img = _synthetic_class_image(0, (50, 200, 50))
    cv2.imwrite(str(refs_dir / "good.png"), img)
    (refs_dir / "broken.png").write_bytes(b"not a png")

    clf = FrameClassifier(refs_dir.parent)
    counts = clf.class_reference_counts()
    assert counts["in_game"] == 1  # only the good one
