"""OCR extraction for broadcast text regions.

Scope (per Phase 4 empirical findings, refining spec §6.2):
    - gold (per team)   — CV primary, OCR'd from broadcast scoreboard
    - game_timer        — CV primary, used for in-game clock cross-check

NOT OCR'd here (livestats primary):
    - kills, towers, dragons, barons, inhibitors

Why: kills/towers/etc are smaller text in the broadcast scoreboard with
unreliable OCR. The fields change as discrete events, so the 30s livestats
delay is acceptable. Gold and timer change every second, so CV freshness
buys real signal there.

Engine: Tesseract via pytesseract. PaddleOCR fallback deferred (spec §17 #4)
until empirical OCR-vs-livestats divergence exceeds threshold.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
import pytesseract

from loltrader.cv.regions import BroadcastRegions, FractionalBox, get_regions

log = logging.getLogger(__name__)

# Default Tesseract install path on Windows (UB-Mannheim distribution).
# Override via LOLTRADER_TESSERACT_CMD env var if installed elsewhere.
_DEFAULT_TESSERACT_PATH = Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe")


def _configure_tesseract() -> None:
    override = os.environ.get("LOLTRADER_TESSERACT_CMD")
    if override:
        pytesseract.pytesseract.tesseract_cmd = override
        return
    if _DEFAULT_TESSERACT_PATH.exists():
        pytesseract.pytesseract.tesseract_cmd = str(_DEFAULT_TESSERACT_PATH)
    # else: rely on PATH


_configure_tesseract()


# Tesseract page-segmentation modes:
#   7 = treat the image as a single line of text (best for short numerical strings)
#   8 = treat the image as a single word
_PSM_LINE = "7"
_PSM_WORD = "8"


# ---------- low-level helpers ----------


def _preprocess_for_ocr(crop: np.ndarray, upscale: int = 4) -> np.ndarray:
    """Standard pipeline: upscale → grayscale → Otsu threshold → invert if dark.

    Tesseract works best on high-contrast, dark-text-on-light images. The
    LCK broadcast scoreboard is the inverse (light text on dark/translucent
    background), so we Otsu-threshold and invert when needed.
    """
    if crop.size == 0:
        return crop
    h, w = crop.shape[:2]
    if upscale > 1:
        crop = cv2.resize(crop, (w * upscale, h * upscale), interpolation=cv2.INTER_CUBIC)
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop
    _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
    # If thresholded image is predominantly black (i.e. text is white-on-black),
    # invert so Tesseract sees dark text on light.
    if thresh.mean() < 127:
        thresh = cv2.bitwise_not(thresh)
    return thresh


def _ocr_digits(crop: np.ndarray, whitelist: str, psm: str = _PSM_LINE) -> str:
    """Run Tesseract with a character whitelist. Returns stripped string."""
    if crop is None or crop.size == 0:
        return ""
    processed = _preprocess_for_ocr(crop)
    config = f"--psm {psm} -c tessedit_char_whitelist={whitelist}"
    text = pytesseract.image_to_string(processed, config=config)
    return text.strip()


# ---------- field parsers ----------


_GOLD_RE = re.compile(r"^(\d+(?:\.\d+)?)([Kk])?$")


def parse_gold(ocr_text: str) -> int | None:
    """Parse OCR text into integer gold.

    Examples:
        "3.0K" -> 3000
        "12.7K" -> 12700
        "873" -> 873
        "03.0K" -> 3000 (leading-zero OCR artifact, tolerated)
        "29K0" -> None (mangled, return None and let watchdog flag)
    """
    if not ocr_text:
        return None
    # Strip leading zeros that aren't part of decimal (OCR artifact)
    cleaned = ocr_text.lstrip("0") or "0"
    m = _GOLD_RE.match(cleaned)
    if not m:
        return None
    num_str, k_suffix = m.groups()
    try:
        value = float(num_str)
    except ValueError:
        return None
    if k_suffix:
        value *= 1000
    return int(round(value))


_TIMER_RE = re.compile(r"^(\d+):(\d{2})$")


def parse_timer(ocr_text: str) -> int | None:
    """Parse 'M:SS' or 'MM:SS' into total seconds. Returns None if malformed."""
    if not ocr_text:
        return None
    # Tesseract sometimes includes trailing garbage; take first ":NN" pattern
    m = _TIMER_RE.match(ocr_text.split()[0] if ocr_text.split() else "")
    if not m:
        return None
    minutes, seconds = int(m.group(1)), int(m.group(2))
    if seconds >= 60:
        return None
    return minutes * 60 + seconds


# ---------- public API ----------


@dataclass(frozen=True)
class OcrResult:
    """Per-frame OCR output. None for fields that couldn't be parsed."""
    blue_gold: int | None
    red_gold: int | None
    game_timer_seconds: int | None
    objective_timer_seconds: int | None

    # Raw OCR text for debugging / audit. Kept short.
    raw_blue_gold: str = ""
    raw_red_gold: str = ""
    raw_game_timer: str = ""
    raw_objective_timer: str = ""


def ocr_frame(frame: np.ndarray, regions: BroadcastRegions | None = None) -> OcrResult:
    """Run all OCR extractors against one in_game frame."""
    if regions is None:
        regions = get_regions()

    raw_blue = _ocr_digits(regions.crop(frame, regions.blue_gold), "0123456789.K")
    raw_red = _ocr_digits(regions.crop(frame, regions.red_gold), "0123456789.K")
    raw_timer = _ocr_digits(regions.crop(frame, regions.game_timer), "0123456789:")
    raw_obj = ""
    if regions.objective_timer is not None:
        raw_obj = _ocr_digits(regions.crop(frame, regions.objective_timer), "0123456789:")

    return OcrResult(
        blue_gold=parse_gold(raw_blue),
        red_gold=parse_gold(raw_red),
        game_timer_seconds=parse_timer(raw_timer),
        objective_timer_seconds=parse_timer(raw_obj),
        raw_blue_gold=raw_blue,
        raw_red_gold=raw_red,
        raw_game_timer=raw_timer,
        raw_objective_timer=raw_obj,
    )


def ocr_region(frame: np.ndarray, box: FractionalBox,
               whitelist: str = "0123456789") -> str:
    """One-off OCR for an arbitrary region. Mostly for debugging / iteration."""
    h, w = frame.shape[:2]
    x1, y1, x2, y2 = box.pixels(w, h)
    return _ocr_digits(frame[y1:y2, x1:x2], whitelist)
