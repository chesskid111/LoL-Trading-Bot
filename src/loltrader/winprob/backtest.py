"""Backtest framework — replays games minute-by-minute through the win-prob
model and simulates a trading strategy.

We don't have historical Kalshi prices, so we approximate the market price
two ways:

  1. **Naive baseline**: sigmoid(gold_diff / 5000) — what a market that only
     uses gold_diff would predict. Captures the "uncalibrated market" case.

  2. **Lagged-model baseline**: the model's prediction at minute t-3 used as
     "what the market knew 3 minutes ago." Simulates a market that reacts
     more slowly than the model.

For each frame, edge = model_p - market_p. If |edge| > threshold AND the
ensemble uncertainty band is narrow enough, simulate a trade with half-Kelly
sizing. Track P&L, drawdown, Sharpe across the holdout games.

This is a simulation, not actual Kalshi P&L — but it validates that the model
generates exploitable signal and gives a Sharpe number for the strategy.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from loltrader.winprob.model import LiveWinProbModel
from loltrader.winprob.state import FEATURE_SCHEMA

log = logging.getLogger(__name__)


# Trading sim parameters
INITIAL_BANKROLL = 1000.0
MIN_EDGE_TO_TRADE = 0.04             # need ≥4¢ edge
MAX_BAND_WIDTH_TO_TRADE = 0.20       # ensemble must agree
KELLY_FRACTION = 0.5                  # half-Kelly sizing
MAX_PER_POSITION = 0.05               # never bet >5% of bankroll
MIN_GAMES_FOR_METRICS = 3


@dataclass
class BacktestResult:
    n_trades: int
    n_winners: int
    hit_rate: float
    final_bankroll: float
    pnl_total: float
    pnl_per_trade: float
    max_drawdown_pct: float
    sharpe: float
    per_minute_trades: dict[int, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def _naive_market_price(gold_diff: float) -> float:
    """Sigmoid baseline market price using only gold_diff."""
    return 1.0 / (1.0 + math.exp(-gold_diff / 5000.0))


def _features_matrix(df: pd.DataFrame, schema: list[str]) -> np.ndarray:
    """Extract schema-ordered features matrix from df."""
    X = np.zeros((len(df), len(schema)), dtype=np.float32)
    for i, col in enumerate(schema):
        if col in df.columns:
            X[:, i] = df[col].fillna(0.0).to_numpy(dtype=np.float32)
    return X


def run_backtest(
    model: LiveWinProbModel,
    holdout_df: pd.DataFrame,
    market_strategy: str = "naive",
    min_edge: float = MIN_EDGE_TO_TRADE,
    initial_bankroll: float = INITIAL_BANKROLL,
) -> BacktestResult:
    """Simulate trading across holdout games.

    Args:
        market_strategy: "naive" (gold-only sigmoid) or "lagged" (model at t-3)
    """
    df = holdout_df.copy().sort_values(["game_id", "minute"])

    # Compute predictions in batch for efficiency
    X = _features_matrix(df, model.feature_schema)
    raw = np.mean([m.predict_proba(X)[:, 1] for m in model.ensemble], axis=0)
    cal = model.calibrator.transform(raw)

    # Ensemble uncertainty band (computed from raw, calibrated independently)
    raw_per_member = np.array([m.predict_proba(X)[:, 1] for m in model.ensemble])
    p10_raw = np.percentile(raw_per_member, 10, axis=0)
    p90_raw = np.percentile(raw_per_member, 90, axis=0)
    p10 = model.calibrator.transform(p10_raw)
    p90 = model.calibrator.transform(p90_raw)
    band_width = p90 - p10

    df = df.assign(model_p=cal, band=band_width)

    bankroll = initial_bankroll
    peak_bankroll = bankroll
    max_drawdown_pct = 0.0
    trades: list[dict] = []
    per_minute_trades: dict[int, int] = {}

    for game_id, game_df in df.groupby("game_id"):
        game_df = game_df.reset_index(drop=True)
        if len(game_df) < 2:
            continue
        winner_blue = int(game_df.iloc[0]["label"])  # same label across all frames

        # Compute market prices for this game
        for i, row in game_df.iterrows():
            minute = int(row["minute"])
            model_p = float(row["model_p"])
            band = float(row["band"])

            # Market price
            if market_strategy == "naive":
                market_p = _naive_market_price(float(row.get("gold_diff", 0.0)))
            elif market_strategy == "lagged":
                lag_idx = max(0, i - 3)
                market_p = float(game_df.iloc[lag_idx]["model_p"])
            else:
                raise ValueError(f"unknown market_strategy: {market_strategy}")

            edge = model_p - market_p
            if abs(edge) < min_edge or band > MAX_BAND_WIDTH_TO_TRADE:
                continue

            # Half-Kelly sizing scaled by inverse band
            kelly = (abs(edge) / max(0.01, market_p if edge < 0 else (1 - market_p)))
            size = bankroll * kelly * KELLY_FRACTION * (1 - band) ** 2
            size = min(size, bankroll * MAX_PER_POSITION)
            if size < 1.0:
                continue

            # Simulate outcome (settles at game end)
            blue_wins = (winner_blue == 1)
            # Long blue if edge positive, long red otherwise
            bet_on_blue = edge > 0
            won = (bet_on_blue == blue_wins)

            # P&L: simple binary payoff
            # Pay market_p (or 1-market_p) per contract; settle at 1 or 0
            entry_cost = (market_p if bet_on_blue else (1 - market_p))
            payoff = (1.0 if won else 0.0)
            contracts = size / max(entry_cost, 0.01)
            pnl = contracts * (payoff - entry_cost)

            bankroll += pnl
            peak_bankroll = max(peak_bankroll, bankroll)
            dd_pct = 1.0 - (bankroll / peak_bankroll)
            max_drawdown_pct = max(max_drawdown_pct, dd_pct)

            trades.append({
                "game_id": game_id, "minute": minute,
                "model_p": model_p, "market_p": market_p, "edge": edge,
                "band": band, "size": size, "pnl": pnl, "won": won,
            })
            per_minute_trades[minute] = per_minute_trades.get(minute, 0) + 1

    n_trades = len(trades)
    if n_trades < MIN_GAMES_FOR_METRICS:
        return BacktestResult(
            n_trades=n_trades, n_winners=0,
            hit_rate=float("nan"),
            final_bankroll=bankroll,
            pnl_total=bankroll - initial_bankroll,
            pnl_per_trade=float("nan"),
            max_drawdown_pct=max_drawdown_pct,
            sharpe=float("nan"),
            per_minute_trades=per_minute_trades,
            notes=[f"too few trades ({n_trades}) for meaningful metrics"],
        )

    n_winners = sum(1 for t in trades if t["won"])
    hit_rate = n_winners / n_trades
    pnl_per_trade = sum(t["pnl"] for t in trades) / n_trades
    returns = np.array([t["pnl"] for t in trades], dtype=np.float64)
    if returns.std() > 0:
        sharpe = float(returns.mean() / returns.std() * math.sqrt(252))
    else:
        sharpe = float("nan")

    return BacktestResult(
        n_trades=n_trades,
        n_winners=n_winners,
        hit_rate=hit_rate,
        final_bankroll=bankroll,
        pnl_total=bankroll - initial_bankroll,
        pnl_per_trade=pnl_per_trade,
        max_drawdown_pct=max_drawdown_pct,
        sharpe=sharpe,
        per_minute_trades=per_minute_trades,
    )


def run_backtest_from_files(
    model_path: str | Path,
    dataset_path: str | Path,
    market_strategy: str = "naive",
) -> BacktestResult:
    """End-to-end CLI helper: load model + dataset, time-split, backtest holdout."""
    from loltrader.winprob.train import _time_split

    model = LiveWinProbModel.load(model_path)
    df = pd.read_parquet(dataset_path)
    _, _, holdout = _time_split(df)
    if len(holdout) == 0:
        raise RuntimeError("no holdout rows after split")

    log.info("backtest on %d holdout rows (strategy=%s)", len(holdout), market_strategy)
    result = run_backtest(model, holdout, market_strategy=market_strategy)

    log.info("result: n_trades=%d hit_rate=%.3f pnl=%+.2f bankroll=%.2f "
             "max_dd=%.1f%% sharpe=%.2f",
             result.n_trades, result.hit_rate, result.pnl_total,
             result.final_bankroll, result.max_drawdown_pct * 100, result.sharpe)
    return result
