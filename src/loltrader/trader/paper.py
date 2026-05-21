"""Paper-trade execution engine.

Persists decisions and paper-trade rows to SQLite. The real-money trader
will eventually replace this with a Kalshi order client, but the
interface stays the same.
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from typing import Literal

from loltrader.backtest.fees import kalshi_fee_cents, slippage_cents

Side = Literal["YES", "NO"]
Action = Literal["BUY_YES", "BUY_NO", "HOLD"]


@dataclass
class DecisionRecord:
    decision_id: int
    market_ticker: str
    action: Action
    gate_reason: str | None


@dataclass
class PaperFill:
    trade_id: int
    decision_id: int
    side: Side
    contracts: int
    fill_price_cents: int
    entry_fee_cents: int


def log_decision(
    conn: sqlite3.Connection,
    *,
    market_ticker: str,
    match_id: int | None,
    model_version: str,
    model_prob: float,
    p10: float,
    p90: float,
    market_yes_bid_cents: int | None,
    market_yes_ask_cents: int | None,
    edge: float,
    edge_threshold: float,
    action: Action,
    gate_reason: str | None,
    made_by: str = "bot",
    decision_ts: int | None = None,
) -> int:
    """Insert a decision row. Returns the new decision_id."""
    ts = decision_ts or int(time.time())
    cur = conn.execute(
        """
        INSERT INTO decisions
            (decision_ts, market_ticker, match_id, model_version,
             model_prob, p10, p90,
             market_yes_bid_cents, market_yes_ask_cents,
             edge, edge_threshold, action, gate_reason, made_by)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (ts, market_ticker, match_id, model_version,
         model_prob, p10, p90,
         market_yes_bid_cents, market_yes_ask_cents,
         edge, edge_threshold, action, gate_reason, made_by),
    )
    conn.commit()
    return int(cur.lastrowid)


def execute_paper_fill(
    conn: sqlite3.Connection,
    *,
    decision_id: int,
    side: Side,
    contracts: int,
    market_yes_ask_cents: int | None,
    market_yes_bid_cents: int | None,
    fill_ts: int | None = None,
) -> PaperFill:
    """Simulate an order fill at the current ask (for BUY_YES) or 100 - bid
    (for BUY_NO), with 1c slippage. Inserts paper_trades row."""
    ts = fill_ts or int(time.time())
    if side == "YES":
        if market_yes_ask_cents is None:
            raise ValueError("BUY_YES requires market_yes_ask_cents")
        fill_price = market_yes_ask_cents + slippage_cents()
    else:
        if market_yes_bid_cents is None:
            raise ValueError("BUY_NO requires market_yes_bid_cents")
        fill_price = (100 - market_yes_bid_cents) + slippage_cents()
    fee = kalshi_fee_cents(fill_price, contracts)

    cur = conn.execute(
        """
        INSERT INTO paper_trades
            (decision_id, opened_at, side, contracts, fill_price_cents, entry_fee_cents)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (decision_id, ts, side, contracts, fill_price, fee),
    )
    conn.commit()
    return PaperFill(
        trade_id=int(cur.lastrowid),
        decision_id=decision_id,
        side=side,
        contracts=contracts,
        fill_price_cents=fill_price,
        entry_fee_cents=fee,
    )


def settle_paper_trade(
    conn: sqlite3.Connection,
    *,
    trade_id: int,
    yes_won: bool,
    settled_ts: int | None = None,
) -> int:
    """Settle an open paper trade. Returns realized PnL in cents."""
    ts = settled_ts or int(time.time())
    row = conn.execute(
        """
        SELECT side, contracts, fill_price_cents, entry_fee_cents
        FROM paper_trades WHERE trade_id = ?
        """,
        (trade_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"No paper_trade with trade_id={trade_id}")
    side = row["side"]
    contracts = row["contracts"]
    fill_price = row["fill_price_cents"]
    entry_fee = row["entry_fee_cents"]

    if side == "YES":
        settle_value = 100 if yes_won else 0
    else:
        settle_value = 0 if yes_won else 100

    cost = contracts * fill_price + entry_fee
    revenue = contracts * settle_value
    pnl = revenue - cost

    conn.execute(
        """
        UPDATE paper_trades
        SET closed_at = ?, settle_value_cents = ?, pnl_cents = ?
        WHERE trade_id = ?
        """,
        (ts, settle_value, pnl, trade_id),
    )
    conn.commit()
    return pnl


def settle_resolved_markets(conn: sqlite3.Connection) -> int:
    """Find paper trades whose underlying market has settled but we haven't
    closed the trade yet. Settle each. Returns the number settled.

    Resolution comes from kalshi_markets.result (set by the corpus
    logger when Kalshi marks the market settled), cross-referenced with
    the linked match's series_winner.
    """
    rows = conn.execute(
        """
        SELECT
            t.trade_id,
            t.side,
            d.market_ticker,
            l.match_id,
            l.side AS link_side,
            mt.series_winner_id,
            mt.team_a_id,
            mt.team_b_id,
            mt.date AS settled_date,
            m.result AS kalshi_result
        FROM paper_trades t
        JOIN decisions d ON d.decision_id = t.decision_id
        JOIN market_match_links l ON l.market_ticker = d.market_ticker
        JOIN matches mt ON mt.match_id = l.match_id
        JOIN kalshi_markets m ON m.market_ticker = d.market_ticker
        WHERE t.closed_at IS NULL
          AND mt.series_winner_id IS NOT NULL
        """
    ).fetchall()
    n = 0
    for r in rows:
        team_a_won = (r["series_winner_id"] == r["team_a_id"])
        if r["link_side"] == 1:
            yes_won = team_a_won
        elif r["link_side"] == 2:
            yes_won = not team_a_won
        else:
            continue
        settle_paper_trade(conn, trade_id=r["trade_id"], yes_won=yes_won)
        n += 1
    return n


def open_session(conn: sqlite3.Connection, starting_bankroll_cents: int) -> int:
    """Open a new bot session, return session_id."""
    cur = conn.execute(
        "INSERT INTO bot_sessions (started_at, starting_bankroll_cents) VALUES (?, ?)",
        (int(time.time()), starting_bankroll_cents),
    )
    conn.commit()
    return int(cur.lastrowid)


def close_session(conn: sqlite3.Connection, session_id: int, end_reason: str) -> None:
    conn.execute(
        "UPDATE bot_sessions SET ended_at = ?, end_reason = ? WHERE session_id = ?",
        (int(time.time()), end_reason, session_id),
    )
    conn.commit()


def session_starting_bankroll(conn: sqlite3.Connection, session_id: int) -> int:
    row = conn.execute(
        "SELECT starting_bankroll_cents FROM bot_sessions WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    return int(row["starting_bankroll_cents"]) if row else 0
