"""Backtest report generation.

Writes a markdown report + PnL curve PNG.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import numpy as np

from loltrader.backtest.metrics import BacktestMetrics
from loltrader.backtest.portfolio import Portfolio


def _cents_to_dollars(cents: int | float) -> str:
    return f"${cents / 100:.2f}"


def write_report(
    portfolio: Portfolio,
    metrics: BacktestMetrics,
    trade_log: list[dict],
    skipped_counters: dict[str, int],
    output_dir: Path,
    title: str = "v1 backtest",
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    report_path = output_dir / f"backtest_report_{ts}.md"
    curve_path = output_dir / f"backtest_pnl_{ts}.png"

    # PnL curve
    _save_pnl_curve(portfolio, curve_path)

    lines: list[str] = []
    lines.append(f"# {title}")
    lines.append("")
    lines.append(f"**Generated:** {ts}")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    starting = portfolio.starting_bankroll_cents
    ending = starting + metrics.total_pnl_cents
    ret_pct = (metrics.total_pnl_cents / starting) * 100 if starting else 0.0
    lines.append(f"| Metric | Value |")
    lines.append(f"|---|---|")
    lines.append(f"| Starting bankroll | {_cents_to_dollars(starting)} |")
    lines.append(f"| Ending bankroll | {_cents_to_dollars(ending)} |")
    lines.append(f"| Total PnL | {_cents_to_dollars(metrics.total_pnl_cents)} ({ret_pct:+.2f}%) |")
    lines.append(f"| Trades | {metrics.n_trades} |")
    lines.append(f"| Win rate | {metrics.win_rate * 100:.1f}% ({metrics.n_winning}/{metrics.n_trades}) |")
    lines.append(f"| Avg PnL / trade | {_cents_to_dollars(metrics.avg_pnl_per_trade_cents)} |")
    lines.append(f"| Median PnL / trade | {_cents_to_dollars(metrics.median_pnl_per_trade_cents)} |")
    lines.append(f"| Largest win | {_cents_to_dollars(metrics.largest_win_cents)} |")
    lines.append(f"| Largest loss | {_cents_to_dollars(metrics.largest_loss_cents)} |")
    lines.append(f"| Avg win | {_cents_to_dollars(metrics.avg_win_cents)} |")
    lines.append(f"| Avg loss | {_cents_to_dollars(metrics.avg_loss_cents)} |")
    lines.append(f"| Max drawdown | {_cents_to_dollars(metrics.max_drawdown_cents)} ({metrics.max_drawdown_pct * 100:.2f}%) |")
    lines.append(f"| Sharpe (per-trade) | {metrics.sharpe_per_trade:.3f} |")
    lines.append("")
    lines.append("## Edge fidelity")
    lines.append("")
    lines.append("Does the model's claimed edge actually materialize?")
    lines.append("")
    lines.append(f"| Metric | Value |")
    lines.append(f"|---|---|")
    lines.append(f"| Avg predicted probability of our side winning | {metrics.avg_predicted_prob:.3f} |")
    lines.append(f"| Avg realized win rate (our side actually won) | {metrics.avg_realized_prob:.3f} |")
    lines.append(f"| Realized edge (realized - predicted) | {metrics.realized_edge:+.3f} |")
    lines.append(f"| Corr(predicted edge, realized PnL/contract) | {metrics.edge_realization_corr:+.3f} |")
    lines.append("")
    lines.append("Edge realization correlation should be positive — bigger predicted edges should produce bigger realized profits. A near-zero correlation suggests the model can't distinguish strong-edge from weak-edge trades.")
    lines.append("")

    lines.append("## Skip counters (decisions that didn't trade)")
    lines.append("")
    lines.append("| Reason | Count |")
    lines.append("|---|---|")
    for k, v in skipped_counters.items():
        lines.append(f"| {k} | {v} |")
    lines.append("")

    lines.append("## PnL curve")
    lines.append("")
    lines.append(f"![PnL]({curve_path.name})")
    lines.append("")

    lines.append("## Trade log")
    lines.append("")
    if trade_log:
        lines.append("| Date | Match | Side | Contracts | Entry $ | Model P | Edge | Result | PnL |")
        lines.append("|---|---|---|---|---|---|---|---|---|")
        # Map trades to positions by market_ticker
        pos_by_mt = {p.market_ticker: p for p in portfolio.closed_positions}
        for t in trade_log:
            pos = pos_by_mt.get(t["market_ticker"])
            outcome = ("WIN" if (pos.pnl_cents or 0) > 0 else "LOSS") if pos else "?"
            pnl = _cents_to_dollars(pos.pnl_cents or 0) if pos else "$0.00"
            lines.append(
                f"| {t['candle_date']} "
                f"| {t['match_id']} "
                f"| {t['side']} "
                f"| {t['contracts']} "
                f"| ${t['entry_price_cents']/100:.2f} "
                f"| {t['model_prob']:.3f} "
                f"| {t['edge_at_entry']:+.3f} "
                f"| {outcome} "
                f"| {pnl} |"
            )
    else:
        lines.append("_(no trades)_")

    report_path.write_text("\n".join(lines), encoding="utf-8")
    return report_path


def _save_pnl_curve(portfolio: Portfolio, out_path: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    closed = portfolio.closed_positions
    if not closed:
        # Empty curve
        fig, ax = plt.subplots(figsize=(8, 4))
        ax.text(0.5, 0.5, "(no trades to plot)", ha="center", va="center", transform=ax.transAxes)
        fig.savefig(out_path, dpi=120)
        plt.close(fig)
        return

    # Sort by settled_date to render PnL over time
    closed_sorted = sorted(closed, key=lambda p: p.settled_date or "")
    pnls = np.array([p.pnl_cents for p in closed_sorted], dtype=np.int64)
    equity = portfolio.starting_bankroll_cents + np.cumsum(pnls)

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 8), sharex=False)
    ax1.plot(range(1, len(equity) + 1), equity / 100, "b-", linewidth=2)
    ax1.axhline(portfolio.starting_bankroll_cents / 100, color="k", linestyle="--", alpha=0.5, label="Starting")
    ax1.set_xlabel("Trade #")
    ax1.set_ylabel("Equity ($)")
    ax1.set_title("Equity curve")
    ax1.legend()
    ax1.grid(True, alpha=0.3)

    ax2.bar(range(1, len(pnls) + 1), pnls / 100,
            color=["green" if p > 0 else "red" for p in pnls])
    ax2.set_xlabel("Trade #")
    ax2.set_ylabel("PnL ($)")
    ax2.set_title("Per-trade PnL")
    ax2.grid(True, alpha=0.3)

    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)
