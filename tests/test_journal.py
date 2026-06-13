"""Tests for the trade journal (loltrader.trader.journal)."""
from __future__ import annotations

import sqlite3

import pytest

from loltrader.trader import journal


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    # Apply just the journal table DDL
    from pathlib import Path
    ddl = (Path(__file__).parent.parent / "src" / "loltrader" / "db" /
           "migrations" / "016_trade_journal.sql").read_text()
    c.executescript(ddl)
    return c


def test_log_entry_computes_edge(conn):
    jid = journal.log_entry(conn, side="blue", model_fair=0.55, market_c=48,
                            contracts=100, entry_price_c=48, leverage=0.3,
                            rec_contracts=120, now_ts=1000)
    row = conn.execute("SELECT * FROM trade_journal WHERE journal_id=?", (jid,)).fetchone()
    assert abs(row["edge_entry"] - 0.07) < 1e-9   # 0.55 - 0.48
    assert row["rec_contracts"] == 120
    assert row["realized_pnl_c"] is None           # not closed yet


def test_log_exit_take_profit_pnl(conn):
    jid = journal.log_entry(conn, side="blue", model_fair=0.55, market_c=44,
                            contracts=100, entry_price_c=44, now_ts=1000)
    journal.log_exit(conn, jid, exit_price_c=64, reason="take_profit", now_ts=2000)
    row = conn.execute("SELECT * FROM trade_journal WHERE journal_id=?", (jid,)).fetchone()
    # (64 - 44) * 100 contracts = +2000c = +$20
    assert row["realized_pnl_c"] == 2000
    assert row["exit_reason"] == "take_profit"


def test_settled_value_takes_precedence(conn):
    jid = journal.log_entry(conn, side="blue", model_fair=0.6, market_c=50,
                            contracts=50, entry_price_c=50, now_ts=1000)
    # Held to settlement, side lost -> settle value 0
    journal.log_exit(conn, jid, exit_price_c=30, reason="settled",
                     settled_value_c=0, now_ts=3000)
    row = conn.execute("SELECT * FROM trade_journal WHERE journal_id=?", (jid,)).fetchone()
    # (0 - 50) * 50 = -2500, ignoring the exit_price_c=30
    assert row["realized_pnl_c"] == -2500


def test_flagged_exit_held_tracked(conn):
    """The discipline ledger: system said exit, user held, and it cost them."""
    jid = journal.log_entry(conn, side="blue", model_fair=0.50, market_c=52,
                            contracts=100, entry_price_c=52, now_ts=1000)
    journal.log_exit(conn, jid, exit_price_c=0, reason="settled",
                     settled_value_c=0, triggers=["coinflip"],
                     flagged_exit_held=True, now_ts=4000)
    s = journal.summarize(conn)
    assert s.n_flagged_exit_held == 1
    assert s.flagged_held_pnl_c == -5200   # (0-52)*100


def test_summarize_edge_capture(conn):
    # Two winning trades that realize roughly their model edge
    j1 = journal.log_entry(conn, side="blue", model_fair=0.60, market_c=50,
                           contracts=100, entry_price_c=50, now_ts=1000)
    journal.log_exit(conn, j1, exit_price_c=60, reason="manual", now_ts=2000)
    j2 = journal.log_entry(conn, side="red", model_fair=0.58, market_c=50,
                           contracts=100, entry_price_c=50, now_ts=1000)
    journal.log_exit(conn, j2, exit_price_c=56, reason="manual", now_ts=2000)

    s = journal.summarize(conn)
    assert s.n_closed == 2
    assert s.n_wins == 2
    assert s.win_rate == 1.0
    assert s.total_pnl_c == 1600          # +1000 + +600
    # avg entry edge = (0.10 + 0.08)/2 = 0.09 -> expected 9c/contract
    # realized = 1600 / 200 = 8c/contract -> capture ~0.89
    assert 0.8 < s.edge_capture < 1.0


def test_summary_render_runs(conn):
    journal.log_entry(conn, side="blue", model_fair=0.6, market_c=50,
                      contracts=10, entry_price_c=50, now_ts=1000)
    out = journal.summarize(conn).render()
    assert "Trade Journal Summary" in out
    assert "edge capture" in out
