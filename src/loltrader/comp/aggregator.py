"""Comp aggregator — Layer 2 of the comp evaluation engine.

Given 5 champion picks (with roles, optionally with player names), produce a
``CompProfile`` describing the team's strength curve over time, baron DPS,
peel supply/demand, archetype, and synergy bonuses.

The scaling curve is the centerpiece: at every minute t in 0-40, we evaluate
the comp's composite scaling score. This is what enables the live win-prob
model to know "this comp peaks at minute 22" or "this comp is still scaling
at minute 30."

Synergies are loaded from ``data/synergies.json`` — a hand-curated table of
specific named pairings (Lulu+Lucian, Maokai+Sett etc.) that get explicit
bonuses on top of the simple sum.

Player×champion comfort overrides are computed lazily from the local DB
(``match_player_stats``) — when a known carry is on a comfort pick (Faker on
Azir, ≥10 games, ≥60% winrate), we boost teamfight_score and wave_clear.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from loltrader.comp.profiles import ChampionProfile, load_profiles
from loltrader.features.draft import ARCHETYPES, classify_archetype

# Scaling curve sample range. We evaluate at 1-minute intervals from 0 to 40.
# Outside this range we extrapolate as flat (LoL games rarely exceed 40 min).
SCALING_MINUTES = list(range(0, 41))

# Anchor points for the scaling interpolation. Maps champion's qualitative
# scaling_early/mid/late tiers to specific in-game minutes. The curve through
# these anchors is what we evaluate.
SCALING_ANCHORS = {
    "scaling_early": 7,    # peak of "early game" — minute 7 (~end of lane)
    "scaling_mid": 20,     # peak of "mid game" — minute 20 (objective focus)
    "scaling_late": 32,    # peak of "late game" — minute 32 (5v5 teamfights)
}

# Player comfort thresholds: must have at least N pro games on the champion
# AND winrate at least X for the comfort bonus to apply.
COMFORT_MIN_GAMES = 10
COMFORT_MIN_WINRATE = 0.60


@dataclass(frozen=True)
class ChampionPick:
    """One champion pick on a team."""
    champion: str
    role: str                 # "top" / "jungle" / "mid" / "bot" / "support"
    player: str | None = None # optional pro player name for comfort lookup


@dataclass
class CompProfile:
    """Layer 2 output — everything we know about how a comp performs."""
    # Time-varying: scaling score at each minute
    scaling_curve: dict[int, float]

    # Aggregate qualitative dimensions (sums + diffs over 5 picks)
    baron_dps_total: float
    peel_supply_total: float
    peel_demand_total: float
    split_push_threat: float
    pick_threat: float
    teamfight_score: float
    engage_score: float
    disengage_score: float
    wave_clear: float
    ult_impact: float

    # Categorical
    archetype: Literal["scaling", "teamfight", "pick", "balanced"]

    # Names of specific synergies that fired
    synergy_bonuses: list[str] = field(default_factory=list)

    # Player×champion comfort overrides that applied
    comfort_overrides: list[str] = field(default_factory=list)

    # Plain-English description (built from archetype + key picks)
    win_condition: str = ""

    # 0-1 confidence proxy: lower if any pick has low confidence
    confidence: float = 0.0

    def scaling_at(self, minute: int) -> float:
        """Return the comp's scaling score at the given minute."""
        if minute <= SCALING_MINUTES[0]:
            return self.scaling_curve[SCALING_MINUTES[0]]
        if minute >= SCALING_MINUTES[-1]:
            return self.scaling_curve[SCALING_MINUTES[-1]]
        return self.scaling_curve[minute]


# ---------- scaling curve helpers ---------------------------------------


def _interp_scaling(profile: ChampionProfile, minute: int) -> float:
    """Linearly interpolate one champion's scaling at minute t.

    The qualitative dimensions give us 3 anchor points (scaling_early at min 7,
    scaling_mid at min 20, scaling_late at min 32). Between anchors we linearly
    interpolate; outside the outer anchors we extrapolate flat.
    """
    e_min, m_min, l_min = (SCALING_ANCHORS["scaling_early"],
                           SCALING_ANCHORS["scaling_mid"],
                           SCALING_ANCHORS["scaling_late"])
    e, m, l = (float(profile.qualitative.scaling_early),
               float(profile.qualitative.scaling_mid),
               float(profile.qualitative.scaling_late))
    if minute <= e_min:
        return e
    if minute <= m_min:
        # Linear from (e_min, e) to (m_min, m)
        return e + (m - e) * (minute - e_min) / (m_min - e_min)
    if minute <= l_min:
        return m + (l - m) * (minute - m_min) / (l_min - m_min)
    return l


def _team_scaling_curve(profiles: list[ChampionProfile]) -> dict[int, float]:
    """Sum each minute's scaling score across all 5 champions."""
    return {
        t: sum(_interp_scaling(p, t) for p in profiles)
        for t in SCALING_MINUTES
    }


# ---------- synergy lookup ----------------------------------------------


_SYNERGY_CACHE: dict[str, dict[str, dict[str, float]]] | None = None


def _load_synergies(path: str | Path = "data/synergies.json") -> dict:
    """Synergy table:
        {"ChampA|ChampB": {"teamfight_score": +1, "scaling_late": +1}, ...}

    Keys are sorted-alphabetically champion pair strings joined by "|" so we
    don't need to think about order. Returns empty dict if file is missing.
    """
    global _SYNERGY_CACHE
    if _SYNERGY_CACHE is not None:
        return _SYNERGY_CACHE
    p = Path(path)
    if not p.exists():
        _SYNERGY_CACHE = {}
        return _SYNERGY_CACHE
    raw = json.loads(p.read_text(encoding="utf-8"))
    _SYNERGY_CACHE = raw
    return raw


def _pair_key(a: str, b: str) -> str:
    return "|".join(sorted([a, b]))


def _apply_synergies(
    picks: list[ChampionPick],
) -> tuple[list[str], dict[str, float]]:
    """Look up every pair against the synergy table; return (names, deltas)."""
    table = _load_synergies()
    fired: list[str] = []
    deltas: dict[str, float] = {}
    for i in range(len(picks)):
        for j in range(i + 1, len(picks)):
            key = _pair_key(picks[i].champion, picks[j].champion)
            if key in table:
                fired.append(key)
                for dim, val in table[key].items():
                    deltas[dim] = deltas.get(dim, 0.0) + float(val)
    return fired, deltas


# ---------- player×champion comfort overrides ---------------------------


def _player_comfort_overrides(
    conn: sqlite3.Connection,
    picks: list[ChampionPick],
) -> tuple[list[str], dict[str, float]]:
    """Apply comfort bonuses for known carries on their best picks.

    Looks up each (player, champion) pair in match_player_stats. If the player
    has at least COMFORT_MIN_GAMES games on the champion AND a winrate of at
    least COMFORT_MIN_WINRATE, apply a small bonus to teamfight_score and
    wave_clear (the dimensions most reflective of mechanical execution).
    """
    fired: list[str] = []
    deltas: dict[str, float] = {}

    for pick in picks:
        if not pick.player:
            continue
        row = conn.execute(
            """
            SELECT COUNT(*) AS games,
                   SUM(CASE WHEN mp.team_id = g.winner_team_id THEN 1 ELSE 0 END) AS wins
            FROM match_player_stats mp
            JOIN match_games g ON g.game_id = mp.game_id
            JOIN players p ON p.player_id = mp.player_id
            WHERE LOWER(p.ign) = LOWER(?) AND mp.champion = ?
            """,
            (pick.player, pick.champion),
        ).fetchone()
        if not row or not row["games"]:
            continue
        games = int(row["games"])
        winrate = (row["wins"] or 0) / games if games else 0.0
        if games >= COMFORT_MIN_GAMES and winrate >= COMFORT_MIN_WINRATE:
            fired.append(f"{pick.player}+{pick.champion} ({games}g, {winrate*100:.0f}%wr)")
            deltas["teamfight_score"] = deltas.get("teamfight_score", 0.0) + 0.5
            deltas["wave_clear"] = deltas.get("wave_clear", 0.0) + 0.5

    return fired, deltas


# ---------- archetype + win condition -----------------------------------


def _archetype_from_picks(profiles: list[ChampionProfile]) -> str:
    """Reuse the existing draft archetype classifier with our profile data.

    The existing classifier wants tag counts (has_mage, has_marksman, etc.).
    We don't store those on ChampionProfile directly, so we proxy via roles +
    qualitative dimensions. A champion with high pick_threat acts assassin-like;
    high scaling_late with high baron_dps acts marksman-like; etc.

    This is heuristic but matches the spec which says "reuse the existing
    archetype classifier." For v1 we accept the approximation.
    """
    tags = {
        "has_mage": 0, "has_marksman": 0, "has_assassin": 0,
        "has_fighter": 0, "has_tank": 0, "has_support": 0,
    }
    for p in profiles:
        role = p.qualitative.primary_role
        if role == "support":
            tags["has_support"] += 1
        elif role == "bot":
            tags["has_marksman"] += 1
        # Use qualitative dimensions as proxy for the role within top/mid/jng
        if p.qualitative.pick_threat >= 2:
            tags["has_assassin"] += 1
        if p.qualitative.peel_supply >= 2 and p.qualitative.engage_score == 0:
            # enchanter/utility — count toward mage for archetype purposes
            tags["has_mage"] += 1
        if p.qualitative.teamfight_score >= 2 and p.qualitative.peel_supply >= 1:
            tags["has_tank"] += 1
        if p.qualitative.engage_score >= 2 or p.qualitative.split_push_threat >= 2:
            tags["has_fighter"] += 1
    onehot = classify_archetype(tags)
    for name in ARCHETYPES:
        if onehot.get(name) == 1:
            return name
    return "balanced"


def _infer_win_condition(profiles: list[ChampionProfile], archetype: str,
                          picks: list[ChampionPick]) -> str:
    """Produce a one-line human-readable win condition string."""
    # Identify the strongest individual scaling threat
    late_carry = max(profiles, key=lambda p: p.qualitative.scaling_late)
    early_threat = max(profiles, key=lambda p: p.qualitative.scaling_early)

    if archetype == "scaling":
        return (f"scale to 30+ min around {late_carry.name} "
                f"(scaling_late={late_carry.qualitative.scaling_late}); "
                "control objectives, avoid early skirmishes")
    if archetype == "pick":
        return (f"snowball early picks, end before scaling carries come online "
                f"(carry={early_threat.name})")
    if archetype == "teamfight":
        return (f"force 5v5 at objectives 20-30 min; teamfight DPS via "
                f"{late_carry.name} + frontline")
    return f"adapt to game state; win condition fluid (carry candidate: {late_carry.name})"


# ---------- public entry ------------------------------------------------


def evaluate_comp(
    picks: list[ChampionPick],
    patch: str = "",
    conn: sqlite3.Connection | None = None,
    profiles_path: str | Path = "data/champion_profiles.json",
) -> CompProfile:
    """Evaluate a 5-champion team comp into a CompProfile.

    Args:
        picks: List of 5 ChampionPick objects (champion + role + optional player).
        patch: Optional patch label for logging; not used in scoring.
        conn: Optional DB connection for player comfort lookups. If None,
            comfort overrides are skipped.
        profiles_path: Path to ``champion_profiles.json``.

    Raises:
        KeyError if any champion is not in the profiles file. Callers should
        catch this and treat the comp as unknown.
    """
    all_profiles = load_profiles(profiles_path)
    profiles: list[ChampionProfile] = []
    missing: list[str] = []
    for pick in picks:
        if pick.champion in all_profiles:
            profiles.append(all_profiles[pick.champion])
        else:
            missing.append(pick.champion)
    if missing:
        raise KeyError(
            f"Champion(s) not in profiles: {missing}. "
            "Run Phase 1.4 bootstrap or add manually."
        )

    # Sum across picks for aggregate dimensions
    def _sum(attr: str) -> float:
        return sum(getattr(p.qualitative, attr) for p in profiles)

    baron_dps = _sum("baron_dps_tier")
    peel_supply = _sum("peel_supply")
    peel_demand = _sum("peel_needs")
    split = _sum("split_push_threat")
    pick = _sum("pick_threat")
    teamfight = _sum("teamfight_score")
    engage = _sum("engage_score")
    disengage = _sum("disengage_score")
    wave = _sum("wave_clear")
    ult = _sum("ult_impact")

    # Time-varying scaling curve
    curve = _team_scaling_curve(profiles)

    # Synergy bonuses
    synergy_names, synergy_deltas = _apply_synergies(picks)
    for dim, val in synergy_deltas.items():
        if dim == "teamfight_score":
            teamfight += val
        elif dim == "engage_score":
            engage += val
        elif dim == "disengage_score":
            disengage += val
        elif dim == "peel_supply":
            peel_supply += val
        elif dim == "pick_threat":
            pick += val
        elif dim == "split_push_threat":
            split += val
        elif dim == "wave_clear":
            wave += val
        elif dim == "ult_impact":
            ult += val
        elif dim in ("scaling_early", "scaling_mid", "scaling_late"):
            # Apply to the curve as a flat shift weighted by anchor proximity
            anchor = SCALING_ANCHORS[dim]
            for t in SCALING_MINUTES:
                # Triangular weighting: max effect at anchor, falls off
                weight = max(0.0, 1.0 - abs(t - anchor) / 12.0)
                curve[t] += val * weight

    # Player×champion comfort overrides
    comfort_names: list[str] = []
    if conn is not None:
        comfort_names, comfort_deltas = _player_comfort_overrides(conn, picks)
        teamfight += comfort_deltas.get("teamfight_score", 0.0)
        wave += comfort_deltas.get("wave_clear", 0.0)

    archetype = _archetype_from_picks(profiles)
    win_condition = _infer_win_condition(profiles, archetype, picks)

    # Confidence is the minimum confidence of any pick — a comp is only as
    # trustworthy as its weakest curated profile.
    confidence = min(p.confidence for p in profiles) if profiles else 0.0

    return CompProfile(
        scaling_curve=curve,
        baron_dps_total=baron_dps,
        peel_supply_total=peel_supply,
        peel_demand_total=peel_demand,
        split_push_threat=split,
        pick_threat=pick,
        teamfight_score=teamfight,
        engage_score=engage,
        disengage_score=disengage,
        wave_clear=wave,
        ult_impact=ult,
        archetype=archetype,  # type: ignore[arg-type]
        synergy_bonuses=synergy_names,
        comfort_overrides=comfort_names,
        win_condition=win_condition,
        confidence=confidence,
    )
