"""Live-aid Streamlit dashboard for v1 trader.

Run with:
    python -m streamlit run src/loltrader/ui/app.py

Polls SQLite + Kalshi (read-only) every few seconds and renders:
  - Bot session status (PnL, bankroll, kill state)
  - Currently tradable markets with model probs vs market prices + edges
  - Open positions
  - Recent decisions log
  - Manual kill switch button
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

from loltrader.config import load_config
from loltrader.db import connect
from loltrader.features import compute_features
from loltrader.model.serve import Model

st.set_page_config(page_title="LoL Trader Dashboard", layout="wide")


@st.cache_resource
def _get_conn():
    return connect()


@st.cache_resource
def _get_model():
    cfg = load_config()
    path = cfg.models_dir / "v1_latest.pkl"
    if not path.exists():
        return None
    return Model.load(path)


def _session_status(conn) -> dict:
    s = conn.execute(
        "SELECT session_id, started_at, starting_bankroll_cents, ended_at, end_reason "
        "FROM bot_sessions ORDER BY session_id DESC LIMIT 1"
    ).fetchone()
    if not s:
        return {"session_id": None, "started_at": None, "starting_bankroll_cents": 0,
                "ended_at": None, "end_reason": None}
    return dict(s)


def _open_positions(conn) -> pd.DataFrame:
    rows = conn.execute(
        """
        SELECT
            t.trade_id, t.opened_at, t.side, t.contracts,
            t.fill_price_cents, t.entry_fee_cents,
            d.market_ticker, d.model_prob, d.edge
        FROM paper_trades t
        JOIN decisions d ON d.decision_id = t.decision_id
        WHERE t.closed_at IS NULL
        ORDER BY t.opened_at DESC
        """
    ).fetchall()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame([dict(r) for r in rows])
    df["opened_at"] = df["opened_at"].map(lambda t: datetime.utcfromtimestamp(t).strftime("%Y-%m-%d %H:%M"))
    df["fill_price"] = df["fill_price_cents"].map(lambda c: f"${c/100:.2f}")
    df["entry_fee"] = df["entry_fee_cents"].map(lambda c: f"${c/100:.2f}")
    df["cost_basis"] = (df["contracts"] * df["fill_price_cents"] + df["entry_fee_cents"]).map(
        lambda c: f"${c/100:.2f}"
    )
    return df[["opened_at", "market_ticker", "side", "contracts",
               "fill_price", "entry_fee", "cost_basis",
               "model_prob", "edge"]]


def _closed_pnl(conn) -> dict:
    row = conn.execute(
        """
        SELECT
            COUNT(*) AS n, SUM(pnl_cents) AS total_pnl,
            SUM(CASE WHEN pnl_cents > 0 THEN 1 ELSE 0 END) AS wins
        FROM paper_trades WHERE closed_at IS NOT NULL
        """
    ).fetchone()
    return dict(row) if row else {"n": 0, "total_pnl": 0, "wins": 0}


def _recent_decisions(conn, limit: int = 40) -> pd.DataFrame:
    rows = conn.execute(
        """
        SELECT
            d.decision_id, d.decision_ts, d.market_ticker, d.action,
            d.gate_reason, d.model_prob, d.p10, d.p90,
            d.market_yes_bid_cents, d.market_yes_ask_cents,
            d.edge, d.edge_threshold, d.made_by
        FROM decisions d
        ORDER BY d.decision_ts DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame([dict(r) for r in rows])
    df["time"] = df["decision_ts"].map(lambda t: datetime.utcfromtimestamp(t).strftime("%H:%M:%S"))
    df["bid/ask"] = df.apply(
        lambda r: f"{(r['market_yes_bid_cents'] or 0)/100:.2f}/{(r['market_yes_ask_cents'] or 0)/100:.2f}",
        axis=1,
    )
    return df[["time", "market_ticker", "action", "gate_reason",
               "model_prob", "edge", "edge_threshold", "bid/ask", "made_by"]]


def _upcoming_markets_with_predictions(conn, model: Model | None, limit: int = 20) -> pd.DataFrame:
    if model is None:
        return pd.DataFrame()
    from loltrader.kalshi.linkage import parse_ticker_game_time_unix
    import time as _time
    now = int(_time.time())
    horizon = now + 48 * 3600
    all_rows = conn.execute(
        """
        SELECT
            m.market_ticker, m.event_ticker, m.title,
            m.yes_bid_cents, m.yes_ask_cents, m.close_time_unix,
            l.match_id, l.side AS link_side,
            mt.team_a_id, mt.team_b_id
        FROM kalshi_markets m
        JOIN market_match_links l ON l.market_ticker = m.market_ticker
        JOIN matches mt ON mt.match_id = l.match_id
        WHERE m.status IN ('active', 'open')
          AND m.series_ticker = 'KXLOLGAME'
          AND l.confidence >= 0.7
          AND m.yes_ask_cents BETWEEN 5 AND 95
        """
    ).fetchall()
    # Filter by ticker-parsed game time, sort, take first N
    enriched = []
    for r in all_rows:
        gt = parse_ticker_game_time_unix(r["event_ticker"])
        if gt is None or not (now <= gt <= horizon):
            continue
        enriched.append((gt, r))
    enriched.sort(key=lambda x: x[0])
    rows = [r for _, r in enriched[:limit]]
    if not rows:
        return pd.DataFrame()

    today = datetime.utcfromtimestamp(now).strftime("%Y-%m-%d")
    out_rows = []
    for r in rows:
        # Parse ticker game time for display
        gt = parse_ticker_game_time_unix(r["event_ticker"])
        try:
            feats = compute_features(conn, r["match_id"], as_of_date=today)
            pred = model.predict_dict(feats)
            # Flip to market-perspective YES probability
            p_yes = pred.yes_prob if r["link_side"] == 1 else (1.0 - pred.yes_prob)
            p10 = pred.p10 if r["link_side"] == 1 else (1.0 - pred.p90)
            p90 = pred.p90 if r["link_side"] == 1 else (1.0 - pred.p10)
        except Exception as e:
            p_yes, p10, p90 = float("nan"), float("nan"), float("nan")

        ask = (r["yes_ask_cents"] or 0) / 100.0
        bid = (r["yes_bid_cents"] or 0) / 100.0
        edge_yes = p_yes - ask
        edge_no = bid - p_yes
        edge = max(edge_yes, edge_no) if p_yes == p_yes else float("nan")
        suggested = "BUY_YES" if edge_yes > 0.03 else ("BUY_NO" if edge_no > 0.03 else "HOLD")

        out_rows.append({
            "game_time": datetime.utcfromtimestamp(gt).strftime("%a %m-%d %H:%M UTC") if gt else "?",
            "title": r["title"][:60],
            "model": f"{p_yes:.2f}" if p_yes == p_yes else "n/a",
            "p10-p90": f"{p10:.2f}-{p90:.2f}" if p10 == p10 else "n/a",
            "bid": f"{bid:.2f}",
            "ask": f"{ask:.2f}",
            "edge": f"{edge:+.3f}" if edge == edge else "n/a",
            "suggested": suggested,
            "market_ticker": r["market_ticker"],
        })
    return pd.DataFrame(out_rows)


# ---------------------------- UI -----------------------------------------

st.title("LoL Trader Dashboard")

cfg = load_config()
conn = _get_conn()
model = _get_model()

# Session status banner
session = _session_status(conn)
pnl = _closed_pnl(conn)
total_pnl_cents = pnl["total_pnl"] or 0
starting = session["starting_bankroll_cents"] or 0
current_equity = starting + total_pnl_cents

col_a, col_b, col_c, col_d, col_e = st.columns(5)
col_a.metric("Session", str(session["session_id"]) if session["session_id"] else "—")
col_b.metric("Starting", f"${starting/100:,.2f}")
col_c.metric("Closed PnL", f"${total_pnl_cents/100:+,.2f}")
col_d.metric("Trades", f"{pnl['n']} ({pnl['wins'] or 0} W)")
col_e.metric("Model loaded", "yes" if model else "no")

# Kill switch
st.subheader("Manual kill switch")
kill_file = cfg.project_root / "data" / "KILL_SWITCH"
kill_present = kill_file.exists()
col1, col2 = st.columns(2)
if kill_present:
    col1.error(f"KILL_SWITCH file present at {kill_file}. Bot is SOFT-killed.")
    if col2.button("Remove kill file (resume)"):
        kill_file.unlink()
        st.success("Kill file removed. Bot will resume on next iteration.")
        st.rerun()
else:
    col1.success("No kill file. Bot can open new positions.")
    if col2.button("Trigger soft kill"):
        kill_file.parent.mkdir(parents=True, exist_ok=True)
        kill_file.touch()
        st.warning("Kill file written. Bot will stop opening new positions within ~5s.")
        st.rerun()

# Upcoming markets
st.subheader("Upcoming tradable markets (next 48h)")
upcoming = _upcoming_markets_with_predictions(conn, model)
if not upcoming.empty:
    st.dataframe(upcoming, hide_index=True, use_container_width=True)
else:
    st.info("No tradable markets within trading window (no open KXLOLGAME markets with confident linkage and active prices).")

# Open positions
st.subheader("Open positions")
open_pos = _open_positions(conn)
if not open_pos.empty:
    st.dataframe(open_pos, hide_index=True, use_container_width=True)
else:
    st.info("No open positions.")

# Recent decisions
st.subheader("Recent decisions (last 40)")
decisions = _recent_decisions(conn)
if not decisions.empty:
    st.dataframe(decisions, hide_index=True, use_container_width=True)
else:
    st.info("No decisions logged yet.")

# Auto-refresh
st.markdown("---")
from datetime import timezone as _tz
st.caption(f"Last refresh: {datetime.now(_tz.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC. "
           "Refresh manually for now (Streamlit's auto-refresh requires extra config).")
