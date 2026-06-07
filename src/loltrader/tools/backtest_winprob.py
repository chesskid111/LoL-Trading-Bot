"""Backtest the trained win-prob model on holdout games.

Usage:
    python -m loltrader.tools.backtest_winprob
    python -m loltrader.tools.backtest_winprob --market lagged

Spec §Phase 4.5.
"""
from __future__ import annotations

import argparse
import logging
import sys

from loltrader.winprob.backtest import run_backtest_from_files


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--model", default="models/winprob_latest.pkl")
    p.add_argument("--dataset", default="data/winprob_training.parquet")
    p.add_argument("--market", default="naive", choices=["naive", "lagged"],
                   help="Market-price approximation strategy")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )
    log = logging.getLogger(__name__)

    try:
        result = run_backtest_from_files(args.model, args.dataset, args.market)
    except Exception:
        log.exception("backtest failed")
        return 1

    log.info("---- backtest summary ----")
    log.info("trades:        %d (winners=%d, hit_rate=%.1f%%)",
             result.n_trades, result.n_winners, result.hit_rate * 100)
    log.info("P&L total:     %+.2f (initial $1000 → final $%.2f)",
             result.pnl_total, result.final_bankroll)
    log.info("P&L per trade: %+.2f", result.pnl_per_trade)
    log.info("Max drawdown:  %.1f%%", result.max_drawdown_pct * 100)
    log.info("Sharpe:        %.2f", result.sharpe)
    if result.per_minute_trades:
        top_active = sorted(result.per_minute_trades.items(),
                             key=lambda x: -x[1])[:8]
        log.info("Most active trade minutes: %s", top_active)
    if result.notes:
        for n in result.notes:
            log.warning("note: %s", n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
