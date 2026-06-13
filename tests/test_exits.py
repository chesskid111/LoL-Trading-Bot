"""Tests for the exit-side risk evaluator (loltrader.trader.exits)."""
from __future__ import annotations

from loltrader.trader.exits import (
    PositionState, assess_exit, game_leverage, size_from_prices, risk_signals,
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


# ---------- structural triggers: edge sets direction, leverage sets size ----------

def test_inhib_lost_with_fat_edge_does_not_exit():
    """Blue lost an inhib, but the MARKET overreacted: model 30%, market 15%.
    The comeback is underpriced. Must NOT exit — hold (fat edge, high leverage)."""
    frame = _frame(minute=30, inhib_diff=-1)
    pos = PositionState(side="blue", entry_price_cents=15, contracts=100)
    a = assess_exit(model_fair_blue=0.30, market_price_blue_cents=15,
                    frame=frame, position=pos)
    assert "own_inhibitor_lost" in a.triggers
    assert a.action != "exit"          # the model says comeback is live + underpriced
    assert a.action in ("hold",)       # fat edge but high leverage -> hold, don't add


def test_inhib_lost_no_edge_reduces_not_exits():
    """Blue lost an inhib, market agrees with model (no edge), high leverage.
    Reduce to manage un-reactable variance — but NOT a blanket exit."""
    frame = _frame(minute=30, inhib_diff=-1)
    pos = PositionState(side="blue", entry_price_cents=28, contracts=100)
    a = assess_exit(model_fair_blue=0.26, market_price_blue_cents=28,
                    frame=frame, position=pos)
    assert "own_inhibitor_lost" in a.triggers
    # edge = 0.26 - 0.28 = -0.02 (no meaningful edge), not deep enough to flip
    assert a.action == "reduce"


def test_terminal_aced_with_inhib_late_exits():
    """The narrow real exit: aced (catastrophic swing) AND base cracked (inhib)
    late — settles before any comeback can occur."""
    frame = _frame(minute=32, inhib_diff=-1, gold_diff_change_last_60s=-3000)
    pos = PositionState(side="blue", entry_price_cents=20, contracts=80)
    a = assess_exit(0.10, 12, frame, pos)
    assert a.action == "exit" and a.urgency == "urgent"
    assert "terminal" in a.reason


def test_catastrophic_swing_alone_not_terminal():
    """Aced but base intact (no inhib lost) + model still favors you -> not a
    terminal exit; comeback window is live."""
    frame = _frame(minute=28, gold_diff_change_last_60s=-3000)  # aced, base safe
    pos = PositionState(side="blue", entry_price_cents=45, contracts=80)
    a = assess_exit(model_fair_blue=0.58, market_price_blue_cents=45,
                    frame=frame, position=pos)
    assert "catastrophic_swing_60s" in a.triggers
    assert a.action != "exit"          # fair 58% > market 45% -> edge, don't fold


def test_opponent_baron_no_edge_reduces():
    # Clearly favored (62%, not a coinflip) but opponent took baron and there's
    # no edge vs market -> reduce the un-reactable variance, don't exit.
    frame = _frame(minute=29, baron_diff=-1, gold_diff=1500)  # red has baron
    pos = PositionState(side="blue", entry_price_cents=58, contracts=50)
    a = assess_exit(0.62, 60, frame, pos)   # edge +0.02 (thin), not coinflip
    assert "opponent_baron_active" in a.triggers
    assert a.action == "reduce"


# ---------- buy the overreaction ----------

def test_buy_overreaction_when_leverage_manageable():
    """Market dumped past fair in a still-manageable game -> ADD (buy the dip)."""
    frame = _frame(minute=20, gold_diff=-1500)  # behind but not late/levered
    pos = PositionState(side="blue", entry_price_cents=40, contracts=50)
    a = assess_exit(model_fair_blue=0.50, market_price_blue_cents=38,
                    frame=frame, position=pos)
    assert a.action == "add"


def test_fat_edge_but_high_leverage_holds_not_adds():
    frame = _frame(minute=33, inhib_diff=-1)   # high leverage
    pos = PositionState(side="blue", entry_price_cents=30, contracts=50)
    a = assess_exit(model_fair_blue=0.45, market_price_blue_cents=30,
                    frame=frame, position=pos)
    assert a.action == "hold"          # edge +15 but can't react -> don't add


def test_deep_tail_requires_fatter_edge():
    """In the deep-underdog zone the model is over-optimistic; a thin edge is
    NOT enough to add (avoids catching a falling knife on a bad model number)."""
    frame = _frame(minute=20, gold_diff=-3000)
    pos = PositionState(side="blue", entry_price_cents=15, contracts=50)
    # model 18%, market 15% -> edge +3% — below the 14% deep-tail threshold
    a = assess_exit(model_fair_blue=0.18, market_price_blue_cents=15,
                    frame=frame, position=pos)
    assert a.action != "add"


def test_overpriced_side_reduces():
    """Market over-rates your side (model below market) -> reduce/exit."""
    frame = _frame(minute=22, gold_diff=-500)
    pos = PositionState(side="blue", entry_price_cents=55, contracts=50)
    a = assess_exit(model_fair_blue=0.42, market_price_blue_cents=55, frame=frame,
                    position=pos)  # edge -0.13
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

def test_hold_when_thin_edge_present():
    """Thin positive edge (above LOW_EDGE, below the add threshold) -> hold."""
    frame = _frame(minute=14, gold_diff=1800)  # early, buffered
    pos = PositionState(side="blue", entry_price_cents=48, contracts=50)
    a = assess_exit(model_fair_blue=0.53, market_price_blue_cents=48,
                    frame=frame, position=pos)   # edge +0.05
    assert a.action == "hold"
    assert a.edge > 0


# ---------- position-agnostic risk signals (dashboard badge) ----------

def test_risk_signals_coinflip_zone():
    frame = _frame(minute=34, gold_diff=200, soul_blue=1)
    rs = risk_signals(p_blue=0.51, frame=frame)
    assert rs.coinflip_zone is True
    assert "COINFLIP" in rs.headline


def test_risk_signals_not_coinflip_when_favored():
    frame = _frame(minute=34, gold_diff=200, soul_blue=1)
    rs = risk_signals(p_blue=0.62, frame=frame)   # favored, not ~50%
    assert rs.coinflip_zone is False


def test_risk_signals_per_side_triggers():
    # blue lost an inhib, red took baron, blue swung down hard
    frame = _frame(minute=30, inhib_diff=-1, baron_diff=-1,
                   gold_diff_change_last_60s=-1500)
    rs = risk_signals(p_blue=0.45, frame=frame)
    assert "own_inhibitor_lost" in rs.triggers_blue
    assert "opponent_baron_active" in rs.triggers_blue
    assert "adverse_swing_60s" in rs.triggers_blue
    # red's perspective: red took the baron (not "opponent baron" for red)
    assert "opponent_baron_active" not in rs.triggers_red


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
