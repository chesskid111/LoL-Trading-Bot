"""Pre-trade risk gates from spec section 9.

Every decision goes through ``validate_decision``. It returns the first
failing gate name (a string) or None if all gates pass.
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime


@dataclass
class GateInputs:
    """All inputs needed to evaluate the risk gates for one decision."""
    bankroll_cents: int
    current_exposure_cents: int
    proposed_position_cost_cents: int
    daily_pnl_cents: int
    total_session_pnl_cents: int
    starting_bankroll_cents: int
    model_uncertainty: float       # p90 - p10
    max_uncertainty: float         # config
    edge: float
    edge_threshold: float
    last_data_seen_ts: int         # Unix seconds of last fresh price snapshot
    now_ts: int                    # Unix seconds
    market_close_unix: int | None
    link_confidence: float
    link_confidence_threshold: float = 0.7
    series_ticker: str = ""
    allowed_series: tuple[str, ...] = ("KXLOLGAME", "KXLOLMAP", "KXLOLTOTALMAPS")
    max_position_pct: float = 0.05
    max_total_exposure_pct: float = 0.20
    daily_stop_loss_pct: float = 0.10
    session_stop_loss_pct: float = 0.30
    max_data_staleness_sec: int = 5
    max_days_to_resolution: int = 14


def validate_decision(g: GateInputs) -> str | None:
    """Return None if all gates pass; else the first failing gate name."""
    # Series whitelist
    if g.series_ticker not in g.allowed_series:
        return "unknown_series"

    # Linkage confidence
    if g.link_confidence < g.link_confidence_threshold:
        return "low_link_confidence"

    # Edge / uncertainty
    if g.model_uncertainty > g.max_uncertainty:
        return "uncertainty_too_high"
    if g.edge <= g.edge_threshold:
        return "edge_below_threshold"

    # Balance
    if g.bankroll_cents <= 0:
        return "balance_zero"
    if g.proposed_position_cost_cents > g.bankroll_cents:
        return "insufficient_balance"

    # Position size caps
    per_market_cap = int(g.starting_bankroll_cents * g.max_position_pct)
    if g.proposed_position_cost_cents > per_market_cap:
        return "exceeds_per_market_cap"

    total_cap = int(g.starting_bankroll_cents * g.max_total_exposure_pct)
    if g.current_exposure_cents + g.proposed_position_cost_cents > total_cap:
        return "exceeds_total_exposure_cap"

    # Stop-losses
    daily_floor = -int(g.starting_bankroll_cents * g.daily_stop_loss_pct)
    if g.daily_pnl_cents < daily_floor:
        return "daily_stop_loss_breached"

    session_floor = -int(g.starting_bankroll_cents * g.session_stop_loss_pct)
    if g.total_session_pnl_cents < session_floor:
        return "session_stop_loss_breached"

    # Data freshness
    if g.now_ts - g.last_data_seen_ts > g.max_data_staleness_sec:
        return "data_stale"

    # Time to resolution
    if g.market_close_unix is None:
        return "no_close_time"
    if g.market_close_unix <= g.now_ts:
        return "market_already_closed"
    if g.market_close_unix - g.now_ts > g.max_days_to_resolution * 86400:
        return "market_resolves_too_far_out"

    return None


def daily_pnl_for_session(
    conn: sqlite3.Connection, session_started_at: int, now_ts: int | None = None,
) -> int:
    """Compute PnL for the current calendar day (UTC) within the session.
    Used by the daily-stop-loss gate."""
    now_ts = now_ts or int(time.time())
    today_start = datetime.utcfromtimestamp(now_ts).strftime("%Y-%m-%d")
    today_start_ts = int(datetime.strptime(today_start, "%Y-%m-%d").timestamp())
    row = conn.execute(
        """
        SELECT COALESCE(SUM(pnl_cents), 0) AS pnl
        FROM paper_trades
        WHERE closed_at IS NOT NULL
          AND closed_at >= ?
          AND closed_at >= ?
        """,
        (today_start_ts, session_started_at),
    ).fetchone()
    return int(row["pnl"]) if row else 0


def session_pnl(conn: sqlite3.Connection, session_started_at: int) -> int:
    """Total PnL within the session (across all calendar days)."""
    row = conn.execute(
        """
        SELECT COALESCE(SUM(pnl_cents), 0) AS pnl
        FROM paper_trades
        WHERE closed_at IS NOT NULL
          AND closed_at >= ?
        """,
        (session_started_at,),
    ).fetchone()
    return int(row["pnl"]) if row else 0
