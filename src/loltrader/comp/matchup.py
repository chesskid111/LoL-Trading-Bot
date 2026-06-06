"""Matchup evaluator — Layer 3 of the comp evaluation engine.

Combines two ``CompProfile`` objects (from Layer 2 aggregator) and per-role
lane matchup data into actionable assessments:

  lane_matchup(champ_a, champ_b, role)         → (shrunk_winrate, games)
  comp_matchup(comp_a, comp_b, minute)          → (favored, edge_magnitude)
  crossover_minute(comp_a, comp_b)              → minute the lead flips, or None

The comp_matchup output is the central input to the cycle-trade strategy:
edge_magnitude tells you how much team A is favored at a specific minute,
crossover_minute tells you when (if ever) the structural advantage flips.

This module is read-only on the matchup data file — refresh data is owned by
``loltrader.tools.refresh_matchups``.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from loltrader.comp.aggregator import CompProfile, ChampionPick, SCALING_MINUTES
from loltrader.comp.matchup_data import load_matchups, lookup_matchup

log = logging.getLogger(__name__)

# How much weight to give the lane matchup component vs the comp scaling
# component. The scaling curve is the bigger signal; lane matchups are an
# adjustment. Roughly: comp scaling carries 75%, lane matchups 25%.
LANE_MATCHUP_WEIGHT = 0.25
SCALING_WEIGHT = 0.75


@dataclass(frozen=True)
class MatchupAssessment:
    """One minute's comp-vs-comp evaluation."""
    minute: int
    favored: str                  # "A" / "B" / "EVEN"
    edge_magnitude: float         # signed scaling diff (positive = A favored)
    scaling_diff: float           # raw curve diff (A - B)
    lane_winrate_avg: float       # average across 5 lanes (>0.5 means A favored)


# ---------- lane matchup ------------------------------------------------


_MATCHUP_CACHE: dict[str, dict] | None = None


def _load_matchups_cached(path: str | Path = "data/lane_matchups.json") -> dict:
    """Cache the matchup JSON in-memory so we don't reread per call."""
    global _MATCHUP_CACHE
    if _MATCHUP_CACHE is None:
        _MATCHUP_CACHE = load_matchups(path)
    return _MATCHUP_CACHE


def reset_matchup_cache() -> None:
    """For tests or after a refresh — force the next call to re-read disk."""
    global _MATCHUP_CACHE
    _MATCHUP_CACHE = None


def lane_matchup(
    champ_a: str,
    champ_b: str,
    role: str,
    matchups_path: str | Path = "data/lane_matchups.json",
) -> tuple[float, int]:
    """Return (shrunk_winrate_a, games) for a specific lane matchup.

    Reads from ``data/lane_matchups.json`` (cached in-memory). When no
    matchup is found, returns the neutral (0.5, 0) — meaning "no evidence,
    assume even."
    """
    table = _load_matchups_cached(matchups_path)
    return lookup_matchup(table, role, champ_a, champ_b)


# ---------- comp vs comp ------------------------------------------------


def _avg_lane_winrate(
    picks_a: list[ChampionPick],
    picks_b: list[ChampionPick],
    matchups_path: str | Path = "data/lane_matchups.json",
) -> float:
    """Average team A's shrunk winrate across the 5 same-role pairings.

    When team_a's role-X doesn't have a counterpart in team_b (rare — happens
    when both teams have the same multi-role champion classified differently),
    that lane is skipped from the average.
    """
    role_to_b = {p.role: p for p in picks_b}
    rates: list[float] = []
    for pa in picks_a:
        pb = role_to_b.get(pa.role)
        if pb is None:
            continue
        wr, _ = lane_matchup(pa.champion, pb.champion, pa.role, matchups_path)
        rates.append(wr)
    return sum(rates) / len(rates) if rates else 0.5


def comp_matchup(
    comp_a: CompProfile,
    comp_b: CompProfile,
    minute: int,
    picks_a: list[ChampionPick] | None = None,
    picks_b: list[ChampionPick] | None = None,
    matchups_path: str | Path = "data/lane_matchups.json",
) -> MatchupAssessment:
    """Evaluate (favored, edge_magnitude) at a specific minute.

    Combines:
      - Scaling curve diff at this minute (75% weight)
      - Lane matchup average across the 5 pairings (25% weight)

    Picks lists are optional — if omitted, lane matchups are skipped and the
    assessment is scaling-only. Useful in tests / hypothetical scoring.

    Returns a MatchupAssessment with:
      - favored = "A" / "B" / "EVEN"
      - edge_magnitude = combined signed score (positive = A favored)
      - scaling_diff = raw curve diff
      - lane_winrate_avg = team A's shrunk winrate averaged across lanes
    """
    minute = max(SCALING_MINUTES[0], min(SCALING_MINUTES[-1], minute))
    scaling_diff = comp_a.scaling_curve[minute] - comp_b.scaling_curve[minute]

    lane_wr = 0.5
    if picks_a and picks_b:
        lane_wr = _avg_lane_winrate(picks_a, picks_b, matchups_path)

    # Lane winrate centered at 0.5 → translate to a +/- delta.
    # A 60% lane winrate ≈ +0.5 edge points; weighting matches the spec.
    lane_edge = (lane_wr - 0.5) * 5.0  # scale to comparable magnitude

    edge = scaling_diff * SCALING_WEIGHT + lane_edge * LANE_MATCHUP_WEIGHT

    if edge > 0.3:
        favored = "A"
    elif edge < -0.3:
        favored = "B"
    else:
        favored = "EVEN"

    return MatchupAssessment(
        minute=minute,
        favored=favored,
        edge_magnitude=edge,
        scaling_diff=scaling_diff,
        lane_winrate_avg=lane_wr,
    )


def crossover_minute(
    comp_a: CompProfile,
    comp_b: CompProfile,
    picks_a: list[ChampionPick] | None = None,
    picks_b: list[ChampionPick] | None = None,
    matchups_path: str | Path = "data/lane_matchups.json",
) -> int | None:
    """Find the minute at which the favored team flips (if it does).

    Returns the smallest minute t in [10, 40] where the favored side
    changes from the favored side at minute 10. Returns None if dominance
    never flips — i.e., one team is ahead throughout the game (a "stomp"
    matchup with no cycle to trade).

    The 10-minute floor avoids classifying the very-early-game noise as
    a meaningful crossover. In practice, the cycle trade plays out
    between minute 15 and minute 35.
    """
    initial = comp_matchup(comp_a, comp_b, 10, picks_a, picks_b, matchups_path)
    initial_side = initial.favored
    if initial_side == "EVEN":
        # Already even at min 10 — find when one side decisively pulls ahead.
        for t in range(11, 41):
            a = comp_matchup(comp_a, comp_b, t, picks_a, picks_b, matchups_path)
            if a.favored != "EVEN":
                return t
        return None

    # Initial side is A or B. Find the first minute where the side changes.
    for t in range(11, 41):
        a = comp_matchup(comp_a, comp_b, t, picks_a, picks_b, matchups_path)
        if a.favored != initial_side:
            # Crossover happened at this minute (or one of the prior ones).
            return t
    return None
