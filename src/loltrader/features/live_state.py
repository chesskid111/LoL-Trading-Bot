"""Per-frame feature extraction from livestats data.

Input: one row from live_frames (with raw_json containing per-participant detail).
Output: a flat feature vector for ML training.

Designed for the Brier backtest — uses only livestats numerical data, no CV.
Phase 5's full 220-feature spec is for the production model; this is a
focused ~50-feature set to test the edge thesis cheaply.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

# Role ordering (assumed: participants 1-5 = blue top/jng/mid/adc/sup; 6-10 = red same)
# Riot's livestats API order is consistent across LCK broadcasts (verified empirically).
ROLES = ["top", "jng", "mid", "adc", "sup"]


@dataclass
class FrameFeatures:
    """Flat feature dict for a single livestats frame.

    All features are differential (blue - red) or per-role-differential.
    The model predicts P(blue wins) — so positive values favor blue.
    """
    # Game time + state
    game_time_sec: int
    game_phase: str          # 'early' | 'mid' | 'late' | 'closeout'

    # Team-level diffs (10)
    gold_diff: int
    kills_diff: int
    towers_diff: int
    inhibitors_diff: int
    dragons_diff: int
    barons_diff: int
    soul_diff: int           # +1 if blue has 4+ drakes, -1 if red, 0 otherwise
    elder_diff: int          # +1 if blue has elder, -1 if red
    cs_diff: int
    health_diff: int         # total team current HP

    # Per-role diffs (5 roles × 4 fields = 20)
    role_gold_diff: dict[str, int] = field(default_factory=dict)
    role_level_diff: dict[str, int] = field(default_factory=dict)
    role_cs_diff: dict[str, int] = field(default_factory=dict)
    role_kp_diff: dict[str, float] = field(default_factory=dict)   # kills+assists - deaths

    # Pace / rate features
    gold_per_min_diff: float = 0.0
    cs_per_min_diff: float = 0.0
    kills_per_min_diff: float = 0.0

    # Game time x state interactions (helps tree model)
    gold_diff_x_time: float = 0.0   # gold diff scaled by time-in-game

    def to_dict(self) -> dict:
        """Flatten to single dict, one key per feature."""
        out = {
            "game_time_sec": self.game_time_sec,
            "game_phase_early":    int(self.game_phase == "early"),
            "game_phase_mid":      int(self.game_phase == "mid"),
            "game_phase_late":     int(self.game_phase == "late"),
            "game_phase_closeout": int(self.game_phase == "closeout"),
            "gold_diff":        self.gold_diff,
            "kills_diff":       self.kills_diff,
            "towers_diff":      self.towers_diff,
            "inhibitors_diff":  self.inhibitors_diff,
            "dragons_diff":     self.dragons_diff,
            "barons_diff":      self.barons_diff,
            "soul_diff":        self.soul_diff,
            "elder_diff":       self.elder_diff,
            "cs_diff":          self.cs_diff,
            "health_diff":      self.health_diff,
            "gold_per_min_diff":  self.gold_per_min_diff,
            "cs_per_min_diff":    self.cs_per_min_diff,
            "kills_per_min_diff": self.kills_per_min_diff,
            "gold_diff_x_time":   self.gold_diff_x_time,
        }
        for r in ROLES:
            out[f"role_{r}_gold_diff"]  = self.role_gold_diff.get(r, 0)
            out[f"role_{r}_level_diff"] = self.role_level_diff.get(r, 0)
            out[f"role_{r}_cs_diff"]    = self.role_cs_diff.get(r, 0)
            out[f"role_{r}_kp_diff"]    = self.role_kp_diff.get(r, 0)
        return out


def _phase(seconds: int) -> str:
    """Bucket game time into spec §8 phase labels."""
    minutes = seconds / 60
    if minutes <= 10:
        return "early"
    if minutes <= 20:
        return "mid"
    if minutes <= 30:
        return "late"
    return "closeout"


def _count_soul(dragons: list) -> int:
    """Returns 1 if team has dragon soul (4+ elemental drakes), else 0.

    Elder dragon is separate and doesn't count toward soul threshold.
    """
    elementals = [d for d in dragons if d.lower() != "elder"]
    return 1 if len(elementals) >= 4 else 0


def _has_elder(dragons: list) -> int:
    return 1 if any(d.lower() == "elder" for d in dragons) else 0


def extract_frame_features(
    frame_row: dict,
    game_start_ts_unix: int,
) -> FrameFeatures:
    """Build features for one frame.

    Args:
        frame_row: a row from live_frames table (dict-like)
        game_start_ts_unix: when state first went to in_game (from games_live)

    Raises ValueError if the frame's raw_json can't be parsed or lacks
    participants (shouldn't happen for in_game frames from Riot).
    """
    raw = json.loads(frame_row["raw_json"])
    blue = raw.get("blueTeam", {}) or {}
    red = raw.get("redTeam", {}) or {}
    blue_parts = blue.get("participants", []) or []
    red_parts = red.get("participants", []) or []
    if len(blue_parts) != 5 or len(red_parts) != 5:
        raise ValueError(f"expected 5 participants per team, got {len(blue_parts)}/{len(red_parts)}")

    game_time = max(0, int(frame_row["frame_ts_unix"]) - game_start_ts_unix)
    minutes = max(game_time / 60, 1)  # avoid div-by-zero

    blue_drakes = blue.get("dragons", []) or []
    red_drakes = red.get("dragons", []) or []

    # Team-level diffs
    gold_diff = int(blue.get("totalGold", 0) or 0) - int(red.get("totalGold", 0) or 0)
    kills_diff = int(blue.get("totalKills", 0) or 0) - int(red.get("totalKills", 0) or 0)
    towers_diff = int(blue.get("towers", 0) or 0) - int(red.get("towers", 0) or 0)
    inh_diff = int(blue.get("inhibitors", 0) or 0) - int(red.get("inhibitors", 0) or 0)
    drakes_diff = len([d for d in blue_drakes if d.lower() != "elder"]) - \
                   len([d for d in red_drakes if d.lower() != "elder"])
    barons_diff = int(blue.get("barons", 0) or 0) - int(red.get("barons", 0) or 0)
    soul_diff = _count_soul(blue_drakes) - _count_soul(red_drakes)
    elder_diff = _has_elder(blue_drakes) - _has_elder(red_drakes)
    blue_cs = sum(int(p.get("creepScore", 0) or 0) for p in blue_parts)
    red_cs = sum(int(p.get("creepScore", 0) or 0) for p in red_parts)
    cs_diff = blue_cs - red_cs
    blue_hp = sum(int(p.get("currentHealth", 0) or 0) for p in blue_parts)
    red_hp = sum(int(p.get("currentHealth", 0) or 0) for p in red_parts)
    health_diff = blue_hp - red_hp

    # Per-role diffs
    role_gold = {}
    role_level = {}
    role_cs = {}
    role_kp = {}
    for i, r in enumerate(ROLES):
        b = blue_parts[i]
        d = red_parts[i]
        role_gold[r]  = int(b.get("totalGold", 0) or 0) - int(d.get("totalGold", 0) or 0)
        role_level[r] = int(b.get("level", 0) or 0) - int(d.get("level", 0) or 0)
        role_cs[r]    = int(b.get("creepScore", 0) or 0) - int(d.get("creepScore", 0) or 0)
        b_kp = int(b.get("kills", 0) or 0) + int(b.get("assists", 0) or 0) - int(b.get("deaths", 0) or 0)
        d_kp = int(d.get("kills", 0) or 0) + int(d.get("assists", 0) or 0) - int(d.get("deaths", 0) or 0)
        role_kp[r] = b_kp - d_kp

    return FrameFeatures(
        game_time_sec=game_time,
        game_phase=_phase(game_time),
        gold_diff=gold_diff,
        kills_diff=kills_diff,
        towers_diff=towers_diff,
        inhibitors_diff=inh_diff,
        dragons_diff=drakes_diff,
        barons_diff=barons_diff,
        soul_diff=soul_diff,
        elder_diff=elder_diff,
        cs_diff=cs_diff,
        health_diff=health_diff,
        role_gold_diff=role_gold,
        role_level_diff=role_level,
        role_cs_diff=role_cs,
        role_kp_diff={k: float(v) for k, v in role_kp.items()},
        gold_per_min_diff=gold_diff / minutes,
        cs_per_min_diff=cs_diff / minutes,
        kills_per_min_diff=kills_diff / minutes,
        gold_diff_x_time=gold_diff * (game_time / 600.0),  # normalized at 10min
    )
