"""Live-trading view for the dashboard (v2.0a decision-support tool).

Shows, for each currently-live LCK game:
  - In-game state from livestats (gold, kills, towers, dragons, barons, recent events)
  - Linked Kalshi markets (yes bid/ask, implied probability)
  - User's "your read" input + edge math against the market
  - Suggested action based on edge sign + magnitude

This is the v2.0a starting point — no model probability yet. The user IS the model;
the tool just makes the math + state visible in one place.
"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone

import pandas as pd
import streamlit as st


# ---------- data queries ----------


def _get_active_games(conn: sqlite3.Connection, lookback_min: int = 240) -> list[dict]:
    """Return games we should show in the live view.

    Includes:
      - games_live rows where game_end_ts_unix IS NULL (game not yet ended)
      - games where game_end_ts_unix is within the last ``lookback_min`` minutes
        (so we still see the closing book / resolution period)
    """
    cutoff = int(time.time()) - lookback_min * 60
    rows = conn.execute(
        """
        SELECT g.game_id, g.league, g.blue_team_code, g.red_team_code,
               g.game_start_ts_unix, g.game_end_ts_unix, g.first_seen_ts_unix,
               g.winner_side, g.esports_match_id, g.game_number, g.source
        FROM games_live g
        WHERE g.game_end_ts_unix IS NULL OR g.game_end_ts_unix >= ?
        ORDER BY g.first_seen_ts_unix DESC
        """,
        (cutoff,),
    ).fetchall()
    return [dict(r) for r in rows]


def _get_latest_frame(conn: sqlite3.Connection, game_id: str) -> dict | None:
    row = conn.execute(
        """
        SELECT frame_id, frame_ts_unix, game_state,
               blue_gold, blue_kills, blue_towers, blue_inhibitors,
               blue_dragons_json, blue_barons,
               red_gold, red_kills, red_towers, red_inhibitors,
               red_dragons_json, red_barons,
               raw_json
        FROM live_frames
        WHERE game_id = ?
        ORDER BY frame_ts_unix DESC LIMIT 1
        """,
        (game_id,),
    ).fetchone()
    return dict(row) if row else None


def _kills_in_window(conn: sqlite3.Connection, game_id: str,
                      latest_ts: int, window_sec: int = 60) -> tuple[int, int]:
    """Return (blue_kills_delta, red_kills_delta) over the last window_sec seconds."""
    cutoff = latest_ts - window_sec
    row = conn.execute(
        """
        SELECT
          (SELECT COALESCE(blue_kills, 0) FROM live_frames
           WHERE game_id = ? AND frame_ts_unix <= ?
           ORDER BY frame_ts_unix DESC LIMIT 1) AS old_blue_k,
          (SELECT COALESCE(red_kills, 0) FROM live_frames
           WHERE game_id = ? AND frame_ts_unix <= ?
           ORDER BY frame_ts_unix DESC LIMIT 1) AS old_red_k
        """,
        (game_id, cutoff, game_id, cutoff),
    ).fetchone()
    if not row:
        return (0, 0)
    # Get current
    cur = conn.execute(
        "SELECT blue_kills, red_kills FROM live_frames WHERE game_id = ? AND frame_ts_unix <= ? "
        "ORDER BY frame_ts_unix DESC LIMIT 1",
        (game_id, latest_ts),
    ).fetchone()
    if not cur:
        return (0, 0)
    cur_blue = int(cur["blue_kills"] or 0)
    cur_red = int(cur["red_kills"] or 0)
    old_blue = int(row["old_blue_k"] or 0)
    old_red = int(row["old_red_k"] or 0)
    return (cur_blue - old_blue, cur_red - old_red)


def _linked_markets(conn: sqlite3.Connection, game_id: str) -> list[dict]:
    """Find Kalshi markets linked to this game (via games_live.esports_match_id → matches)."""
    # Look up the v1 matches table via esports_match_id, then market_match_links
    rows = conn.execute(
        """
        SELECT m.market_ticker, m.event_ticker, m.title,
               m.yes_bid_cents, m.yes_ask_cents, m.last_price_cents, m.status,
               l.match_id, l.side AS link_side, l.confidence
        FROM kalshi_markets m
        JOIN market_match_links l ON l.market_ticker = m.market_ticker
        JOIN matches mt ON mt.match_id = l.match_id
        JOIN games_live g ON g.match_id = mt.match_id
        WHERE g.game_id = ? AND l.confidence >= 0.7
          AND m.status IN ('active', 'open')
        """,
        (game_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def _all_active_lol_markets(conn: sqlite3.Connection, limit: int = 40) -> list[dict]:
    """All currently-active LoL markets on Kalshi, regardless of game linkage.

    Useful when the user is watching a live game and needs to find the right
    market manually (linkage may not catch all games immediately).
    """
    rows = conn.execute(
        """
        SELECT market_ticker, event_ticker, title, status,
               yes_bid_cents, yes_ask_cents, last_price_cents,
               close_time_unix
        FROM kalshi_markets
        WHERE status IN ('active', 'open')
          AND (series_ticker = 'KXLOLGAME' OR market_ticker LIKE 'KXLOLGAME-%')
          AND yes_ask_cents IS NOT NULL
        ORDER BY COALESCE(close_time_unix, 99999999999) ASC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------- rendering ----------


def _fmt_game_clock(game_start_ts: int | None, current_frame_ts: int | None) -> str:
    """Return MM:SS in-game time, or '—' if unknown."""
    if not game_start_ts or not current_frame_ts:
        return "—"
    seconds = max(0, current_frame_ts - game_start_ts)
    m, s = divmod(seconds, 60)
    return f"{m:02d}:{s:02d}"


def _team_label(code: str | None, default: str) -> str:
    return code if code else default


def _render_game(conn: sqlite3.Connection, game: dict) -> None:
    """Render one game's panel."""
    game_id = game["game_id"]
    frame = _get_latest_frame(conn, game_id)

    blue_label = _team_label(game.get("blue_team_code"), "Blue")
    red_label = _team_label(game.get("red_team_code"), "Red")

    # Header
    header_cols = st.columns([3, 1, 1, 1])
    game_clock = _fmt_game_clock(game.get("game_start_ts_unix"),
                                  frame["frame_ts_unix"] if frame else None)
    state_label = "—"
    if frame:
        state_label = frame["game_state"].upper() if frame["game_state"] else "?"
    elif game.get("game_end_ts_unix"):
        state_label = "ENDED"

    header_cols[0].subheader(f"{blue_label} (blue) vs {red_label} (red)")
    header_cols[1].metric("In-game", game_clock)
    header_cols[2].metric("State", state_label)
    if game.get("game_number") is not None:
        header_cols[3].metric("Game #", str(game["game_number"]))

    if frame is None:
        st.info("No livestats frames yet for this game.")
        return

    # State row — gold / kills / towers / dragons / barons
    blue_dragons = json.loads(frame.get("blue_dragons_json") or "[]")
    red_dragons = json.loads(frame.get("red_dragons_json") or "[]")
    gold_diff = (frame.get("blue_gold") or 0) - (frame.get("red_gold") or 0)
    leader = blue_label if gold_diff > 0 else red_label if gold_diff < 0 else "even"

    cols = st.columns(6)
    cols[0].metric(f"{blue_label} gold", f"{frame['blue_gold'] or 0:,}")
    cols[1].metric(f"{red_label} gold", f"{frame['red_gold'] or 0:,}")
    cols[2].metric("Gold lead", f"{abs(gold_diff):,} for {leader}" if gold_diff else "even",
                    delta=int(gold_diff) if gold_diff else None)
    cols[3].metric("Kills", f"{frame['blue_kills']}/{frame['red_kills']}")
    cols[4].metric("Towers", f"{frame['blue_towers']}/{frame['red_towers']}")
    cols[5].metric("Inhibs", f"{frame['blue_inhibitors']}/{frame['red_inhibitors']}")

    # Drakes + barons
    cols2 = st.columns(4)
    cols2[0].markdown(f"**{blue_label} drakes:** {', '.join(blue_dragons) if blue_dragons else '—'}")
    cols2[1].markdown(f"**{red_label} drakes:** {', '.join(red_dragons) if red_dragons else '—'}")
    cols2[2].metric("Barons", f"{frame['blue_barons']}/{frame['red_barons']}")
    # Soul thresholds
    blue_elementals = [d for d in blue_dragons if d.lower() != "elder"]
    red_elementals = [d for d in red_dragons if d.lower() != "elder"]
    soul_status = "—"
    if len(blue_elementals) >= 4:
        soul_status = f"{blue_label} SOUL"
    elif len(red_elementals) >= 4:
        soul_status = f"{red_label} SOUL"
    elif len(blue_elementals) == 3 or len(red_elementals) == 3:
        soul_status = "Soul point"
    cols2[3].metric("Soul", soul_status)

    # Recent activity
    if frame.get("frame_ts_unix"):
        blue_60, red_60 = _kills_in_window(conn, game_id, frame["frame_ts_unix"], 60)
        if blue_60 or red_60:
            st.markdown(f"**Last 60s:** {blue_label} +{blue_60} kills, {red_label} +{red_60} kills")

    # Markets
    markets = _linked_markets(conn, game_id)
    if not markets:
        st.warning("No linked Kalshi markets found for this game (or all markets are closed).")
        return

    st.markdown("**Kalshi markets:**")
    for m in markets:
        side_label = "YES = " + (blue_label if m.get("link_side") == 1 else red_label)
        ask = (m.get("yes_ask_cents") or 0) / 100
        bid = (m.get("yes_bid_cents") or 0) / 100
        implied = ask if ask > 0 else 0  # implied prob of yes if you buy at ask

        # User's read input
        slider_key = f"read_{m['market_ticker']}"
        your_read = st.slider(
            f"Your read of P({side_label}): {m['market_ticker']}",
            min_value=0, max_value=100, value=int(implied * 100),
            key=slider_key,
            help=f"{m.get('title')[:80] if m.get('title') else ''}",
        )
        your_prob = your_read / 100

        # Edge math
        edge_yes = your_prob - ask   # positive → buy YES is +EV
        edge_no = bid - your_prob    # positive → buy NO is +EV
        action = "HOLD"
        if edge_yes > 0.05:
            action = f"BUY YES @ {ask:.2f}"
        elif edge_no > 0.05:
            action = f"BUY NO @ {bid:.2f}"

        ec = st.columns(5)
        ec[0].metric("Book bid/ask", f"{bid:.2f} / {ask:.2f}")
        ec[1].metric("Implied %", f"{implied*100:.0f}%")
        ec[2].metric("Your %", f"{your_read}%")
        ec[3].metric("Edge YES", f"{edge_yes:+.2f}")
        ec[4].metric("Edge NO",  f"{edge_no:+.2f}")
        if action != "HOLD":
            st.success(f"Suggested: **{action}**  (edge {max(edge_yes, edge_no):+.2f})")

    st.divider()


def render_live_view(conn: sqlite3.Connection) -> None:
    """Top-level renderer. Called from app.py."""
    st.header("Live Trading — active games")

    # Auto-refresh controls (top row)
    refresh_cols = st.columns([1, 1, 2])
    autorefresh_on = refresh_cols[0].toggle(
        "Auto-refresh", value=True,
        help="Re-poll DB every N seconds. Turn off if you're typing in a slider.",
    )
    interval_sec = refresh_cols[1].number_input(
        "Refresh sec", min_value=2, max_value=60, value=5, step=1,
        label_visibility="collapsed",
    )
    if autorefresh_on:
        try:
            from streamlit_autorefresh import st_autorefresh
            st_autorefresh(interval=int(interval_sec) * 1000, key="live_view_refresh")
        except ImportError:
            refresh_cols[2].warning(
                "streamlit-autorefresh not installed. Run `pip install streamlit-autorefresh`."
            )

    games = _get_active_games(conn)

    # Filter out historical_backtest games unless user explicitly toggles
    show_historical = st.checkbox(
        "Include historical (backtest) games for review",
        value=False,
        help="If no live games are happening, you can browse extracted historical games here.",
    )
    if not show_historical:
        games = [g for g in games if g.get("source") != "historical_backtest"]

    if not games:
        if show_historical:
            st.info("No games in DB. Run `python -m loltrader.tools.backtest_extract` or "
                     "start `python -m loltrader.tools.game_discovery` during a live LCK window.")
        else:
            st.info(
                "No live LCK games being tracked. To track: run\n"
                "`python -m loltrader.tools.game_discovery` during an LCK window."
            )
    else:
        st.caption(f"Showing {len(games)} game(s).")
        for game in games:
            _render_game(conn, game)

    # Trade log + open positions — always shown so user can record manual trades
    st.divider()
    _render_trade_log_panel(conn)

    # Active markets panel — always shown, even when no live games are being tracked
    st.divider()
    _render_active_markets_panel(conn)


def _log_manual_trade(
    conn: sqlite3.Connection,
    market_ticker: str,
    side: str,
    contracts: int,
    fill_price_cents: int,
    note: str = "",
) -> int:
    """Insert a decisions + paper_trades pair for a manually-executed trade.

    Returns the new trade_id.
    """
    from loltrader.backtest.fees import kalshi_fee_cents

    now = int(time.time())
    fee = kalshi_fee_cents(fill_price_cents, contracts)

    # Look up market book at time of trade for provenance
    mk = conn.execute(
        "SELECT yes_bid_cents, yes_ask_cents FROM kalshi_markets WHERE market_ticker = ?",
        (market_ticker,),
    ).fetchone()
    bid_c = mk["yes_bid_cents"] if mk else None
    ask_c = mk["yes_ask_cents"] if mk else None

    action = f"BUY_{side}"
    cursor = conn.execute(
        """
        INSERT INTO decisions (
            decision_ts, market_ticker, action, made_by, gate_reason,
            market_yes_bid_cents, market_yes_ask_cents, action_type
        ) VALUES (?, ?, ?, 'user', ?, ?, ?, 'OPEN')
        """,
        (now, market_ticker, action, note or None, bid_c, ask_c),
    )
    decision_id = cursor.lastrowid

    conn.execute(
        """
        INSERT INTO paper_trades (
            decision_id, opened_at, side, contracts,
            fill_price_cents, entry_fee_cents, action_type
        ) VALUES (?, ?, ?, ?, ?, ?, 'OPEN')
        """,
        (decision_id, now, side, contracts, fill_price_cents, fee),
    )
    trade_id = cursor.lastrowid
    conn.commit()
    return trade_id


def _close_trade(
    conn: sqlite3.Connection,
    trade_id: int,
    exit_price_cents: int,
) -> int:
    """Close an open position. Computes P&L (including entry+exit fees).

    Returns realized pnl in cents.
    """
    from loltrader.backtest.fees import kalshi_fee_cents

    row = conn.execute(
        "SELECT contracts, fill_price_cents, entry_fee_cents, side FROM paper_trades WHERE trade_id = ?",
        (trade_id,),
    ).fetchone()
    if not row:
        raise ValueError(f"trade {trade_id} not found")

    contracts = row["contracts"]
    entry_price_c = row["fill_price_cents"]
    entry_fee_c = row["entry_fee_cents"]
    side = row["side"]

    exit_fee = kalshi_fee_cents(exit_price_cents, contracts)
    # YES contracts: P&L = contracts * (exit - entry) - both fees
    # NO contracts: P&L = contracts * (entry - exit) - both fees  (we "buy NO" effectively means we want price to drop)
    # Kalshi treats NO as "buy at (100-X)" — for simplicity treat YES and NO symmetrically here.
    if side == "YES":
        gross = contracts * (exit_price_cents - entry_price_c)
    else:  # NO — we benefit if price went DOWN (since we paid 100-X effectively)
        # We bought at (100-entry_price_c) effectively; close at (100-exit_price_cents)
        gross = contracts * (entry_price_c - exit_price_cents)
    pnl = gross - entry_fee_c - exit_fee
    now = int(time.time())
    conn.execute(
        "UPDATE paper_trades SET closed_at=?, settle_value_cents=?, pnl_cents=? WHERE trade_id=?",
        (now, exit_price_cents, pnl, trade_id),
    )
    conn.commit()
    return pnl


def _render_trade_log_panel(conn: sqlite3.Connection) -> None:
    """Form to record a manual trade + show recent + open positions."""
    st.subheader("Manual trade log")
    st.caption(
        "Record trades you placed on Kalshi here. The tool tracks P&L automatically. "
        "One-click execution comes in Phase B."
    )

    with st.expander("➕ Log a new trade", expanded=False):
        col1, col2, col3, col4, col5 = st.columns([3, 1, 1, 1, 2])
        market_ticker = col1.text_input("Market ticker",
                                          placeholder="KXLOLGAME-...", key="trade_market")
        side = col2.selectbox("Side", ["YES", "NO"], key="trade_side")
        contracts = col3.number_input("Contracts", min_value=1, value=10, step=1, key="trade_contracts")
        fill_price = col4.number_input("Fill price (¢)", min_value=1, max_value=99, value=50, step=1, key="trade_price")
        note = col5.text_input("Note (optional)", placeholder="why I took this trade",
                                 key="trade_note")
        if st.button("Log trade", type="primary"):
            if not market_ticker.strip():
                st.error("Market ticker is required")
            else:
                try:
                    trade_id = _log_manual_trade(
                        conn, market_ticker.strip(), side, int(contracts),
                        int(fill_price), note,
                    )
                    st.success(f"Logged trade #{trade_id}: {side} × {contracts} @ {fill_price}¢")
                except Exception as e:
                    st.error(f"Failed to log: {e}")

    # Open positions table
    open_rows = conn.execute(
        """
        SELECT t.trade_id, t.opened_at, t.side, t.contracts,
               t.fill_price_cents, t.entry_fee_cents,
               d.market_ticker, d.gate_reason
        FROM paper_trades t
        JOIN decisions d ON d.decision_id = t.decision_id
        WHERE t.closed_at IS NULL AND d.made_by = 'user'
        ORDER BY t.opened_at DESC
        """
    ).fetchall()
    if open_rows:
        st.markdown("**Open positions (manual):**")
        for r in open_rows:
            cols = st.columns([2, 1, 1, 1, 2, 2])
            cols[0].text(r["market_ticker"])
            cols[1].text(f"{r['side']} x{r['contracts']}")
            cols[2].text(f"@ {r['fill_price_cents']}¢")
            cost = r["contracts"] * r["fill_price_cents"] + r["entry_fee_cents"]
            cols[3].text(f"${cost/100:.2f}")
            close_price = cols[4].number_input(
                "Exit ¢", min_value=1, max_value=99, value=r["fill_price_cents"],
                key=f"close_{r['trade_id']}", label_visibility="collapsed",
            )
            if cols[5].button(f"Close #{r['trade_id']}", key=f"close_btn_{r['trade_id']}"):
                pnl = _close_trade(conn, r["trade_id"], int(close_price))
                if pnl >= 0:
                    st.success(f"Closed #{r['trade_id']}: +${pnl/100:.2f} profit")
                else:
                    st.error(f"Closed #{r['trade_id']}: -${abs(pnl)/100:.2f} loss")
                st.rerun()
    else:
        st.caption("No open manual positions.")

    # Recent closed trades + running P&L
    recent_closed = conn.execute(
        """
        SELECT t.trade_id, t.opened_at, t.closed_at, t.side, t.contracts,
               t.fill_price_cents, t.settle_value_cents, t.pnl_cents,
               d.market_ticker
        FROM paper_trades t
        JOIN decisions d ON d.decision_id = t.decision_id
        WHERE t.closed_at IS NOT NULL AND d.made_by = 'user'
        ORDER BY t.closed_at DESC LIMIT 20
        """
    ).fetchall()
    if recent_closed:
        st.markdown("**Recently closed (last 20):**")
        rows = []
        cumulative = 0
        for r in recent_closed:
            pnl_d = (r["pnl_cents"] or 0) / 100
            cumulative += pnl_d
            opened_dt = datetime.fromtimestamp(r["opened_at"], tz=timezone.utc).strftime("%m-%d %H:%M")
            rows.append({
                "Trade #": r["trade_id"],
                "Opened": opened_dt,
                "Market": r["market_ticker"][:30],
                "Side x Contracts": f"{r['side']} x{r['contracts']}",
                "Entry": f"{r['fill_price_cents']}¢",
                "Exit": f"{r['settle_value_cents']}¢",
                "P&L": f"${pnl_d:+.2f}",
            })
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
        # Total P&L
        total_pnl = sum((r["pnl_cents"] or 0) for r in recent_closed) / 100
        wins = sum(1 for r in recent_closed if (r["pnl_cents"] or 0) > 0)
        st.caption(
            f"**Last-20 P&L: ${total_pnl:+.2f}** ({wins}/{len(recent_closed)} wins). "
            f"Avg ${total_pnl/len(recent_closed):+.2f}/trade."
        )


def _render_active_markets_panel(conn: sqlite3.Connection) -> None:
    """All currently-active Kalshi LoL markets, regardless of game linkage.

    Lets the user find + read prices for any market they're trading manually.
    """
    st.subheader("Active Kalshi LoL markets")

    markets = _all_active_lol_markets(conn, limit=80)
    if not markets:
        st.info(
            "No active LoL markets in DB. Run "
            "`python -m loltrader.tools.daily_logger` to refresh the Kalshi corpus."
        )
        return

    # Build a clean dataframe with edge calc support
    rows = []
    now = int(time.time())
    for m in markets:
        bid_c = m.get("yes_bid_cents") or 0
        ask_c = m.get("yes_ask_cents") or 0
        last_c = m.get("last_price_cents") or 0
        close_ts = m.get("close_time_unix")
        hours_to_close = (close_ts - now) / 3600 if close_ts else None
        rows.append({
            "Market": m["market_ticker"],
            "Title": (m.get("title") or "")[:55],
            "Bid": f"{bid_c/100:.2f}" if bid_c else "—",
            "Ask": f"{ask_c/100:.2f}" if ask_c else "—",
            "Last": f"{last_c/100:.2f}" if last_c else "—",
            "Implied %": f"{ask_c}%" if ask_c else "—",
            "Spread (¢)": (ask_c - bid_c) if (bid_c and ask_c) else None,
            "Closes in (h)": f"{hours_to_close:.1f}" if hours_to_close else "—",
        })

    df = pd.DataFrame(rows)
    st.dataframe(df, hide_index=True, use_container_width=True)
    st.caption(
        f"{len(rows)} active KXLOLGAME markets. Sorted by closest-to-close. "
        "Run `python -m loltrader.tools.daily_logger` to refresh prices."
    )
