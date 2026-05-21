"""Backtest-specific metrics.

PnL, Sharpe, max drawdown, win rate, edge realization correlation,
per-confidence-bucket breakdown.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from loltrader.backtest.portfolio import Portfolio


@dataclass
class BacktestMetrics:
    n_trades: int
    n_winning: int
    win_rate: float
    total_pnl_cents: int
    avg_pnl_per_trade_cents: float
    median_pnl_per_trade_cents: float
    largest_win_cents: int
    largest_loss_cents: int
    avg_win_cents: float
    avg_loss_cents: float
    # Risk
    max_drawdown_cents: int
    max_drawdown_pct: float
    sharpe_per_trade: float          # mean / std of per-trade PnL (not annualized)
    # Edge fidelity
    realized_edge: float             # mean of (yes_won_int - model_prob) across trades
    edge_realization_corr: float     # corr(predicted_edge, realized_pnl_per_contract)
    # Calibration of bets
    avg_predicted_prob: float        # mean of model_prob across trades
    avg_realized_prob: float         # fraction of trades whose YES outcome occurred


def compute_metrics(portfolio: Portfolio, trade_log: list[dict]) -> BacktestMetrics:
    closed = portfolio.closed_positions
    if not closed:
        return _empty_metrics()

    pnls = np.array([p.pnl_cents for p in closed], dtype=np.int64)
    wins = pnls > 0
    losses = pnls <= 0
    n = len(closed)

    # Drawdown: walk through trade-by-trade equity curve
    equity = portfolio.starting_bankroll_cents + np.cumsum(pnls)
    running_max = np.maximum.accumulate(np.concatenate(([portfolio.starting_bankroll_cents], equity)))
    drawdowns = running_max[:-1] + 0  # initial running max excluding final
    # Better: at each point, drawdown = running_max_so_far - current
    running_max_curve = np.maximum.accumulate(equity)
    dd = running_max_curve - equity
    max_dd_cents = int(np.max(dd)) if len(dd) > 0 else 0
    max_dd_pct = max_dd_cents / portfolio.starting_bankroll_cents if portfolio.starting_bankroll_cents else 0

    # Sharpe per trade (rough)
    sharpe = float(np.mean(pnls) / np.std(pnls)) if np.std(pnls) > 0 else 0.0

    # Edge fidelity: for each trade, predicted_edge vs realized PnL per contract
    # Map closed positions to their trade_log entries by market_ticker
    log_by_market = {t["market_ticker"]: t for t in trade_log}
    predicted_edges = []
    realized_pnl_per_contract = []
    realized_yes_won = []
    model_probs = []
    for pos in closed:
        log_entry = log_by_market.get(pos.market_ticker, {})
        predicted_edges.append(log_entry.get("edge_at_entry", 0.0))
        per_contract = pos.pnl_cents / pos.contracts
        realized_pnl_per_contract.append(per_contract)
        # "yes_won" from this side's perspective: did our side win?
        side_won = 1 if (pos.pnl_cents or 0) > 0 else 0
        realized_yes_won.append(side_won)
        model_probs.append(pos.model_prob)

    predicted_edges = np.array(predicted_edges)
    realized_pnl_per_contract = np.array(realized_pnl_per_contract)
    realized_yes_won = np.array(realized_yes_won)
    model_probs = np.array(model_probs)

    if len(predicted_edges) > 1 and np.std(predicted_edges) > 0 and np.std(realized_pnl_per_contract) > 0:
        corr = float(np.corrcoef(predicted_edges, realized_pnl_per_contract)[0, 1])
    else:
        corr = 0.0

    realized_edge = float(np.mean(realized_yes_won - model_probs))

    return BacktestMetrics(
        n_trades=n,
        n_winning=int(wins.sum()),
        win_rate=float(wins.mean()),
        total_pnl_cents=int(pnls.sum()),
        avg_pnl_per_trade_cents=float(pnls.mean()),
        median_pnl_per_trade_cents=float(np.median(pnls)),
        largest_win_cents=int(pnls.max()) if n > 0 else 0,
        largest_loss_cents=int(pnls.min()) if n > 0 else 0,
        avg_win_cents=float(pnls[wins].mean()) if wins.sum() > 0 else 0.0,
        avg_loss_cents=float(pnls[losses].mean()) if losses.sum() > 0 else 0.0,
        max_drawdown_cents=max_dd_cents,
        max_drawdown_pct=float(max_dd_pct),
        sharpe_per_trade=sharpe,
        realized_edge=realized_edge,
        edge_realization_corr=corr,
        avg_predicted_prob=float(model_probs.mean()),
        avg_realized_prob=float(realized_yes_won.mean()),
    )


def _empty_metrics() -> BacktestMetrics:
    return BacktestMetrics(
        n_trades=0, n_winning=0, win_rate=0.0, total_pnl_cents=0,
        avg_pnl_per_trade_cents=0.0, median_pnl_per_trade_cents=0.0,
        largest_win_cents=0, largest_loss_cents=0,
        avg_win_cents=0.0, avg_loss_cents=0.0,
        max_drawdown_cents=0, max_drawdown_pct=0.0,
        sharpe_per_trade=0.0,
        realized_edge=0.0, edge_realization_corr=0.0,
        avg_predicted_prob=0.0, avg_realized_prob=0.0,
    )
