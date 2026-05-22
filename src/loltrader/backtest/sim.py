"""Walk historical Kalshi data through the model + trader logic and
record realistic PnL.

v1 scope:
  - Trade KXLOLGAME (series winner) markets only — avoids correlation
    complexity from also trading map markets on the same series.
  - One trade per Kalshi market: at the first candle with a "live" price
    (yes_ask between 5 and 95c, meaning the market is actively pricing),
    we evaluate edge once and decide. No re-entries, no holding through.
  - Hold to settlement: positions close when the market resolves at
    100c (YES won) or 0c (NO won).
  - Strict no-leak: ``compute_features`` is called with
    as_of_date = candle's date, so all feature inputs are strictly
    older than the candle.

What's NOT in v1 backtest yet:
  - Trading multiple markets per match (correlation accounting needed)
  - Resizing / re-pricing decisions over the life of a market
  - Walk-forward retraining (currently uses the single v1_latest model)
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime

from loltrader.backtest.fees import kalshi_fee_cents, slippage_cents
from loltrader.backtest.portfolio import Portfolio, Position
from loltrader.features import compute_features
from loltrader.model.serve import Model
from loltrader.trader.paper import (
    execute_paper_fill,
    log_decision,
    settle_paper_trade,
)

log = logging.getLogger(__name__)

DEFAULT_BASE_EDGE_THRESHOLD = 0.03           # 3% required edge
UNCERTAINTY_PENALTY = 0.5                    # threshold += 0.5 * (p90 - p10)
LIVE_PRICE_RANGE_CENTS = (5, 95)             # only act on actively-trading markets


@dataclass
class BacktestResult:
    portfolio: Portfolio
    decisions_considered: int                # candles we evaluated
    skipped_no_edge: int                     # edge below threshold
    skipped_no_link: int                     # market not linked confidently
    skipped_already_traded: int              # one-trade-per-market rule
    skipped_no_features: int                 # feature computation failed
    skipped_cap: int                         # position would breach risk gates
    trade_log: list[dict] = field(default_factory=list)


def _eligible_candles_for_backtest(
    conn: sqlite3.Connection,
    start_date: str,
    end_date: str,
) -> list[sqlite3.Row]:
    """All KXLOLGAME candles in the window, joined to linked match info.
    Includes only candles where the market is actively trading and the
    underlying match has settled (so we know the outcome for PnL)."""
    return conn.execute(
        """
        SELECT
            c.market_ticker,
            c.end_period_ts,
            c.price_close_cents,
            c.yes_bid_close_cents,
            c.yes_ask_close_cents,
            c.volume,
            m.event_ticker,
            m.result      AS market_result,
            l.match_id,
            l.side        AS link_side,
            l.confidence,
            mt.date       AS match_date,
            mt.team_a_id,
            mt.team_b_id,
            mt.series_winner_id
        FROM kalshi_candles c
        JOIN kalshi_markets m ON m.market_ticker = c.market_ticker
        JOIN market_match_links l ON l.market_ticker = c.market_ticker
        JOIN matches mt ON mt.match_id = l.match_id
        WHERE m.series_ticker = 'KXLOLGAME'
          AND l.confidence >= 0.7
          AND mt.series_winner_id IS NOT NULL
          AND date(c.end_period_ts, 'unixepoch') >= ?
          AND date(c.end_period_ts, 'unixepoch') <= ?
          AND c.yes_ask_close_cents BETWEEN ? AND ?
          AND c.yes_bid_close_cents >= 1
        ORDER BY c.end_period_ts ASC
        """,
        (start_date, end_date, *LIVE_PRICE_RANGE_CENTS),
    ).fetchall()


def _model_yes_prob_for_market(model_p_team_a: float, link_side: int | None) -> float:
    """Translate the model's P(team_a wins) into P(this market's YES wins).

    Our model predicts P(team_a wins the series). Kalshi's YES contract
    might resolve on either team:
      link_side == 1: YES = team_a wins -> p_yes = p_team_a
      link_side == 2: YES = team_b wins -> p_yes = 1 - p_team_a
    """
    if link_side == 1:
        return model_p_team_a
    if link_side == 2:
        return 1.0 - model_p_team_a
    # Unknown / totals — caller should not have included this market
    return float("nan")


def _clear_existing_backtest_rows(
    conn: sqlite3.Connection, start_date: str, end_date: str
) -> int:
    """Idempotency: remove any prior backtest decisions/paper_trades in
    this window. Returns count of rows deleted."""
    start_ts = int(datetime.fromisoformat(start_date).timestamp())
    end_ts = int(datetime.fromisoformat(end_date).timestamp()) + 86400  # end-of-day
    # Find decision_ids first so we can delete paper_trades by FK
    rows = conn.execute(
        """
        SELECT decision_id FROM decisions
        WHERE made_by = 'backtest' AND decision_ts BETWEEN ? AND ?
        """,
        (start_ts, end_ts),
    ).fetchall()
    ids = [r["decision_id"] for r in rows]
    if not ids:
        return 0
    placeholders = ",".join("?" * len(ids))
    conn.execute(f"DELETE FROM paper_trades WHERE decision_id IN ({placeholders})", ids)
    conn.execute(f"DELETE FROM decisions WHERE decision_id IN ({placeholders})", ids)
    conn.commit()
    log.info("Cleared %d prior backtest decisions in window", len(ids))
    return len(ids)


def run_backtest(
    conn: sqlite3.Connection,
    model: Model,
    start_date: str,
    end_date: str,
    starting_bankroll_cents: int = 200_000,        # $2,000
    base_edge_threshold: float = DEFAULT_BASE_EDGE_THRESHOLD,
    kelly_fraction: float = 0.25,
    persist_to_db: bool = False,
) -> BacktestResult:
    """Run the v1 backtest over the date window.

    Args:
        persist_to_db: if True, write decisions + paper_trades rows with
            ``made_by='backtest'`` so the dashboard and trade-count metrics
            include these samples. Idempotent — clears prior backtest rows
            for the same window before inserting.

    Returns a BacktestResult with the closed portfolio + skip counters.
    """
    portfolio = Portfolio(
        starting_bankroll_cents=starting_bankroll_cents,
        kelly_fraction=kelly_fraction,
    )

    if persist_to_db:
        _clear_existing_backtest_rows(conn, start_date, end_date)

    candles = _eligible_candles_for_backtest(conn, start_date, end_date)
    log.info("Backtest window %s -> %s: %d eligible candles", start_date, end_date, len(candles))

    model_version = model.metadata.get("trained_at_utc", "unknown")

    seen_markets: set[str] = set()
    settled_pending: dict[str, Position] = {}  # market_ticker -> open Position

    decisions = 0
    skipped_no_edge = 0
    skipped_no_link = 0
    skipped_already = 0
    skipped_no_features = 0
    skipped_cap = 0
    trade_log: list[dict] = []

    for c in candles:
        mt = c["market_ticker"]
        if mt in seen_markets:
            skipped_already += 1
            continue

        # Compute features as_of the candle's date (no leak)
        candle_date = datetime.utcfromtimestamp(c["end_period_ts"]).strftime("%Y-%m-%d")
        # Strict: candle_date must be on or before the match_date. If the
        # candle is later than the match (post-game candle), the match
        # already happened and our feature query would be valid but the
        # trade itself wouldn't be — skip these.
        if candle_date > c["match_date"]:
            continue
        try:
            feats = compute_features(conn, c["match_id"], as_of_date=candle_date)
        except Exception as e:
            log.debug("Feature computation failed for match %s: %s", c["match_id"], e)
            skipped_no_features += 1
            seen_markets.add(mt)
            continue

        decisions += 1

        pred = model.predict_dict(feats)
        model_p_team_a = pred.yes_prob
        p_yes = _model_yes_prob_for_market(model_p_team_a, c["link_side"])
        if p_yes != p_yes:  # NaN check
            seen_markets.add(mt)
            continue

        # Threshold scales with uncertainty band
        threshold = base_edge_threshold + UNCERTAINTY_PENALTY * (pred.p90 - pred.p10)

        yes_ask = c["yes_ask_close_cents"] / 100.0
        yes_bid = c["yes_bid_close_cents"] / 100.0
        edge_yes = p_yes - yes_ask
        edge_no = yes_bid - p_yes

        side = None
        if edge_yes > threshold:
            side = "YES"
            entry_price_cents = c["yes_ask_close_cents"] + slippage_cents()
            edge_at_entry = edge_yes
        elif edge_no > threshold:
            side = "NO"
            # Buying NO at (100 - yes_bid) cents = same as selling YES at yes_bid
            entry_price_cents = (100 - c["yes_bid_close_cents"]) + slippage_cents()
            edge_at_entry = edge_no
        else:
            skipped_no_edge += 1
            if persist_to_db:
                log_decision(
                    conn,
                    market_ticker=mt, match_id=c["match_id"],
                    model_version=model_version,
                    model_prob=p_yes, p10=pred.p10, p90=pred.p90,
                    market_yes_bid_cents=c["yes_bid_close_cents"],
                    market_yes_ask_cents=c["yes_ask_close_cents"],
                    edge=max(edge_yes, edge_no),
                    edge_threshold=threshold,
                    action="HOLD", gate_reason="edge_below_threshold",
                    made_by="backtest",
                    decision_ts=int(c["end_period_ts"]),
                )
            seen_markets.add(mt)
            continue

        # Position sizing
        desired_contracts = portfolio.kelly_size_contracts(side, p_yes, entry_price_cents)
        if desired_contracts <= 0:
            skipped_cap += 1
            seen_markets.add(mt)
            continue

        # Trim down to risk gates if needed
        def fee_for(n: int) -> int:
            return kalshi_fee_cents(entry_price_cents, n)

        allowed, reason = portfolio.cap_contracts(side, desired_contracts, entry_price_cents, fee_for)
        if allowed <= 0:
            skipped_cap += 1
            seen_markets.add(mt)
            continue

        entry_fee = fee_for(allowed)
        ok, gate_reason = portfolio.can_open(side, allowed, entry_price_cents, entry_fee)
        if not ok:
            skipped_cap += 1
            seen_markets.add(mt)
            continue

        # Open the position
        pos = Position(
            market_ticker=mt,
            match_id=c["match_id"],
            side=side,
            contracts=allowed,
            entry_price_cents=entry_price_cents,
            entry_fee_cents=entry_fee,
            entry_date=candle_date,
            model_prob=p_yes,
            market_implied=yes_ask if side == "YES" else (1.0 - yes_bid),
            edge=edge_at_entry,
            p10=pred.p10,
            p90=pred.p90,
        )
        portfolio.open_position(pos)
        settled_pending[mt] = pos
        seen_markets.add(mt)

        # Optionally persist to DB so the dashboard sees these trades
        if persist_to_db:
            decision_id = log_decision(
                conn,
                market_ticker=mt, match_id=c["match_id"],
                model_version=model_version,
                model_prob=p_yes, p10=pred.p10, p90=pred.p90,
                market_yes_bid_cents=c["yes_bid_close_cents"],
                market_yes_ask_cents=c["yes_ask_close_cents"],
                edge=edge_at_entry, edge_threshold=threshold,
                action=f"BUY_{side}",
                gate_reason=None,
                made_by="backtest",
                decision_ts=int(c["end_period_ts"]),
            )
            fill = execute_paper_fill(
                conn,
                decision_id=decision_id,
                side=side,
                contracts=allowed,
                market_yes_ask_cents=c["yes_ask_close_cents"],
                market_yes_bid_cents=c["yes_bid_close_cents"],
                fill_ts=int(c["end_period_ts"]),
            )
            # Stash trade_id so we can settle it below
            pos.trade_id = fill.trade_id  # type: ignore

        trade_log.append({
            "candle_ts": int(c["end_period_ts"]),
            "candle_date": candle_date,
            "match_id": c["match_id"],
            "market_ticker": mt,
            "side": side,
            "contracts": allowed,
            "entry_price_cents": entry_price_cents,
            "entry_fee_cents": entry_fee,
            "model_prob": p_yes,
            "p10": pred.p10,
            "p90": pred.p90,
            "edge_at_entry": edge_at_entry,
            "threshold": threshold,
        })

    # Settle every open position based on the recorded series_winner
    for c in candles:
        mt = c["market_ticker"]
        if mt not in settled_pending:
            continue
        pos = settled_pending.pop(mt)
        team_a_won = c["series_winner_id"] == c["team_a_id"]
        # link_side semantics: 1 -> YES is team_a; 2 -> YES is team_b
        if c["link_side"] == 1:
            yes_won = team_a_won
        else:
            yes_won = not team_a_won
        portfolio.settle_position(pos, yes_won, c["match_date"])

        # If we persisted to DB, settle there too
        if persist_to_db and hasattr(pos, "trade_id"):
            settled_ts = int(datetime.fromisoformat(c["match_date"]).timestamp())
            settle_paper_trade(
                conn,
                trade_id=pos.trade_id,
                yes_won=yes_won,
                settled_ts=settled_ts,
            )

    log.info(
        "Backtest done: %d decisions, %d trades, "
        "skipped: no_edge=%d no_link=%d already=%d no_features=%d cap=%d",
        decisions, len(trade_log),
        skipped_no_edge, skipped_no_link, skipped_already,
        skipped_no_features, skipped_cap,
    )

    return BacktestResult(
        portfolio=portfolio,
        decisions_considered=decisions,
        skipped_no_edge=skipped_no_edge,
        skipped_no_link=skipped_no_link,
        skipped_already_traded=skipped_already,
        skipped_no_features=skipped_no_features,
        skipped_cap=skipped_cap,
        trade_log=trade_log,
    )
