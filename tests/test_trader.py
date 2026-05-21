"""Trader tests: gates, paper engine, kill switches."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from loltrader.db import connect, migrate
from loltrader.trader.gates import GateInputs, validate_decision
from loltrader.trader.killswitch import KillLevel, evaluate_kill_state
from loltrader.trader.paper import (
    execute_paper_fill,
    log_decision,
    settle_paper_trade,
)


# --- Gates ---------------------------------------------------------------

def _gate_template(**overrides) -> GateInputs:
    base = dict(
        bankroll_cents=100_000,
        current_exposure_cents=0,
        proposed_position_cost_cents=5_000,
        daily_pnl_cents=0,
        total_session_pnl_cents=0,
        starting_bankroll_cents=100_000,
        model_uncertainty=0.10,
        max_uncertainty=0.40,
        edge=0.10,
        edge_threshold=0.03,
        last_data_seen_ts=int(time.time()),
        now_ts=int(time.time()),
        market_close_unix=int(time.time()) + 3600,
        link_confidence=0.95,
        series_ticker="KXLOLGAME",
    )
    base.update(overrides)
    return GateInputs(**base)


def test_all_gates_pass():
    assert validate_decision(_gate_template()) is None


def test_unknown_series_fails():
    assert validate_decision(_gate_template(series_ticker="KXSOMETHINGELSE")) == "unknown_series"


def test_low_link_confidence_fails():
    assert validate_decision(_gate_template(link_confidence=0.5)) == "low_link_confidence"


def test_uncertainty_too_high_fails():
    assert validate_decision(_gate_template(model_uncertainty=0.5)) == "uncertainty_too_high"


def test_edge_below_threshold_fails():
    assert validate_decision(_gate_template(edge=0.02, edge_threshold=0.03)) == "edge_below_threshold"


def test_per_market_cap_fails():
    # 5% of 100k = 5000, propose 10000
    assert validate_decision(_gate_template(proposed_position_cost_cents=10_000)) == "exceeds_per_market_cap"


def test_total_exposure_cap_fails():
    # 20% of 100k = 20000; current 18000, propose 5000 -> 23000 > 20000
    assert validate_decision(_gate_template(
        current_exposure_cents=18_000, proposed_position_cost_cents=5_000,
    )) == "exceeds_total_exposure_cap"


def test_daily_stop_loss_breached():
    # 10% of 100k = 10000; daily PnL = -11000
    assert validate_decision(_gate_template(daily_pnl_cents=-11_000)) == "daily_stop_loss_breached"


def test_data_stale_fails():
    now = int(time.time())
    # 30s old (threshold default is 5s)
    assert validate_decision(_gate_template(
        last_data_seen_ts=now - 30, now_ts=now,
    )) == "data_stale"


def test_market_resolves_too_far_out():
    now = int(time.time())
    # 30 days out (cap is 14)
    assert validate_decision(_gate_template(
        market_close_unix=now + 30 * 86400, now_ts=now,
    )) == "market_resolves_too_far_out"


# --- Kill switches -------------------------------------------------------

def test_kill_none_when_healthy(tmp_path: Path):
    now = int(time.time())
    state = evaluate_kill_state(
        daily_pnl_cents=0, session_pnl_cents=0,
        starting_bankroll_cents=100_000,
        last_data_seen_ts=now, now_ts=now,
        kill_file=tmp_path / "no_such_file",
    )
    assert state.level == KillLevel.NONE


def test_kill_soft_on_daily_drawdown(tmp_path: Path):
    now = int(time.time())
    state = evaluate_kill_state(
        daily_pnl_cents=-11_000, session_pnl_cents=-11_000,
        starting_bankroll_cents=100_000,
        last_data_seen_ts=now, now_ts=now,
        kill_file=tmp_path / "no_such_file",
    )
    assert state.level == KillLevel.SOFT
    assert state.reason == "daily_drawdown_soft"


def test_kill_hard_on_session_drawdown(tmp_path: Path):
    now = int(time.time())
    state = evaluate_kill_state(
        daily_pnl_cents=0, session_pnl_cents=-25_000,
        starting_bankroll_cents=100_000,
        last_data_seen_ts=now, now_ts=now,
        kill_file=tmp_path / "no_such_file",
    )
    assert state.level == KillLevel.HARD


def test_kill_emergency_on_severe_drawdown(tmp_path: Path):
    now = int(time.time())
    state = evaluate_kill_state(
        daily_pnl_cents=0, session_pnl_cents=-35_000,
        starting_bankroll_cents=100_000,
        last_data_seen_ts=now, now_ts=now,
        kill_file=tmp_path / "no_such_file",
    )
    assert state.level == KillLevel.EMERGENCY


def test_kill_hard_when_data_dead(tmp_path: Path):
    now = int(time.time())
    state = evaluate_kill_state(
        daily_pnl_cents=0, session_pnl_cents=0,
        starting_bankroll_cents=100_000,
        last_data_seen_ts=now - 120, now_ts=now,   # 2 min stale
        kill_file=tmp_path / "no_such_file",
    )
    assert state.level == KillLevel.HARD
    assert state.reason == "data_feed_dead"


def test_kill_soft_on_manual_file(tmp_path: Path):
    kf = tmp_path / "KILL_SWITCH"
    kf.touch()
    now = int(time.time())
    state = evaluate_kill_state(
        daily_pnl_cents=0, session_pnl_cents=0,
        starting_bankroll_cents=100_000,
        last_data_seen_ts=now, now_ts=now,
        kill_file=kf,
    )
    assert state.level == KillLevel.SOFT
    assert state.reason == "manual_killfile"


# --- Paper engine --------------------------------------------------------

@pytest.fixture
def trader_db(tmp_path: Path):
    db = tmp_path / "trader.db"
    conn = connect(db)
    migrate(conn)
    # Need a Kalshi market so decision FK doesn't fail. But the FK on
    # market_ticker references kalshi_markets which would need its own
    # parent rows. For simplicity, foreign_keys = ON is default but we
    # can disable for these tests since we're only testing trader logic.
    conn.execute("PRAGMA foreign_keys = OFF")
    yield conn
    conn.close()


def test_log_decision_persists(trader_db):
    did = log_decision(
        trader_db,
        market_ticker="M1", match_id=1, model_version="v1",
        model_prob=0.65, p10=0.55, p90=0.75,
        market_yes_bid_cents=50, market_yes_ask_cents=52,
        edge=0.13, edge_threshold=0.03,
        action="BUY_YES", gate_reason=None,
    )
    assert did > 0
    row = trader_db.execute("SELECT action, gate_reason FROM decisions WHERE decision_id = ?",
                            (did,)).fetchone()
    assert row["action"] == "BUY_YES"
    assert row["gate_reason"] is None


def test_paper_fill_yes(trader_db):
    did = log_decision(
        trader_db,
        market_ticker="M1", match_id=1, model_version="v1",
        model_prob=0.65, p10=0.55, p90=0.75,
        market_yes_bid_cents=50, market_yes_ask_cents=52,
        edge=0.13, edge_threshold=0.03,
        action="BUY_YES", gate_reason=None,
    )
    fill = execute_paper_fill(
        trader_db,
        decision_id=did, side="YES", contracts=10,
        market_yes_ask_cents=52, market_yes_bid_cents=50,
    )
    # Ask + 1c slippage = 53
    assert fill.fill_price_cents == 53
    # Fee at 53c, 10 contracts: 7*10*53*47/10000, ceiling
    expected_fee = (7 * 10 * 53 * 47 + 9999) // 10000
    assert fill.entry_fee_cents == expected_fee


def test_settle_paper_trade_yes_win(trader_db):
    did = log_decision(
        trader_db,
        market_ticker="M1", match_id=1, model_version="v1",
        model_prob=0.65, p10=0.55, p90=0.75,
        market_yes_bid_cents=50, market_yes_ask_cents=52,
        edge=0.13, edge_threshold=0.03,
        action="BUY_YES", gate_reason=None,
    )
    fill = execute_paper_fill(
        trader_db,
        decision_id=did, side="YES", contracts=10,
        market_yes_ask_cents=52, market_yes_bid_cents=50,
    )
    pnl = settle_paper_trade(trader_db, trade_id=fill.trade_id, yes_won=True)
    # Revenue: 10 * 100 = 1000. Cost: 10 * 53 + fee
    expected_fee = (7 * 10 * 53 * 47 + 9999) // 10000
    expected_pnl = 1000 - (10 * 53 + expected_fee)
    assert pnl == expected_pnl
