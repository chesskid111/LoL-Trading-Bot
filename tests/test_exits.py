"""Tests for the exit-side risk evaluator (loltrader.trader.exits)."""
from __future__ import annotations

from loltrader.trader.exits import (
    PositionState, assess_exit, game_leverage, size_from_prices,
)


def _frame(**kw):
    """Minimal live frame with sane defaults; override via kwargs."""
    base = {
        "minute": 15, "gold_diff": 0, "kill_diff": 0, "inhib_diff": 0,
        "baron_diff": 0, "soul_blue": 0, "soul_red": 0,
        "gold_diff_change_last_60s": 0,
    }
    base.update(kw)
    return base


# ---------- leverage ----------

def test_leverage_low_early_buffered():
    # min 15, even-ish but with a buffer, nothing structural
    assert game_leverage(_frame(minute=15, gold_diff=3000)) < 0.5


def test_leverage_high_when_inhib_down():
    assert game_leverage(_frame(minute=30, inhib_diff=-1)) >= 0.8


def test_leverage_high_late_even_game():
    # 32 min, even gold (no buffer) — the dangerous coinflip zone
    assert game_leverage(_frame(minute=32, gold_diff=200)) >= 0.5


def test_leverage_high_with_soul():
    assert game_leverage(_frame(minute=29, soul_blue=1)) >= 0.7


# ---------- THE game-1 scenario ----------

def test_game1_coinflip_flags_exit():
    """70k-70k ocean soul, ~35 min, model ~50%, market ~52c, holding blue.

    This is the exact situation that lost $200: late, even, soul point,
    near-coinflip, no edge. Must flag URGENT EXIT.
    """
    frame = _frame(minute=35, gold_diff=300, soul_blue=1,
                   gold_diff_change_last_60s=100)
    pos = PositionState(side="blue", entry_price_cents=44, contracts=100)
    a = assess_exit(model_fair_blue=0.51, market_price_blue_cents=52,
                    frame=frame, position=pos)
    assert a.coinflip is True
    assert a.action == "exit"
    assert a.urgency == "urgent"


def test_scaling_comp_even_late_is_NOT_coinflip():
    """A favored scaling comp at even gold late is NOT a coinflip — the
    comp-adjusted fair reads ~58%, so the detector must NOT fire exit."""
    frame = _frame(minute=33, gold_diff=200, soul_blue=1)
    pos = PositionState(side="blue", entry_price_cents=50, contracts=100)
    # model says 58% (scaling comp), market lagging at 52
    a = assess_exit(model_fair_blue=0.58, market_price_blue_cents=52,
                    frame=frame, position=pos)
    assert a.coinflip is False
    assert a.action != "exit"   # we have edge AND we're favored — hold


# ---------- structural triggers ----------

def test_own_inhibitor_lost_urgent_exit_blue():
    frame = _frame(minute=30, inhib_diff=-1)  # blue down an inhib
    pos = PositionState(side="blue", entry_price_cents=60, contracts=50)
    a = assess_exit(0.55, 58, frame, pos)
    assert "own_inhibitor_lost" in a.triggers
    assert a.action == "exit" and a.urgency == "urgent"


def test_own_inhibitor_lost_orients_to_red_side():
    # inhib_diff = +1 means RED lost an inhib (blue-minus-red positive).
    # If I'm long RED, that's MY inhibitor lost.
    frame = _frame(minute=30, inhib_diff=+1)
    pos = PositionState(side="red", entry_price_cents=60, contracts=50)
    a = assess_exit(0.45, 42, frame, pos)  # fair_blue 0.45 -> red fair 0.55
    assert "own_inhibitor_lost" in a.triggers
    assert a.action == "exit"


def test_catastrophic_swing_urgent_exit():
    frame = _frame(minute=28, gold_diff_change_last_60s=-3000)  # got aced
    pos = PositionState(side="blue", entry_price_cents=55, contracts=80)
    a = assess_exit(0.5, 50, frame, pos)
    assert "catastrophic_swing_60s" in a.triggers
    assert a.action == "exit" and a.urgency == "urgent"


def test_opponent_baron_reduce_when_levered():
    frame = _frame(minute=29, baron_diff=-1, gold_diff=1500)  # red has baron
    pos = PositionState(side="blue", entry_price_cents=55, contracts=50)
    a = assess_exit(0.56, 54, frame, pos)
    assert "opponent_baron_active" in a.triggers
    assert a.action in ("reduce", "exit")


# ---------- take profit ----------

def test_take_profit_ladders_into_leverage():
    """Up 20c (44->64 mkt) into a late levered game with edge gone -> ladder out."""
    frame = _frame(minute=30, gold_diff=2000, baron_diff=1)  # blue has baron
    pos = PositionState(side="blue", entry_price_cents=44, contracts=100)
    a = assess_exit(model_fair_blue=0.66, market_price_blue_cents=64,
                    frame=frame, position=pos)
    assert a.action == "take_profit"
    assert len(a.suggested_ladder) == 3
    # rungs are above current market and sum to ~1.0
    assert all(price > 64 for price, _ in a.suggested_ladder)
    assert abs(sum(frac for _, frac in a.suggested_ladder) - 1.0) < 0.02


# ---------- hold ----------

def test_hold_when_edge_present_low_leverage():
    frame = _frame(minute=14, gold_diff=1800)  # early, buffered
    pos = PositionState(side="blue", entry_price_cents=45, contracts=50)
    a = assess_exit(model_fair_blue=0.60, market_price_blue_cents=48,
                    frame=frame, position=pos)
    assert a.action == "hold"
    assert a.edge > 0


# ---------- sizing ----------

def test_size_zero_when_no_edge():
    assert size_from_prices(0.50, 0.55, 0.2, 100000, 0.3) == 0


def test_size_discounts_with_leverage():
    base = size_from_prices(0.60, 0.50, 0.2, 100000, leverage=0.0)
    levered = size_from_prices(0.60, 0.50, 0.2, 100000, leverage=0.8)
    assert levered < base


def test_size_capped_at_max_pct():
    # huge edge should still cap at 5% of bankroll
    sz = size_from_prices(0.90, 0.30, 0.0, 100000, leverage=0.0, max_pct=0.05)
    assert sz <= 5000
