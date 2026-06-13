"""Trade journal — log manual Kalshi trades against model fair, measure edge.

The measurement loop. Decision-support means the user places trades manually;
this records what the model said vs what they did vs what happened, so that
after enough trades we can answer the only question that matters: *is the
realized edge real?* Entry edge that doesn't convert to realized PnL means the
model's fair values aren't trustworthy yet (or sizing/exits are leaking it).

Usage (from dashboard or REPL):
    jid = log_entry(conn, game_id=..., side="blue", model_fair=0.55,
                    market_c=48, leverage=0.3, contracts=100, entry_price_c=48,
                    rec_contracts=120)
    ...
    log_exit(conn, jid, model_fair=0.72, market_c=70, exit_price_c=70,
             reason="take_profit", triggers=["opponent_baron_active"],
             flagged_exit_held=False)
    ...
    print(summarize(conn))
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass


def log_entry(
    conn: sqlite3.Connection,
    *,
    side: str,
    model_fair: float,
    market_c: float,
    contracts: int,
    entry_price_c: int,
    game_id: str | None = None,
    ticker: str | None = None,
    leverage: float | None = None,
    rec_contracts: int | None = None,
    entry_minute: float | None = None,
    now_ts: int | None = None,
) -> int:
    """Record a trade entry. Returns journal_id. All probs oriented to `side`."""
    now_ts = now_ts or int(time.time())
    edge = model_fair - market_c / 100.0
    cur = conn.execute(
        """
        INSERT INTO trade_journal (
            created_at, game_id, ticker, side,
            entry_ts, entry_minute, model_fair_entry, market_entry_c,
            edge_entry, leverage_entry, contracts, entry_price_c, rec_contracts
        ) VALUES (?,?,?,?, ?,?,?,?, ?,?,?,?,?)
        """,
        (now_ts, game_id, ticker, side,
         now_ts, entry_minute, model_fair, market_c,
         edge, leverage, contracts, entry_price_c, rec_contracts),
    )
    conn.commit()
    return int(cur.lastrowid)


def log_exit(
    conn: sqlite3.Connection,
    journal_id: int,
    *,
    exit_price_c: int,
    reason: str,
    model_fair: float | None = None,
    market_c: float | None = None,
    triggers: list[str] | None = None,
    flagged_exit_held: bool = False,
    exit_minute: float | None = None,
    settled_value_c: int | None = None,
    notes: str | None = None,
    now_ts: int | None = None,
) -> None:
    """Close a journaled trade + compute realized PnL.

    realized_pnl_c = (exit_or_settle - entry_price) * contracts.
    If settled_value_c is given (held to settlement: 100 win / 0 loss), it takes
    precedence over exit_price_c for PnL.
    """
    now_ts = now_ts or int(time.time())
    row = conn.execute(
        "SELECT entry_price_c, contracts FROM trade_journal WHERE journal_id=?",
        (journal_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"journal_id {journal_id} not found")
    entry_price_c = row["entry_price_c"]
    contracts = row["contracts"]
    close_value = settled_value_c if settled_value_c is not None else exit_price_c
    realized = int((close_value - entry_price_c) * contracts)

    conn.execute(
        """
        UPDATE trade_journal SET
            exit_ts=?, exit_minute=?, model_fair_exit=?, market_exit_c=?,
            exit_price_c=?, exit_reason=?, triggers_at_exit=?,
            flagged_exit_held=?, realized_pnl_c=?, settled_value_c=?, notes=?
        WHERE journal_id=?
        """,
        (now_ts, exit_minute, model_fair, market_c, exit_price_c, reason,
         ",".join(triggers or []), 1 if flagged_exit_held else 0,
         realized, settled_value_c, notes, journal_id),
    )
    conn.commit()


@dataclass
class JournalSummary:
    n_trades: int
    n_closed: int
    n_wins: int
    win_rate: float
    total_pnl_c: int
    avg_edge_entry: float          # mean model edge at entry (prob units)
    realized_edge_c: float         # mean realized PnL per contract (cents)
    edge_capture: float            # realized_edge / expected_edge (1.0 = full)
    n_flagged_exit_held: int       # times system said exit, user held
    flagged_held_pnl_c: int        # PnL on those — the discipline cost/benefit
    by_exit_reason: dict[str, int] # count per exit reason

    def render(self) -> str:
        lines = [
            "=== Trade Journal Summary ===",
            f"  trades: {self.n_trades} ({self.n_closed} closed)",
            f"  win rate: {self.win_rate:.0%}  ({self.n_wins}/{self.n_closed})",
            f"  total PnL: {self.total_pnl_c/100:+.2f}",
            f"  avg entry edge: {self.avg_edge_entry:+.1%} (prob)",
            f"  realized edge: {self.realized_edge_c:+.2f}c / contract",
            f"  edge capture: {self.edge_capture:.0%} "
            f"(realized vs model-expected; <100% = leaking edge in sizing/exits)",
            f"  flagged-exit-but-held: {self.n_flagged_exit_held} trades, "
            f"PnL {self.flagged_held_pnl_c/100:+.2f} "
            f"(the discipline ledger — negative means holding past the flag cost you)",
            f"  exits by reason: {self.by_exit_reason}",
        ]
        return "\n".join(lines)


def summarize(conn: sqlite3.Connection) -> JournalSummary:
    """Aggregate journaled trades into the metrics that reveal real edge."""
    rows = conn.execute("SELECT * FROM trade_journal").fetchall()
    n_trades = len(rows)
    closed = [r for r in rows if r["realized_pnl_c"] is not None]
    n_closed = len(closed)
    n_wins = sum(1 for r in closed if (r["realized_pnl_c"] or 0) > 0)
    total_pnl = sum(int(r["realized_pnl_c"] or 0) for r in closed)
    avg_edge = (sum(float(r["edge_entry"] or 0) for r in rows) / n_trades
                if n_trades else 0.0)

    # realized edge per contract (cents): total PnL / total contracts closed
    total_contracts = sum(int(r["contracts"]) for r in closed) or 1
    realized_edge_c = total_pnl / total_contracts

    # edge capture: realized cents/contract vs model-expected cents/contract.
    # expected per contract = edge_entry * 100 (prob edge -> cents on a $1 binary)
    exp_edge_c = (sum(float(r["edge_entry"] or 0) * 100 * int(r["contracts"])
                      for r in closed) / total_contracts) if closed else 0.0
    edge_capture = (realized_edge_c / exp_edge_c) if exp_edge_c > 0 else 0.0

    flagged = [r for r in closed if r["flagged_exit_held"]]
    flagged_pnl = sum(int(r["realized_pnl_c"] or 0) for r in flagged)

    by_reason: dict[str, int] = {}
    for r in closed:
        reason = r["exit_reason"] or "unknown"
        by_reason[reason] = by_reason.get(reason, 0) + 1

    return JournalSummary(
        n_trades=n_trades, n_closed=n_closed, n_wins=n_wins,
        win_rate=(n_wins / n_closed if n_closed else 0.0),
        total_pnl_c=total_pnl, avg_edge_entry=avg_edge,
        realized_edge_c=realized_edge_c, edge_capture=edge_capture,
        n_flagged_exit_held=len(flagged), flagged_held_pnl_c=flagged_pnl,
        by_exit_reason=by_reason,
    )
