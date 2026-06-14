"""Generate a plain-English draft breakdown from two evaluated comps.

Turns the comp engine's numbers (scaling curves, archetypes, dimension scores)
into the kind of read you'd get in chat: who's favored at draft, when each
comp peaks, and the key dynamics — so the dashboard can show the bot's
reasoning, not just a win-prob number.

Pure function of two CompProfiles (+ optional team codes). No DB/network.
Everything is derived from comp DIMENSIONS, not champion-specific lore, so it
stays honest and data-driven.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from loltrader.comp.aggregator import CompProfile, ChampionPick


def _crossover_minute(blue: CompProfile, red: CompProfile) -> Optional[int]:
    """First minute (5-40) where the scaling lead flips sides, if any."""
    prev_sign = None
    for m in range(5, 41):
        d = blue.scaling_at(m) - red.scaling_at(m)
        sign = 1 if d > 0 else (-1 if d < 0 else 0)
        if sign == 0:
            continue
        if prev_sign is not None and sign != prev_sign:
            return m
        prev_sign = sign
    return None


def _favored(blue: CompProfile, red: CompProfile) -> tuple[str, float]:
    """Transparent draft-only lean from comp dimensions. Returns (side, prob).

    NOT the model's win-prob — a coarse composite of late scaling, teamfight,
    engage-vs-disengage, pick and baron control. Labeled as draft-only upstream.
    """
    late = blue.scaling_at(32) - red.scaling_at(32)
    tf = blue.teamfight_score - red.teamfight_score
    engage = (blue.engage_score - red.disengage_score) - (red.engage_score - blue.disengage_score)
    pick = blue.pick_threat - red.pick_threat
    baron = blue.baron_dps_total - red.baron_dps_total
    # Weights: scaling + teamfight dominate, engage/pick/baron secondary
    score = 0.35 * late + 0.30 * tf + 0.18 * engage + 0.10 * pick + 0.07 * baron
    prob = 1.0 / (1.0 + math.exp(-score / 4.0))   # logistic; /4 keeps it gentle
    side = "blue" if prob >= 0.5 else "red"
    return side, prob


def _dynamics(blue: CompProfile, red: CompProfile,
              bt: str, rt: str) -> list[str]:
    """Ranked list of the most significant draft dynamics (top few)."""
    cand: list[tuple[float, str]] = []

    # Scaling timeline
    late_gap = blue.scaling_at(32) - red.scaling_at(32)
    if abs(late_gap) >= 2:
        win, lose = (bt, rt) if late_gap > 0 else (rt, bt)
        cand.append((abs(late_gap) * 0.9,
                     f"{win} scales harder into the late game — {lose} wants to close early."))

    early_gap = blue.scaling_at(10) - red.scaling_at(10)
    if abs(early_gap) >= 2:
        win = bt if early_gap > 0 else rt
        cand.append((abs(early_gap) * 0.8,
                     f"{win} is stronger early and should look to snowball a lead."))

    # Engage vs disengage (forced-fight vulnerability)
    b_force = blue.engage_score - red.disengage_score
    r_force = red.engage_score - blue.disengage_score
    if b_force - r_force >= 3:
        cand.append((b_force - r_force,
                     f"{bt} has more engage than {rt} can disengage — {rt} is vulnerable to forced fights."))
    elif r_force - b_force >= 3:
        cand.append((r_force - b_force,
                     f"{rt} has more engage than {bt} can disengage — {bt} is vulnerable to forced fights."))

    # Pick potential
    pick_gap = blue.pick_threat - red.pick_threat
    if abs(pick_gap) >= 3:
        win = bt if pick_gap > 0 else rt
        cand.append((abs(pick_gap) * 0.7,
                     f"{win} can pick off isolated targets — punishes poor positioning."))

    # Teamfight
    tf_gap = blue.teamfight_score - red.teamfight_score
    if abs(tf_gap) >= 3:
        win = bt if tf_gap > 0 else rt
        cand.append((abs(tf_gap) * 0.8,
                     f"{win} is the stronger 5v5 teamfighting comp."))

    # Siege / baron control
    baron_gap = blue.baron_dps_total - red.baron_dps_total
    if abs(baron_gap) >= 4:
        win = bt if baron_gap > 0 else rt
        cand.append((abs(baron_gap) * 0.5,
                     f"{win} melts objectives faster — favored on Baron/Dragon contests."))

    # Peel mismatch (does a carry-heavy comp lack protection?)
    if blue.peel_demand_total - blue.peel_supply_total >= 4:
        cand.append((blue.peel_demand_total - blue.peel_supply_total,
                     f"{bt}'s carries need more peel than the comp provides — exposed if dove."))
    if red.peel_demand_total - red.peel_supply_total >= 4:
        cand.append((red.peel_demand_total - red.peel_supply_total,
                     f"{rt}'s carries need more peel than the comp provides — exposed if dove."))

    cand.sort(key=lambda x: -x[0])
    return [t for _, t in cand[:3]]


@dataclass
class DraftSide:
    team: Optional[str]
    archetype: str
    picks: list[dict]
    scaling_early: float
    scaling_mid: float
    scaling_late: float
    teamfight: float
    engage: float
    disengage: float
    pick_threat: float
    baron_dps: float
    synergies: list[str]
    win_condition: str
    confidence: float


def _side(comp: CompProfile, picks: list[ChampionPick], team: Optional[str]) -> DraftSide:
    return DraftSide(
        team=team,
        archetype=comp.archetype,
        picks=[{"champion": p.champion, "role": p.role} for p in picks],
        scaling_early=round(comp.scaling_at(10), 1),
        scaling_mid=round(comp.scaling_at(20), 1),
        scaling_late=round(comp.scaling_at(32), 1),
        teamfight=round(comp.teamfight_score, 1),
        engage=round(comp.engage_score, 1),
        disengage=round(comp.disengage_score, 1),
        pick_threat=round(comp.pick_threat, 1),
        baron_dps=round(comp.baron_dps_total, 1),
        synergies=list(comp.synergy_bonuses),
        win_condition=comp.win_condition or "",
        confidence=round(comp.confidence, 2),
    )


def build_draft_read(
    blue: CompProfile, red: CompProfile,
    blue_picks: list[ChampionPick], red_picks: list[ChampionPick],
    blue_team: Optional[str] = None, red_team: Optional[str] = None,
) -> dict:
    """Full structured draft breakdown for one game (wire-ready dict)."""
    bt = blue_team or "Blue"
    rt = red_team or "Red"

    side, prob = _favored(blue, red)
    crossover = _crossover_minute(blue, red)
    dynamics = _dynamics(blue, red, bt, rt)

    # Headline
    favored_team = bt if side == "blue" else rt
    lean_pct = prob if side == "blue" else (1 - prob)
    strength = ("slightly" if lean_pct < 0.56 else
                "clearly" if lean_pct < 0.64 else "heavily")
    headline = f"Draft leans {strength} {favored_team} ({lean_pct*100:.0f}% on comp alone)"
    if crossover:
        late_team = bt if blue.scaling_at(40) > red.scaling_at(40) else rt
        headline += f" — {late_team} takes over around min {crossover}"

    return {
        "blue": _side(blue, blue_picks, blue_team),
        "red": _side(red, red_picks, red_team),
        "favored_side": side,
        "favored_team": favored_team,
        "draft_lean_pct": round(lean_pct, 3),
        "scaling_crossover_min": crossover,
        "headline": headline,
        "dynamics": dynamics,
        "disclaimer": ("Comp/draft only — does not include team strength or form. "
                       "Use the live win-prob once the game starts."),
    }
