"""FastAPI app — entry point for the v3.0 dashboard.

Run with:
    python -m loltrader.api.main
or:
    uvicorn loltrader.api.main:app --port 8502 --reload
"""
from __future__ import annotations

import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from loltrader.api.broker import MarketBroker
from loltrader.api.predict import load_latest_model, predict_all_active, predict_market
from loltrader.db import connect

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup + shutdown handler — wires up the Kalshi WS broker."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    broker = MarketBroker()
    app.state.broker = broker
    app.state.model = load_latest_model()
    await broker.startup()
    yield
    await broker.shutdown()


app = FastAPI(title="LoL Trader (v3.0)", lifespan=lifespan)


# --- REST endpoints --------------------------------------------------------

@app.get("/api/markets")
async def list_markets(league: str | None = None, limit: int = 100) -> dict:
    """List active LoL markets, optionally filtered by league."""
    from loltrader.ui.leagues import league_for_match

    with connect() as c:
        rows = c.execute(
            """
            SELECT m.market_ticker, m.event_ticker, m.title AS market_title,
                   m.status, m.yes_bid_cents, m.yes_ask_cents, m.last_price_cents,
                   m.close_time_unix, m.volume_24h,
                   e.title AS event_title, e.sub_title AS event_sub
            FROM kalshi_markets m
            LEFT JOIN kalshi_events e ON e.event_ticker = m.event_ticker
            WHERE m.status IN ('active', 'open')
              AND (m.series_ticker = 'KXLOLGAME' OR m.market_ticker LIKE 'KXLOLGAME-%')
              AND m.yes_ask_cents IS NOT NULL
            ORDER BY COALESCE(m.close_time_unix, 99999999999) ASC
            LIMIT ?
            """,
            (limit * 3,),  # over-fetch then filter
        ).fetchall()

    out = []
    for r in rows:
        d = dict(r)
        d["league"] = league_for_match(d.get("event_title") or d.get("market_title") or "",
                                       d.get("event_sub"))
        if league and d["league"] != league:
            continue
        out.append(d)
        if len(out) >= limit:
            break
    return {"markets": out, "count": len(out)}


@app.get("/api/games")
async def list_games(lookback_min: int = 240) -> dict:
    """Live games from Riot livestats — currently playing or recently ended.

    Each row includes the latest frame's team-level state so the frontend can
    show a compact game-state strip next to the relevant Kalshi market.
    Match markets to games client-side by comparing market_ticker's trailing
    team segment to ``blue_team_code`` / ``red_team_code``.
    """
    cutoff = int(time.time()) - lookback_min * 60
    with connect() as c:
        # Restrict to games whose latest frame is within `lookback_min` — this
        # filters out historical extraction data where game_end_ts_unix was
        # never backfilled.
        rows = c.execute(
            """
            SELECT g.game_id, g.league,
                   g.blue_team_code, g.red_team_code,
                   g.game_start_ts_unix, g.game_end_ts_unix,
                   g.winner_side, g.game_number,
                   f.frame_ts_unix, f.game_state,
                   f.blue_gold, f.blue_kills, f.blue_towers, f.blue_inhibitors,
                   f.blue_barons, f.blue_dragons_json,
                   f.red_gold, f.red_kills, f.red_towers, f.red_inhibitors,
                   f.red_barons, f.red_dragons_json
            FROM games_live g
            JOIN live_frames f
                   ON f.frame_id = (
                       SELECT frame_id FROM live_frames
                       WHERE game_id = g.game_id
                       ORDER BY frame_ts_unix DESC LIMIT 1
                   )
            WHERE f.frame_ts_unix >= ?
            ORDER BY f.frame_ts_unix DESC
            """,
            (cutoff,),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        # Compute a derived gold delta for convenience
        bg, rg = d.get("blue_gold"), d.get("red_gold")
        d["gold_diff"] = (bg - rg) if (bg is not None and rg is not None) else None
        # Parse dragon JSON to a count each (frontend doesn't need types)
        for side in ("blue", "red"):
            raw = d.pop(f"{side}_dragons_json", None)
            try:
                d[f"{side}_dragons"] = len(json.loads(raw)) if raw else 0
            except (TypeError, ValueError):
                d[f"{side}_dragons"] = 0
        out.append(d)
    return {"games": out, "count": len(out)}


@app.get("/api/orderbook/{market_ticker}")
async def get_orderbook(market_ticker: str) -> dict:
    """Latest orderbook snapshot for a market."""
    with connect() as c:
        row = c.execute(
            "SELECT updated_at_unix, yes_bids_json, yes_asks_json "
            "FROM kalshi_orderbook_latest WHERE market_ticker = ?",
            (market_ticker,),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="no orderbook yet")
    return {
        "market_ticker": market_ticker,
        "updated_at": row["updated_at_unix"],
        "bids": json.loads(row["yes_bids_json"] or "[]"),
        "asks": json.loads(row["yes_asks_json"] or "[]"),
    }


class TradeRequest(BaseModel):
    market_ticker: str
    side: str = Field(..., pattern="^(YES|NO|yes|no)$")
    contracts: int = Field(..., ge=1, le=10000)
    limit_price_cents: int = Field(..., ge=1, le=99)
    live_mode: bool = False
    note: str = ""


@app.post("/api/trade")
async def execute_trade(req: TradeRequest) -> dict:
    """Execute a buy order. Paper mode by default; live only if creds support write."""
    from loltrader.trader.execute import execute_buy, ExecutionError
    try:
        with connect() as conn:
            result = execute_buy(
                conn=conn,
                market_ticker=req.market_ticker,
                side=req.side.upper(),
                contracts=req.contracts,
                limit_price_cents=req.limit_price_cents,
                live_mode=req.live_mode,
                note=req.note,
            )
        return {
            "trade_id": result.trade_id,
            "decision_id": result.decision_id,
            "fill_price_cents": result.fill_price_cents,
            "fee_cents": result.fee_cents,
            "mode": result.mode,
            "order_id": result.order_id,
        }
    except ExecutionError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected: {e}")


@app.get("/api/predictions")
async def predictions(market_ticker: str | None = None) -> dict:
    """Pre-match model probability + market edge per linked market.

    Returns one row per active LoL market with a linked match (confidence >= 0.7).
    Model is v1 XGBoost trained on Glicko, recent form, H2H, draft, schedule,
    meta — see [docs/superpowers/specs/2026-05-19-lol-trading-bot-design.md].

    **Honest scope:** pre-match only (66.3% holdout accuracy). Stale once live
    state matters. Use as opening read, not mid-game oracle.
    """
    model = app.state.model
    if model is None:
        raise HTTPException(status_code=503, detail="model not loaded")
    with connect() as c:
        if market_ticker:
            p = predict_market(c, model, market_ticker)
            return {"predictions": [p.to_dict()] if p else [], "count": 1 if p else 0}
        preds = predict_all_active(c, model)
    return {
        "predictions": [p.to_dict() for p in preds],
        "count": len(preds),
        "model_info": {
            "feature_count": len(model.feature_spec),
            "ensemble_size": len(model._ensemble) if hasattr(model, "_ensemble") else 0,
        },
    }


@app.get("/api/trades")
async def list_trades(limit: int = 50) -> dict:
    """Recent paper+live trades with realized + unrealized P&L.

    Realized P&L (closed trades): ``settle_value_cents - fill_price_cents - entry_fee_cents``.
    Unrealized P&L (open trades): mark to ``yes_bid_cents`` for YES side,
    ``100 - yes_ask_cents`` for NO side, minus entry cost + entry fee.
    """
    with connect() as c:
        rows = c.execute(
            """
            SELECT t.trade_id, t.opened_at, t.closed_at,
                   t.side, t.contracts, t.fill_price_cents, t.entry_fee_cents,
                   t.settle_value_cents, t.pnl_cents, t.action_type,
                   d.market_ticker, d.action, d.made_by, d.gate_reason,
                   m.title AS market_title, m.yes_bid_cents, m.yes_ask_cents, m.status
            FROM paper_trades t
            JOIN decisions d ON d.decision_id = t.decision_id
            LEFT JOIN kalshi_markets m ON m.market_ticker = d.market_ticker
            ORDER BY t.opened_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    trades: list[dict] = []
    realized_total = 0
    unrealized_total = 0
    open_count = 0
    closed_count = 0

    for r in rows:
        d = dict(r)
        contracts = d["contracts"] or 0
        fill = d["fill_price_cents"] or 0
        fee = d["entry_fee_cents"] or 0
        if d["closed_at"] is not None:
            pnl = d["pnl_cents"] or 0
            d["pnl"] = pnl
            d["pnl_kind"] = "realized"
            realized_total += pnl
            closed_count += 1
        else:
            # Mark to market — null bid/ask means we just can't compute
            if d["side"] == "YES":
                mark = d["yes_bid_cents"]
            else:
                ask = d["yes_ask_cents"]
                mark = (100 - ask) if ask is not None else None
            if mark is not None:
                exit_value = mark * contracts
                cost = fill * contracts + fee
                pnl = exit_value - cost
            else:
                pnl = None
            d["pnl"] = pnl
            d["pnl_kind"] = "unrealized"
            if pnl is not None:
                unrealized_total += pnl
            open_count += 1
        trades.append(d)

    return {
        "trades": trades,
        "count": len(trades),
        "summary": {
            "realized_cents": realized_total,
            "unrealized_cents": unrealized_total,
            "total_cents": realized_total + unrealized_total,
            "open_positions": open_count,
            "closed_positions": closed_count,
        },
    }


@app.post("/api/refresh-markets")
async def refresh_markets() -> dict:
    """Trigger a Kalshi corpus fast-refresh (~10s)."""
    from loltrader.kalshi.corpus import fast_refresh_active_lol_markets
    from loltrader.kalshi.rest import KalshiClient
    from loltrader.config import load_config
    try:
        client = KalshiClient(load_config().kalshi)
        with connect() as c:
            n = fast_refresh_active_lol_markets(client, c)
        return {"ok": True, "count": n}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/live_winprob/{game_id}")
async def live_winprob(game_id: str) -> dict:
    """Return the live win-prob model's calibrated prediction for ``game_id``.

    404 if no prediction is available (game not started, model not loaded, or
    no in_game frames yet). Otherwise returns the full LivePrediction.

    Spec §Phase 5.
    """
    broker: MarketBroker = app.state.broker
    if not broker.winprob.is_ready:
        raise HTTPException(503, "winprob model not loaded")
    with connect() as c:
        pred = broker.winprob.predict(c, game_id)
    if pred is None:
        raise HTTPException(404, f"no prediction available for {game_id}")
    return broker.winprob.to_wire(pred)


@app.get("/api/health")
async def health() -> dict:
    """Quick liveness check: how many markets, how many clients connected."""
    broker: MarketBroker = app.state.broker
    return {
        "ok": True,
        "ts": int(time.time()),
        "ws_clients": len(broker.clients),
        "kalshi_ws_connected": broker.ws_client is not None,
        "markets_subscribed": len(broker.ws_client.market_tickers) if broker.ws_client else 0,
        "winprob_model_loaded": broker.winprob.is_ready,
        "winprob_model_version": broker.winprob._model_version if broker.winprob.is_ready else None,
    }


# --- WebSocket endpoint ----------------------------------------------------

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    """Browser WebSocket: receive subscribe/unsubscribe, broadcast ticker + orderbook updates.

    Message format from client:
        {"type": "subscribe", "tickers": ["KXLOLGAME-...", ...]}
        {"type": "unsubscribe", "tickers": [...]}
        {"type": "ping"}

    Server pushes:
        {"type": "ticker", "market_ticker": ..., "yes_bid": ..., "yes_ask": ..., "ts": ...}
        {"type": "orderbook", "market_ticker": ..., "bids": [[p,s],...], "asks": [[p,s],...], "ts": ...}
        {"type": "pong", "ts": ...}
    """
    await ws.accept()
    broker: MarketBroker = app.state.broker
    client = await broker.add_client(ws)
    try:
        while True:
            data = await ws.receive_text()
            try:
                msg = json.loads(data)
            except json.JSONDecodeError:
                continue
            mtype = msg.get("type")
            if mtype == "subscribe":
                tickers = set(msg.get("tickers") or [])
                client.subscribed_tickers |= tickers
            elif mtype == "unsubscribe":
                tickers = set(msg.get("tickers") or [])
                client.subscribed_tickers -= tickers
            elif mtype == "ping":
                client.last_ping_ts = time.time()
                await ws.send_text(json.dumps({"type": "pong", "ts": int(time.time() * 1000)}))
    except WebSocketDisconnect:
        pass
    finally:
        await broker.remove_client(client)


# --- Static file serving ---------------------------------------------------

# Mount static AFTER api routes so /api/* takes precedence
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    """Serve the dashboard HTML."""
    html_path = STATIC_DIR / "index.html"
    if not html_path.exists():
        return HTMLResponse(
            "<h1>LoL Trader v3</h1><p>Frontend not built yet.</p>",
            status_code=200,
        )
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


def main() -> int:
    """Entry point so `python -m loltrader.api.main` works."""
    import uvicorn
    uvicorn.run(
        "loltrader.api.main:app",
        host="0.0.0.0",
        port=8502,
        reload=False,
        log_level="info",
    )
    return 0


if __name__ == "__main__":
    main()
