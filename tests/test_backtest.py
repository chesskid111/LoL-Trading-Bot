"""Backtest tests: fee math, Kelly sizing, settlement PnL, no-leak."""
from __future__ import annotations

import pytest

from loltrader.backtest.fees import kalshi_fee_cents, slippage_cents
from loltrader.backtest.portfolio import Portfolio, Position


# --- Fee math -------------------------------------------------------------

def test_fee_at_midpoint():
    """At $0.50, 100 contracts: fee = 0.07 * 100 * 0.5 * 0.5 = $1.75 -> 175c."""
    assert kalshi_fee_cents(50, 100) == 175


def test_fee_at_high_price():
    """At $0.90, 100 contracts: fee = 0.07 * 100 * 0.9 * 0.1 = $0.63 -> 63c."""
    assert kalshi_fee_cents(90, 100) == 63


def test_fee_at_low_price():
    """At $0.10, 100 contracts: same as $0.90 by symmetry -> 63c."""
    assert kalshi_fee_cents(10, 100) == 63


def test_fee_rounds_up():
    """At $0.50, 1 contract: 0.07 * 1 * 0.5 * 0.5 = $0.0175 -> 2c (rounded up)."""
    assert kalshi_fee_cents(50, 1) == 2


def test_fee_edge_cases():
    assert kalshi_fee_cents(50, 0) == 0
    assert kalshi_fee_cents(0, 100) == 0    # price 0 = no fee
    assert kalshi_fee_cents(100, 100) == 0  # price 100 = no fee


def test_slippage_constant():
    assert slippage_cents() == 1


# --- Portfolio: Kelly sizing -----------------------------------------------

def test_kelly_zero_when_no_edge():
    """Model thinks 50%, market priced at 50% -> Kelly = 0."""
    p = Portfolio(starting_bankroll_cents=100_000, kelly_fraction=0.25)
    # Buying YES at 50c when model says 50%: no edge
    assert p.kelly_size_contracts("YES", model_prob=0.5, price_cents=50) == 0


def test_kelly_positive_with_edge():
    """Model thinks 70%, market priced at 50% -> positive Kelly."""
    p = Portfolio(starting_bankroll_cents=100_000, kelly_fraction=0.25)
    n = p.kelly_size_contracts("YES", model_prob=0.7, price_cents=50)
    assert n > 0


def test_kelly_fraction_scales():
    """Halving kelly_fraction roughly halves contract count."""
    p1 = Portfolio(starting_bankroll_cents=100_000, kelly_fraction=0.5)
    p2 = Portfolio(starting_bankroll_cents=100_000, kelly_fraction=0.25)
    n1 = p1.kelly_size_contracts("YES", 0.7, 50)
    n2 = p2.kelly_size_contracts("YES", 0.7, 50)
    assert abs(n1 - 2 * n2) <= 1


# --- Portfolio: risk gates -------------------------------------------------

def test_per_market_cap():
    """5% of $1000 = $50; trying to buy $100 worth fails."""
    p = Portfolio(starting_bankroll_cents=100_000, max_position_pct=0.05)
    # 200 contracts at 50c = $100 cost (ignoring fee)
    ok, reason = p.can_open("YES", contracts=200, price_cents=50, entry_fee_cents=0)
    assert not ok
    assert reason == "exceeds_per_market_cap"


def test_per_market_cap_within_limits_ok():
    p = Portfolio(starting_bankroll_cents=100_000, max_position_pct=0.05)
    # 80 contracts at 50c = $40
    ok, reason = p.can_open("YES", contracts=80, price_cents=50, entry_fee_cents=0)
    assert ok, reason


def test_total_exposure_cap():
    """20% of $1000 = $200 total."""
    p = Portfolio(
        starting_bankroll_cents=100_000,
        max_position_pct=0.05,
        max_total_exposure_pct=0.20,
    )
    # Open 4 positions of $50 each (= $200 total). Fifth one should fail.
    for i in range(4):
        ok, _ = p.can_open("YES", 100, 50, 0)
        assert ok
        p.open_position(Position(
            market_ticker=f"M{i}", match_id=i, side="YES",
            contracts=100, entry_price_cents=50, entry_fee_cents=0,
            entry_date="2026-01-01", model_prob=0.6, market_implied=0.5,
            edge=0.1, p10=0.5, p90=0.7,
        ))
    ok, reason = p.can_open("YES", 100, 50, 0)
    assert not ok
    assert reason == "exceeds_total_exposure_cap"


def test_cap_contracts_trims_to_feasible():
    p = Portfolio(starting_bankroll_cents=100_000, max_position_pct=0.05)
    # Desired 200, but only ~100 fits within 5% cap at 50c
    allowed, _ = p.cap_contracts(
        "YES", contracts=200, price_cents=50, entry_fee_cents_for=lambda n: 0
    )
    assert 0 < allowed <= 100


# --- Position settlement ---------------------------------------------------

def test_settle_yes_win():
    p = Portfolio(starting_bankroll_cents=100_000)
    pos = Position(
        market_ticker="M", match_id=1, side="YES",
        contracts=10, entry_price_cents=60, entry_fee_cents=5,
        entry_date="2026-01-01", model_prob=0.7, market_implied=0.6,
        edge=0.1, p10=0.6, p90=0.8,
    )
    p.open_position(pos)
    # Bankroll after entry: 100000 - (10*60 + 5) = 100000 - 605 = 99395
    assert p.bankroll_cents == 99_395

    # Settle YES wins: each contract pays out 100c
    pnl = p.settle_position(pos, yes_won=True, settled_date="2026-01-02")
    # Revenue = 10 * 100 = 1000. Cost = 605. PnL = 395.
    assert pnl == 395
    # Bankroll: 99395 + 1000 = 100395
    assert p.bankroll_cents == 100_395


def test_settle_yes_loss():
    p = Portfolio(starting_bankroll_cents=100_000)
    pos = Position(
        market_ticker="M", match_id=1, side="YES",
        contracts=10, entry_price_cents=60, entry_fee_cents=5,
        entry_date="2026-01-01", model_prob=0.7, market_implied=0.6,
        edge=0.1, p10=0.6, p90=0.8,
    )
    p.open_position(pos)
    pnl = p.settle_position(pos, yes_won=False, settled_date="2026-01-02")
    # Revenue = 0. Cost = 605. PnL = -605.
    assert pnl == -605
    assert p.bankroll_cents == 100_000 - 605


def test_settle_no_win():
    """We bought NO at 40c (which is buying NO when yes_bid=60). NO wins if YES loses."""
    p = Portfolio(starting_bankroll_cents=100_000)
    pos = Position(
        market_ticker="M", match_id=1, side="NO",
        contracts=10, entry_price_cents=40, entry_fee_cents=5,
        entry_date="2026-01-01", model_prob=0.3, market_implied=0.4,
        edge=0.1, p10=0.2, p90=0.4,
    )
    p.open_position(pos)
    # yes_won = False -> we (NO) win
    pnl = p.settle_position(pos, yes_won=False, settled_date="2026-01-02")
    # Revenue = 10 * 100 = 1000. Cost = 10*40 + 5 = 405. PnL = 595.
    assert pnl == 595
