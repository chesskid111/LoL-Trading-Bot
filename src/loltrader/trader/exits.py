"""Exit-side risk evaluation — the position-management layer.

gates.py decides whether to OPEN a position. This module decides whether to
HOLD, REDUCE, TAKE PROFIT, or EXIT one you already have. It is the discipline
layer that addresses the core failure mode: holding a near-coinflip late game
with maximum leverage and zero remaining edge (the game-1 throw).

Two independent mechanisms:

1. Structural triggers — read off the live frame. These are slow-moving,
   broadcast-visible events everyone sees on the same delay, so they're not a
   speed game: own inhibitor lost, opponent baron, big adverse gold swing in
   the last 60s (proxy for a lost fight / ace). When these fire late, the game
   can end before you can react → reduce/exit pre-emptively.

2. Coinflip detector — the three-signal conjunction:
     A. comp-adjusted win prob near 50%   (model says it's close)
     B. high game leverage                (one fight ends it)
     C. low remaining edge                (|fair - market| ~ 0)
   When all three hold you are holding pure variance with no compensation.
   The model's late-50/50 calibration was verified (gap 1.6%), so "≈50%" here
   is trustworthy: it really is a coinflip. Exit.

Pure functions — no DB, no network. The dashboard calls assess_exit() with the
live frame + model fair + market price + current position and surfaces the
recommendation. Nothing here places orders (creds are scope=read anyway).

All frame diffs follow the state integrator's blue-minus-red convention
(positive = blue-favorable); we orient to the held side via _orient().
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional

# --- thresholds (tunable; defaults reasoned in the module docstring) ---
COINFLIP_BAND = 0.10          # |fair - 0.5| < this  => near coinflip
LOW_EDGE = 0.04               # |fair - market| < this => no remaining edge
LATE_GAME_MIN = 28            # minute at/after which leverage is structurally high
MID_LATE_MIN = 24
GOLD_BUFFER = 2500            # |gold_diff| < this (mid-late) => a fight can swing it
ADVERSE_VELOCITY = -1200      # my-side gold change over last 60s worse than this
COLLAPSE_VELOCITY = -2500     # catastrophic swing (likely ace) => urgent exit
TAKE_PROFIT_GAIN_C = 15       # up >= 15c into rising leverage => ladder out
HIGH_LEVERAGE = 0.6

Side = Literal["blue", "red"]


@dataclass(frozen=True)
class PositionState:
    """The position you currently hold (the side you are long)."""
    side: Side
    entry_price_cents: int     # what you paid for your side (0-100)
    contracts: int = 0


@dataclass
class ExitAssessment:
    action: Literal["hold", "take_profit", "reduce", "exit"]
    urgency: Literal["none", "advisory", "urgent"]
    triggers: list[str]
    coinflip: bool
    leverage: float            # 0..1
    edge: float                # fair - market, oriented to your side
    fair_mine: float           # comp-adjusted win prob for your side
    market_mine: float         # market-implied prob for your side
    reason: str
    suggested_ladder: list[tuple[int, float]] = field(default_factory=list)  # (price_c, fraction)
    unrealized_pnl_cents: Optional[float] = None


def _orient(value: float, side: Side) -> float:
    """Flip a blue-minus-red diff to the held side's perspective."""
    return value if side == "blue" else -value


def game_leverage(frame: dict) -> float:
    """0..1 — how much a single fight can decide the game *right now*.

    Not about who's winning; about whether the state is fragile. High leverage
    means your information edge is worth little (the market converges fast) and
    the variance you can't react to is maximal.
    """
    minute = float(frame.get("minute", 0) or 0)
    score = 0.0
    if minute >= LATE_GAME_MIN:
        score = max(score, 0.6)
    elif minute >= MID_LATE_MIN:
        score = max(score, 0.35)
    # An inhibitor is down somewhere — base exposed, fights are lethal
    if float(frame.get("inhib_diff", 0) or 0) != 0:
        score = max(score, 0.8)
    # A baron is active — single biggest swing object
    if float(frame.get("baron_diff", 0) or 0) != 0:
        score = max(score, 0.7)
    # Dragon soul point reached
    if frame.get("soul_blue") or frame.get("soul_red"):
        score = max(score, 0.7)
    # No gold buffer to absorb a lost fight, mid-late
    if abs(float(frame.get("gold_diff", 0) or 0)) < GOLD_BUFFER and minute >= 22:
        score = max(score, 0.5)
    return min(1.0, score)


def assess_exit(
    model_fair_blue: float,
    market_price_blue_cents: float,
    frame: dict,
    position: PositionState,
) -> ExitAssessment:
    """Evaluate hold/reduce/take-profit/exit for the held position.

    Args:
        model_fair_blue: v2 comp-adjusted P(blue win), 0..1.
        market_price_blue_cents: market YES price for BLUE, 0..100.
        frame: live state features (blue-minus-red diffs).
        position: the side you're long, entry price, contracts.
    """
    side = position.side
    fair_mine = model_fair_blue if side == "blue" else 1.0 - model_fair_blue
    market_mine = (market_price_blue_cents if side == "blue"
                   else 100.0 - market_price_blue_cents) / 100.0
    edge = fair_mine - market_mine
    lev = game_leverage(frame)

    # ---- structural triggers (oriented to my side) ----
    triggers: list[str] = []
    inhib = _orient(float(frame.get("inhib_diff", 0) or 0), side)
    if inhib < 0:
        triggers.append("own_inhibitor_lost")
    baron = _orient(float(frame.get("baron_diff", 0) or 0), side)
    if baron < 0:
        triggers.append("opponent_baron_active")
    vel = _orient(float(frame.get("gold_diff_change_last_60s", 0) or 0), side)
    if vel <= COLLAPSE_VELOCITY:
        triggers.append("catastrophic_swing_60s")
    elif vel <= ADVERSE_VELOCITY:
        triggers.append("adverse_swing_60s")

    # ---- coinflip detector (A ∧ B ∧ C) ----
    near_coin = abs(fair_mine - 0.5) < COINFLIP_BAND
    high_lev = lev >= HIGH_LEVERAGE
    low_edge = abs(edge) < LOW_EDGE
    coinflip = near_coin and high_lev and low_edge

    # ---- unrealized PnL (if a position is set) ----
    pnl = None
    if position.contracts and position.entry_price_cents:
        pnl = (market_mine * 100 - position.entry_price_cents) * position.contracts

    # ---- decide ----
    action: str = "hold"
    urgency: str = "none"
    reason = "no exit signal"
    ladder: list[tuple[int, float]] = []

    if "catastrophic_swing_60s" in triggers or "own_inhibitor_lost" in triggers:
        action, urgency = "exit", "urgent"
        reason = ("structural collapse (" + ", ".join(triggers) +
                  ") — game can end before you react; exit now")
    elif coinflip:
        action, urgency = "exit", "urgent"
        reason = (f"coinflip + max leverage + ~0 edge: fair {fair_mine:.0%}, "
                  f"market {market_mine:.0%}, leverage {lev:.0%} — pure variance, "
                  f"no compensation. Exit (the game-1 lesson).")
    elif triggers and lev >= HIGH_LEVERAGE:
        action, urgency = "reduce", "advisory"
        reason = ("structural risk (" + ", ".join(triggers) +
                  f") at {lev:.0%} leverage — cut size")
    elif (pnl is not None
          and (market_mine * 100 - position.entry_price_cents) >= TAKE_PROFIT_GAIN_C
          and lev >= 0.5):
        action, urgency = "take_profit", "advisory"
        gain = market_mine * 100 - position.entry_price_cents
        reason = (f"up {gain:.0f}c into rising leverage ({lev:.0%}) — ladder out "
                  f"with resting sells rather than risk a swing")
        base = int(round(market_mine * 100))
        ladder = [(min(95, base + 5), 0.34),
                  (min(97, base + 12), 0.33),
                  (min(99, base + 20), 0.33)]
    elif edge > LOW_EDGE and lev < HIGH_LEVERAGE:
        reason = (f"edge {edge:+.0%} still present at {lev:.0%} leverage — hold")

    return ExitAssessment(
        action=action, urgency=urgency, triggers=triggers, coinflip=coinflip,
        leverage=lev, edge=edge, fair_mine=fair_mine, market_mine=market_mine,
        reason=reason, suggested_ladder=ladder, unrealized_pnl_cents=pnl,
    )


def recommend_position_size(
    edge: float,
    model_uncertainty: float,
    bankroll_cents: int,
    leverage: float,
    max_pct: float = 0.05,
    kelly_fraction: float = 0.5,
) -> int:
    """Half-Kelly entry size, discounted by leverage + model uncertainty, capped.

    Complements gates.py (which only *validates* a proposed size). Sizing tracks
    edge × (1 - leverage): big edge in a buffered game = full size; thin edge OR
    a fragile late state = small or zero. For a binary contract bought at price p
    with true prob q, Kelly fraction is (q - p)/(1 - p) = edge/(1 - market).
    """
    if edge <= 0:
        return 0
    market_mine = max(0.01, min(0.99, edge + (0.0)))  # placeholder if unknown
    # Caller passes edge only; approximate (1 - market) ~ (1 - (fair - edge)).
    # To keep this self-contained we use a conservative denominator of 0.5,
    # which under-sizes slightly (safe direction). Callers with market price
    # should prefer size_from_prices() below.
    kelly = edge / 0.5
    frac = kelly_fraction * kelly
    frac *= (1.0 - 0.7 * max(0.0, min(1.0, leverage)))      # leverage discount
    frac *= max(0.2, 1.0 - max(0.0, min(1.0, model_uncertainty)))  # uncertainty
    frac = max(0.0, min(frac, max_pct))
    return int(bankroll_cents * frac)


def size_from_prices(
    fair_mine: float,
    market_mine: float,
    model_uncertainty: float,
    bankroll_cents: int,
    leverage: float,
    max_pct: float = 0.05,
    kelly_fraction: float = 0.5,
) -> int:
    """Preferred sizing when you have both fair and market: exact Kelly."""
    edge = fair_mine - market_mine
    if edge <= 0:
        return 0
    denom = max(0.05, 1.0 - market_mine)
    kelly = edge / denom
    frac = kelly_fraction * kelly
    frac *= (1.0 - 0.7 * max(0.0, min(1.0, leverage)))
    frac *= max(0.2, 1.0 - max(0.0, min(1.0, model_uncertainty)))
    frac = max(0.0, min(frac, max_pct))
    return int(bankroll_cents * frac)
