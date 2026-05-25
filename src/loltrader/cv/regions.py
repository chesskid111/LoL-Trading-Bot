"""Broadcast region coordinates for OCR extraction.

Stores regions as fractional coordinates (0..1) so they scale to any
resolution. Calibrated against the LCK 2026 Spring broadcast layout
empirically — see docs/superpowers/specs/2026-05-24-lol-trading-bot-v2-design.md
§17 #2 (resolution variants) and the Phase 4 build journal.

If LCK's production team refreshes the broadcast overlay (typical at
split boundaries), region coords need recalibration. Add a new
``LCK_BROADCAST_2026_SUMMER`` etc. and switch the active config.

Pragmatic CV-OCR scope (refined from spec §6.2):
    - gold (per team) + game_timer    — CV-primary (real freshness gain)
    - kills/towers/dragons/barons     — livestats-primary (event-driven,
      30s delay tolerable, OCR was unreliable on smaller fields)

This isn't a CV failure — it's a sensible scope decision. The model's
features are unchanged; the SOURCE for some fields shifts from CV to
livestats.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import NamedTuple

import numpy as np


class FractionalBox(NamedTuple):
    """Region defined as fractions of the frame (resolution-independent)."""
    x1: float  # 0..1
    y1: float
    x2: float
    y2: float

    def pixels(self, frame_w: int, frame_h: int) -> tuple[int, int, int, int]:
        return (
            int(self.x1 * frame_w),
            int(self.y1 * frame_h),
            int(self.x2 * frame_w),
            int(self.y2 * frame_h),
        )


@dataclass(frozen=True)
class BroadcastRegions:
    """A complete set of region coordinates for a particular broadcast layout."""

    # Identifier for diagnostics + diffing between layout revisions
    name: str

    # OCR targets (numerical, big text, change rapidly)
    game_timer: FractionalBox
    blue_gold: FractionalBox
    red_gold: FractionalBox

    # Optional secondary OCR target — countdown to next dragon/baron
    objective_timer: FractionalBox | None = None

    # CV regions (not OCR'd, used by other extractors)
    minimap: FractionalBox | None = None

    def crop(self, frame: np.ndarray, box: FractionalBox) -> np.ndarray:
        """Crop a frame to the given fractional region."""
        h, w = frame.shape[:2]
        x1, y1, x2, y2 = box.pixels(w, h)
        return frame[y1:y2, x1:x2]


# Calibrated against frame_002280s in lck_2026_dns_gen_bro_t1.webm
# (1280x720, GEN vs DNS, in_game state, game time 6:37).
# Key lesson from calibration: exclude the gold-coin icon from both
# blue_gold and red_gold regions, or Tesseract reads it as a "0".
# Pixel coords → fractional:
#   game_timer:     x=15-90, y=15-45     → 0.012-0.070, 0.021-0.063
#   blue_gold:      x=512-562, y=20-40   → 0.400-0.439, 0.028-0.056
#   red_gold:       x=730-773, y=20-40   → 0.570-0.604, 0.028-0.056
#   objective_timer:x=1180-1260, y=15-45 → 0.922-0.984, 0.021-0.063
#   minimap:        x=1010-1280, y=510-720 → 0.789-1.0, 0.708-1.0
LCK_BROADCAST_2026 = BroadcastRegions(
    name="lck_broadcast_2026",
    game_timer=FractionalBox(0.012, 0.021, 0.070, 0.063),
    blue_gold=FractionalBox(0.400, 0.028, 0.439, 0.056),
    red_gold=FractionalBox(0.570, 0.028, 0.604, 0.056),
    objective_timer=FractionalBox(0.922, 0.021, 0.984, 0.063),
    minimap=FractionalBox(0.789, 0.708, 1.0, 1.0),
)


# Default active config. To support a different broadcast (LCS / LEC etc.)
# add a new BroadcastRegions instance and select via config.
ACTIVE_REGIONS: BroadcastRegions = LCK_BROADCAST_2026


def get_regions(layout_name: str | None = None) -> BroadcastRegions:
    """Return the active regions config, or a specific named one.

    Defaults to ACTIVE_REGIONS. Pass a layout_name to select an alternate
    (currently only 'lck_broadcast_2026' exists).
    """
    if layout_name is None or layout_name == ACTIVE_REGIONS.name:
        return ACTIVE_REGIONS
    # Future: dispatch on name when we have multiple layouts
    raise KeyError(f"unknown broadcast layout: {layout_name}")
