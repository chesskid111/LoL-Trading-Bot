"""Pydantic schemas for external data validation.

Used by the importers to verify human-extracted data is well-formed before
it flows into model features.
"""
from __future__ import annotations

from datetime import date
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator


# ---------- gol.gg ------------------------------------------------------


class GolGGSynergyRow(BaseModel):
    """One row from the gol.gg Champion Synergy table after copy-to-clipboard.

    Source columns: CHAMPION 1, CHAMPION 2, # GAMES, WINRATE, DUO GD@15, DUO CSD@15

    gol.gg appends the role to each champion name ('Pantheon JUNGLE'), which we
    parse + preserve — off-meta variants (Sett support vs Sett top) have
    completely different play patterns and shouldn't be merged.
    """
    champion_1: str
    role_1: Optional[Literal["top", "jungle", "mid", "bot", "support"]] = None
    champion_2: str
    role_2: Optional[Literal["top", "jungle", "mid", "bot", "support"]] = None
    n_games: int = Field(..., ge=1)
    winrate: float = Field(..., ge=0.0, le=1.0)
    duo_gd_15: float          # raw value (can be negative)
    duo_csd_15: float         # raw CS differential

    @field_validator("winrate", mode="before")
    @classmethod
    def normalize_winrate(cls, v):
        """Accept either 81.0 or 0.81 — normalize to 0.0-1.0 range."""
        v = float(v)
        if v > 1.0:
            return v / 100.0
        return v


class GolGGTripleRow(BaseModel):
    """One row from gol.gg's 3-champion synergy table.

    Source: Number of champions = 3 in the Champion synergy filter.
    Captures wombo trios like Sion+Annie+Rell that pair data misses.
    """
    champion_1: str
    role_1: Optional[Literal["top", "jungle", "mid", "bot", "support"]] = None
    champion_2: str
    role_2: Optional[Literal["top", "jungle", "mid", "bot", "support"]] = None
    champion_3: str
    role_3: Optional[Literal["top", "jungle", "mid", "bot", "support"]] = None
    n_games: int = Field(..., ge=1)
    winrate: float = Field(..., ge=0.0, le=1.0)
    duo_gd_15: float
    duo_csd_15: float

    @field_validator("winrate", mode="before")
    @classmethod
    def normalize_winrate(cls, v):
        v = float(v)
        return v / 100.0 if v > 1.0 else v


class ExpandedTriple(BaseModel):
    """Single entry in data/processed/triples_expanded.json.

    Triple key: 'Champ1:role1|Champ2:role2|Champ3:role3' (sorted).
    Boosts apply ONLY when ALL 3 champions appear in the comp (with roles).
    """
    triple_key: str
    champion_1: str
    role_1: Optional[Literal["top", "jungle", "mid", "bot", "support"]] = None
    champion_2: str
    role_2: Optional[Literal["top", "jungle", "mid", "bot", "support"]] = None
    champion_3: str
    role_3: Optional[Literal["top", "jungle", "mid", "bot", "support"]] = None
    n_games_total: int
    winrate: float = Field(..., ge=0.0, le=1.0)
    avg_duo_gd_15: float
    avg_duo_csd_15: float
    synergy_type: Literal["early_game", "mid_game", "late_game", "neutral"]

    scaling_early_boost: float = 0.0
    scaling_mid_boost: float = 0.0
    scaling_late_boost: float = 0.0
    teamfight_boost: float = 0.0
    engage_boost: float = 0.0
    pick_threat_boost: float = 0.0

    source: str = "gol.gg"
    imported_at: str


class GolGGChampionStatRow(BaseModel):
    """One row from gol.gg Champions ranking table.

    The exact column set varies — this is the common subset we expect.
    """
    champion: str
    role: Literal["top", "jungle", "mid", "bot", "support"]
    n_games: int = Field(..., ge=1)
    winrate: float = Field(..., ge=0.0, le=1.0)
    pickrate: Optional[float] = Field(None, ge=0.0, le=1.0)
    banrate: Optional[float] = Field(None, ge=0.0, le=1.0)
    kda: Optional[float] = Field(None, ge=0.0)
    gold_at_15: Optional[float] = None
    cs_at_15: Optional[float] = None
    dmg_per_min: Optional[float] = Field(None, ge=0.0)

    @field_validator("winrate", "pickrate", "banrate", mode="before")
    @classmethod
    def normalize_pct(cls, v):
        if v is None:
            return v
        v = float(v)
        return v / 100.0 if v > 1.0 else v

    @field_validator("role", mode="before")
    @classmethod
    def normalize_role(cls, v):
        if v is None:
            return v
        return str(v).lower().strip()


# ---------- DPM ---------------------------------------------------------


class DPMTeamStats(BaseModel):
    """Team-level head-to-head stats from DPM premium."""
    team_code: str               # "T1", "HLE", etc.
    team_full: str               # "Hanwha Life Esports"
    season: int                  # 2026
    split: str                   # "LCK_Rounds_1-2"
    n_games: int = Field(..., ge=1)
    winrate_overall: float = Field(..., ge=0.0, le=1.0)

    # First-to-objective rates
    first_blood_pct: Optional[float] = None
    first_tower_pct: Optional[float] = None
    first_dragon_pct: Optional[float] = None
    first_baron_pct: Optional[float] = None
    first_herald_pct: Optional[float] = None
    first_horde_pct: Optional[float] = None
    first_inhibitor_pct: Optional[float] = None

    # Game duration buckets (% of games won given game reaches this minute)
    winrate_26plus_min: Optional[float] = None
    winrate_28plus_min: Optional[float] = None
    winrate_30plus_min: Optional[float] = None
    winrate_32plus_min: Optional[float] = None
    winrate_34plus_min: Optional[float] = None
    winrate_36plus_min: Optional[float] = None
    winrate_38plus_min: Optional[float] = None
    winrate_40plus_min: Optional[float] = None

    # First-to-kills (aggression profile)
    first_to_5_kills: Optional[float] = None
    first_to_10_kills: Optional[float] = None
    first_to_15_kills: Optional[float] = None
    first_to_20_kills: Optional[float] = None

    # Win-first-game-of-bo
    win_first_game_of_bo: Optional[float] = None

    extracted_at: str            # ISO date when this snapshot was taken
    source: str = "DPM Premium"

    @field_validator("*", mode="before")
    @classmethod
    def normalize_pcts(cls, v):
        if isinstance(v, (int, float)) and 1.0 < v <= 100.0:
            return v / 100.0
        return v


class DPMChampionPoolEntry(BaseModel):
    champion: str
    games: int = Field(..., ge=1)
    winrate: Optional[float] = None


class DPMPlayerStats(BaseModel):
    """Player-level stats from DPM premium."""
    handle: str                  # "Faker"
    full_name: Optional[str] = None
    team_code: str               # "T1"
    role: Literal["top", "jungle", "mid", "bot", "support"]
    season: int
    split: str
    n_games: int = Field(..., ge=1)
    winrate: float = Field(..., ge=0.0, le=1.0)

    # General stats
    kda: float = Field(..., ge=0.0)
    kp_pct: Optional[float] = None       # kill participation %
    dmg_per_min: Optional[float] = None
    cs_per_min: Optional[float] = None
    gold_per_min: Optional[float] = None

    # Lane phase
    first_blood_pct: Optional[float] = None
    cs_diff_15: Optional[float] = None
    gold_diff_15: Optional[float] = None
    xp_diff_15: Optional[float] = None
    first_to_level_2: Optional[float] = None
    first_to_level_6: Optional[float] = None

    # Champion pool
    champion_pool: list[DPMChampionPoolEntry] = Field(default_factory=list)

    extracted_at: str
    source: str = "DPM Premium"

    @field_validator("winrate", "kp_pct", "first_blood_pct",
                       "first_to_level_2", "first_to_level_6", mode="before")
    @classmethod
    def normalize_pct(cls, v):
        if v is None:
            return v
        v = float(v)
        return v / 100.0 if v > 1.0 else v


# ---------- Processed (aggregated) --------------------------------------


class ExpandedSynergy(BaseModel):
    """Single entry in data/processed/synergies_expanded.json.

    pair_key includes both champion AND role so off-meta picks
    (Sett support vs Sett top) stay distinct.
    Format: "Champ1:role1|Champ2:role2" (sorted alphabetically).
    """
    pair_key: str                # "Caitlyn:bot|LeeSin:jungle"
    champion_1: str
    role_1: Optional[Literal["top", "jungle", "mid", "bot", "support"]] = None
    champion_2: str
    role_2: Optional[Literal["top", "jungle", "mid", "bot", "support"]] = None
    n_games_total: int
    winrate: float = Field(..., ge=0.0, le=1.0)
    avg_duo_gd_15: float
    avg_duo_csd_15: float
    synergy_type: Literal["early_game", "mid_game", "late_game", "neutral"]

    # Boost values that get added to team's comp features
    scaling_early_boost: float = 0.0
    scaling_mid_boost: float = 0.0
    scaling_late_boost: float = 0.0
    teamfight_boost: float = 0.0
    engage_boost: float = 0.0
    pick_threat_boost: float = 0.0

    source: str = "gol.gg"
    imported_at: str


class TeamStrength(BaseModel):
    """Single team entry in data/processed/team_strength.json."""
    team_code: str
    team_full: str
    league: str

    # Aggregate strength signals
    recent_winrate: float
    early_game_index: float          # composite of first-to-X kills, first blood %
    mid_game_index: float            # composite of first tower, first baron %
    late_game_index: float           # composite of win % past 30/32/34 min
    objective_control_index: float   # composite of all first-objective rates

    # Raw inputs (for traceability)
    n_games: int
    winrate_30plus_min: Optional[float] = None
    winrate_34plus_min: Optional[float] = None
    first_baron_pct: Optional[float] = None
    first_tower_pct: Optional[float] = None

    last_updated: str
