"""The trader decision loop.

Runs as a long-lived process. Every iteration:
  1. Refresh market state from Kalshi (current bid/ask for relevant markets)
  2. Settle any newly-resolved paper trades
  3. Check kill state; soft/hard/emergency -> halt new entries
  4. For each tradable market, compute features + predict + decide + log
  5. Place paper-fills for decisions that pass all gates
  6. Sleep, repeat

v1 implementation uses REST polling (every N seconds). Phase 4 will swap
this for WebSocket-pushed updates.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from loltrader.backtest.fees import kalshi_fee_cents, slippage_cents
from loltrader.backtest.portfolio import Portfolio
from loltrader.backtest.sim import _model_yes_prob_for_market
from loltrader.features import compute_features
from loltrader.kalshi.rest import KalshiClient, KalshiRestError
from loltrader.model.serve import Model
from loltrader.trader.gates import (
    GateInputs,
    daily_pnl_for_session,
    session_pnl,
    validate_decision,
)
from loltrader.trader.killswitch import (
    KillLevel,
    KillState,
    evaluate_kill_state,
)
from loltrader.trader.paper import (
    close_session,
    execute_paper_fill,
    log_decision,
    open_session,
    settle_resolved_markets,
)

log = logging.getLogger(__name__)

ALLOWED_SERIES = ("KXLOLGAME",)        # v1: series winner only
DEFAULT_BASE_EDGE_THRESHOLD = 0.03
UNCERTAINTY_PENALTY = 0.5
MAX_UNCERTAINTY = 0.40
TRADING_WINDOW_HOURS_BEFORE_CLOSE = 24  # only act on markets closing within 24h


@dataclass
class TraderConfig:
    starting_bankroll_cents: int = 200_000
    base_edge_threshold: float = DEFAULT_BASE_EDGE_THRESHOLD
    kelly_fraction: float = 0.25
    poll_interval_sec: int = 30
    max_iterations: int | None = None     # None = run forever
    dry_run: bool = True                  # paper trade only
    kill_file_path: Path = Path("data/KILL_SWITCH")


def _candidate_markets(conn: sqlite3.Connection, now_ts: int, horizon_hours: int) -> list[sqlite3.Row]:
    """Markets that are open, linked confidently, and closing within horizon."""
    horizon_ts = now_ts + horizon_hours * 3600
    return conn.execute(
        """
        SELECT
            m.market_ticker, m.event_ticker, m.series_ticker,
            m.yes_bid_cents, m.yes_ask_cents, m.close_time_unix,
            m.last_seen_at,
            l.match_id, l.side AS link_side, l.confidence,
            mt.team_a_id, mt.team_b_id
        FROM kalshi_markets m
        JOIN market_match_links l ON l.market_ticker = m.market_ticker
        JOIN matches mt ON mt.match_id = l.match_id
        WHERE m.status IN ('active', 'open')
          AND m.series_ticker IN ('KXLOLGAME')
          AND l.confidence >= 0.7
          AND m.close_time_unix BETWEEN ? AND ?
          AND m.yes_ask_cents BETWEEN 5 AND 95
          AND m.yes_bid_cents BETWEEN 1 AND 95
        """,
        (now_ts, horizon_ts),
    ).fetchall()


def _has_open_position(conn: sqlite3.Connection, market_ticker: str) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM paper_trades t
        JOIN decisions d ON d.decision_id = t.decision_id
        WHERE d.market_ticker = ? AND t.closed_at IS NULL
        LIMIT 1
        """,
        (market_ticker,),
    ).fetchone()
    return row is not None


def trader_iteration(
    conn: sqlite3.Connection,
    client: KalshiClient,
    model: Model,
    cfg: TraderConfig,
    session_id: int,
    portfolio: Portfolio,
    last_data_seen_ts: int,
) -> tuple[KillState, int, int]:
    """Run one iteration of the trader. Returns (kill_state, new_data_ts, n_decisions)."""
    now_ts = int(time.time())

    # 1. Settle any newly-resolved markets
    n_settled = settle_resolved_markets(conn)
    if n_settled:
        log.info("Settled %d resolved markets", n_settled)

    # 2. Kill switch check
    daily_pnl = daily_pnl_for_session(conn, now_ts=now_ts, session_started_at=0)
    sess_pnl = session_pnl(conn, session_started_at=0)
    kill = evaluate_kill_state(
        daily_pnl_cents=daily_pnl,
        session_pnl_cents=sess_pnl,
        starting_bankroll_cents=cfg.starting_bankroll_cents,
        last_data_seen_ts=last_data_seen_ts,
        now_ts=now_ts,
        kill_file=cfg.kill_file_path,
    )
    if kill.level != KillLevel.NONE:
        log.warning("Kill state: %s (%s)", kill.level.value, kill.reason)
        if kill.level in (KillLevel.HARD, KillLevel.EMERGENCY):
            return kill, last_data_seen_ts, 0

    # 3. Pull fresh market state for relevant tickers
    candidates = _candidate_markets(conn, now_ts, TRADING_WINDOW_HOURS_BEFORE_CLOSE)
    log.debug("Found %d candidate markets within trading window", len(candidates))
    if not candidates:
        return kill, last_data_seen_ts, 0

    # For v1: read prices from the DB (the daily logger refreshes them).
    # Phase 4 will swap this for real-time WS data.
    new_data_ts = last_data_seen_ts
    n_decisions = 0
    model_version = model.metadata.get("trained_at_utc", "unknown")

    for c in candidates:
        mt = c["market_ticker"]
        if _has_open_position(conn, mt):
            continue
        if c["yes_ask_cents"] is None or c["yes_bid_cents"] is None:
            continue

        # Compute features as_of today
        today = datetime.utcfromtimestamp(now_ts).strftime("%Y-%m-%d")
        try:
            feats = compute_features(conn, c["match_id"], as_of_date=today)
        except Exception as e:
            log.debug("Feature computation failed for match %s: %s", c["match_id"], e)
            continue

        pred = model.predict_dict(feats)
        p_yes = _model_yes_prob_for_market(pred.yes_prob, c["link_side"])
        threshold = cfg.base_edge_threshold + UNCERTAINTY_PENALTY * (pred.p90 - pred.p10)

        yes_ask = c["yes_ask_cents"] / 100.0
        yes_bid = c["yes_bid_cents"] / 100.0
        edge_yes = p_yes - yes_ask
        edge_no = yes_bid - p_yes

        if edge_yes > threshold:
            side, edge = "YES", edge_yes
            entry_price = c["yes_ask_cents"] + slippage_cents()
            action = "BUY_YES"
        elif edge_no > threshold:
            side, edge = "NO", edge_no
            entry_price = (100 - c["yes_bid_cents"]) + slippage_cents()
            action = "BUY_NO"
        else:
            log_decision(
                conn,
                market_ticker=mt, match_id=c["match_id"],
                model_version=model_version,
                model_prob=p_yes, p10=pred.p10, p90=pred.p90,
                market_yes_bid_cents=c["yes_bid_cents"],
                market_yes_ask_cents=c["yes_ask_cents"],
                edge=max(edge_yes, edge_no),
                edge_threshold=threshold,
                action="HOLD", gate_reason="edge_below_threshold",
            )
            n_decisions += 1
            continue

        # Position sizing
        contracts = portfolio.kelly_size_contracts(side, p_yes, entry_price)
        if contracts <= 0:
            log_decision(
                conn,
                market_ticker=mt, match_id=c["match_id"],
                model_version=model_version,
                model_prob=p_yes, p10=pred.p10, p90=pred.p90,
                market_yes_bid_cents=c["yes_bid_cents"],
                market_yes_ask_cents=c["yes_ask_cents"],
                edge=edge, edge_threshold=threshold,
                action="HOLD", gate_reason="kelly_zero",
            )
            n_decisions += 1
            continue

        # Trim to caps
        def fee_for(n: int) -> int:
            return kalshi_fee_cents(entry_price, n)

        allowed, reason = portfolio.cap_contracts(side, contracts, entry_price, fee_for)
        if allowed <= 0:
            log_decision(
                conn,
                market_ticker=mt, match_id=c["match_id"],
                model_version=model_version,
                model_prob=p_yes, p10=pred.p10, p90=pred.p90,
                market_yes_bid_cents=c["yes_bid_cents"],
                market_yes_ask_cents=c["yes_ask_cents"],
                edge=edge, edge_threshold=threshold,
                action="HOLD", gate_reason=f"cap_{reason}",
            )
            n_decisions += 1
            continue
        entry_fee = fee_for(allowed)

        # Full gate validation
        cost = allowed * entry_price + entry_fee
        gates = GateInputs(
            bankroll_cents=portfolio.bankroll_cents,
            current_exposure_cents=portfolio.open_exposure_cents,
            proposed_position_cost_cents=cost,
            daily_pnl_cents=daily_pnl,
            total_session_pnl_cents=sess_pnl,
            starting_bankroll_cents=cfg.starting_bankroll_cents,
            model_uncertainty=pred.p90 - pred.p10,
            max_uncertainty=MAX_UNCERTAINTY,
            edge=edge,
            edge_threshold=threshold,
            last_data_seen_ts=last_data_seen_ts,
            now_ts=now_ts,
            market_close_unix=c["close_time_unix"],
            link_confidence=c["confidence"],
            series_ticker=c["series_ticker"],
            allowed_series=ALLOWED_SERIES,
        )
        fail = validate_decision(gates)
        if fail:
            log_decision(
                conn,
                market_ticker=mt, match_id=c["match_id"],
                model_version=model_version,
                model_prob=p_yes, p10=pred.p10, p90=pred.p90,
                market_yes_bid_cents=c["yes_bid_cents"],
                market_yes_ask_cents=c["yes_ask_cents"],
                edge=edge, edge_threshold=threshold,
                action="HOLD", gate_reason=fail,
            )
            n_decisions += 1
            continue

        # All checks passed — log decision + simulate fill (only if not SOFT-killed)
        if kill.level == KillLevel.SOFT:
            log_decision(
                conn,
                market_ticker=mt, match_id=c["match_id"],
                model_version=model_version,
                model_prob=p_yes, p10=pred.p10, p90=pred.p90,
                market_yes_bid_cents=c["yes_bid_cents"],
                market_yes_ask_cents=c["yes_ask_cents"],
                edge=edge, edge_threshold=threshold,
                action="HOLD", gate_reason=f"soft_kill:{kill.reason}",
            )
            n_decisions += 1
            continue

        decision_id = log_decision(
            conn,
            market_ticker=mt, match_id=c["match_id"],
            model_version=model_version,
            model_prob=p_yes, p10=pred.p10, p90=pred.p90,
            market_yes_bid_cents=c["yes_bid_cents"],
            market_yes_ask_cents=c["yes_ask_cents"],
            edge=edge, edge_threshold=threshold,
            action=action, gate_reason=None,
        )
        fill = execute_paper_fill(
            conn,
            decision_id=decision_id,
            side=side,
            contracts=allowed,
            market_yes_ask_cents=c["yes_ask_cents"],
            market_yes_bid_cents=c["yes_bid_cents"],
        )
        # Update portfolio in-memory
        from loltrader.backtest.portfolio import Position
        pos = Position(
            market_ticker=mt, match_id=c["match_id"], side=side,
            contracts=allowed, entry_price_cents=fill.fill_price_cents,
            entry_fee_cents=fill.entry_fee_cents, entry_date=today,
            model_prob=p_yes, market_implied=yes_ask if side == "YES" else (1.0 - yes_bid),
            edge=edge, p10=pred.p10, p90=pred.p90,
        )
        portfolio.open_position(pos)
        n_decisions += 1
        log.info("PAPER %s %d @ $%.2f on %s (edge=%.3f)",
                 action, allowed, fill.fill_price_cents / 100, mt, edge)
        new_data_ts = max(new_data_ts, c["last_seen_at"] or 0)

    return kill, new_data_ts, n_decisions


def run_trader(cfg: TraderConfig, model_path: Path, db_path: Path | None = None) -> int:
    """Main trader entry point. Returns exit code."""
    from loltrader.db import connect, migrate
    conn = connect(db_path)
    migrate(conn)

    client = KalshiClient()
    model = Model.load(model_path)
    portfolio = Portfolio(
        starting_bankroll_cents=cfg.starting_bankroll_cents,
        kelly_fraction=cfg.kelly_fraction,
    )
    session_id = open_session(conn, cfg.starting_bankroll_cents)
    log.info("Bot session %d opened, starting bankroll $%.2f",
             session_id, cfg.starting_bankroll_cents / 100)

    last_data_seen_ts = int(time.time())
    iterations = 0
    end_reason = "manual"
    try:
        while True:
            kill, last_data_seen_ts, n = trader_iteration(
                conn, client, model, cfg, session_id, portfolio, last_data_seen_ts
            )
            log.info("Iteration %d: %d decisions, kill_state=%s",
                     iterations, n, kill.level.value)
            if kill.level == KillLevel.EMERGENCY:
                end_reason = "emergency_kill"
                break
            if kill.level == KillLevel.HARD:
                end_reason = "hard_kill"
                break
            iterations += 1
            if cfg.max_iterations and iterations >= cfg.max_iterations:
                end_reason = "max_iterations"
                break
            time.sleep(cfg.poll_interval_sec)
    except KeyboardInterrupt:
        log.info("Interrupt received, shutting down")
        end_reason = "interrupt"
    finally:
        close_session(conn, session_id, end_reason)
        log.info("Session %d closed: %s", session_id, end_reason)
        conn.close()
    return 0
